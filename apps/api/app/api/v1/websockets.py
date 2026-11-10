"""Authenticated run event WebSocket with replay and live catch-up."""

import asyncio
import hmac
from typing import Annotated, Literal, TypeAlias, cast
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.agents.schemas import PlanDocument
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.core.security import SESSION_COOKIE, verify_session_token
from app.core.validation import validation_message
from app.events.bus import channel_for_run
from app.events.redis import get_redis
from app.models.event import Event
from app.repos.events import EventRepository
from app.repos.runs import RunRepository
from app.repos.users import UserRepository
from app.services.orchestration import (
    InvalidAnswers,
    PlanService,
    QuestionService,
    WorkflowConflict,
    enqueue_advance,
)

router = APIRouter()


class AnswersSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["answers.submit"]
    answers: dict[str, object]


class ApprovalDecide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["approval.decide"]
    approval_id: UUID
    decision: Literal["approve", "reject", "edit"]
    edited_payload: dict[str, object] | None = None


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["user.message"]
    content: str
    agent_id: UUID | None = None


class PlanEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["plan.edit"]
    plan: dict[str, object]


class RunControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["run.pause", "run.resume", "run.cancel"]


class AgentInterrupt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["agent.interrupt"]
    agent_id: UUID


ClientMessage: TypeAlias = Annotated[  # noqa: UP040
    AnswersSubmit | ApprovalDecide | UserMessage | PlanEdit | RunControl | AgentInterrupt,
    Field(discriminator="type"),
]
_client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter[ClientMessage](
    cast(type[ClientMessage], ClientMessage)
)


async def _authorized_user(websocket: WebSocket) -> UUID | None:
    claims = verify_session_token(websocket.cookies.get(SESSION_COOKIE))
    if claims is None:
        return None
    user_id, token_version = claims
    async with SessionFactory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None or user.session_version != token_version:
            return None
    return user_id


async def _replay_until(
    websocket: WebSocket, user_id: UUID, run_id: UUID, after_seq: int, through_seq: int
) -> int:
    last_seq = after_seq
    while last_seq < through_seq:
        async with SessionFactory() as session:
            events = await EventRepository(session).list_after(
                user_id, run_id, last_seq, through_seq=through_seq, limit=500
            )
        if not events:
            break
        for event in events:
            if event.seq <= last_seq:
                continue
            await websocket.send_json(_event_payload(event))
            last_seq = event.seq
    return last_seq


def _event_payload(event: Event) -> dict[str, object]:
    return cast(
        dict[str, object],
        jsonable_encoder(
            {
                "id": event.id,
                "run_id": event.run_id,
                "seq": event.seq,
                "type": event.type,
                "payload": event.payload,
                "created_at": event.created_at,
            }
        ),
    )


async def _catch_up(websocket: WebSocket, user_id: UUID, run_id: UUID, last_seq: int) -> int:
    async with SessionFactory() as session:
        high_watermark = await EventRepository(session).high_watermark(user_id, run_id)
    if high_watermark is None:
        return last_seq
    return await _replay_until(websocket, user_id, run_id, last_seq, high_watermark)


@router.websocket("/ws/runs/{run_id}")
async def run_event_stream(websocket: WebSocket, run_id: UUID, after_seq: int = 0) -> None:
    """Subscribe before taking the replay watermark, then reconcile from Postgres."""
    expected_origin = get_settings().web_origin.rstrip("/")
    origin = websocket.headers.get("origin", "").rstrip("/")
    if not origin or not hmac.compare_digest(origin, expected_origin):
        await websocket.close(code=4403)
        return
    user_id = await _authorized_user(websocket)
    if user_id is None:
        await websocket.close(code=4401)
        return
    if after_seq < 0:
        await websocket.close(code=4400)
        return
    async with SessionFactory() as session:
        run = await RunRepository(session).get_for_user(user_id, run_id)
    if run is None:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    pubsub = get_redis().pubsub()
    listener_task: asyncio.Task[None] | None = None
    receive_task: asyncio.Task[str] | None = None
    notifications: asyncio.Queue[str] = asyncio.Queue()

    async def listen() -> None:
        async for message in pubsub.listen():
            if message.get("type") == "message":
                await notifications.put(str(message.get("data", "")))

    try:
        await pubsub.subscribe(channel_for_run(run_id))
        listener_task = asyncio.create_task(listen())
        async with SessionFactory() as session:
            watermark = await EventRepository(session).high_watermark(user_id, run_id)
        last_seq = await _replay_until(
            websocket, user_id, run_id, after_seq, watermark or after_seq
        )
        receive_task = asyncio.create_task(websocket.receive_text())
        while True:
            try:
                await asyncio.wait_for(notifications.get(), timeout=0.5)
            except TimeoutError:
                pass
            last_seq = await _catch_up(websocket, user_id, run_id, last_seq)
            if receive_task.done():
                try:
                    raw_message = receive_task.result()
                except WebSocketDisconnect:
                    break
                try:
                    client_message = _client_message_adapter.validate_json(raw_message)
                except ValidationError:
                    await websocket.send_json(
                        {
                            "type": "invalid_message",
                            "message": "Message did not match a supported shape.",
                        }
                    )
                else:
                    try:
                        if isinstance(client_message, AnswersSubmit):
                            async with SessionFactory() as session:
                                await QuestionService(session).submit(
                                    user_id, run_id, client_message.answers
                                )
                            await enqueue_advance(user_id, run_id)
                            await websocket.send_json(
                                {"type": "request.accepted", "request_type": client_message.type}
                            )
                        elif isinstance(client_message, PlanEdit):
                            plan = PlanDocument.model_validate(client_message.plan)
                            async with SessionFactory() as session:
                                await PlanService(session).update(user_id, run_id, plan)
                            await websocket.send_json(
                                {"type": "request.accepted", "request_type": client_message.type}
                            )
                        else:
                            await websocket.send_json(
                                {
                                    "type": "not_implemented_yet",
                                    "request_type": client_message.type,
                                }
                            )
                    except ValidationError as error:
                        message = validation_message(error)
                        if isinstance(client_message, PlanEdit):
                            message = f"Plan is invalid: {message}"
                        await websocket.send_json(
                            {
                                "type": "request.rejected",
                                "request_type": client_message.type,
                                "message": message,
                            }
                        )
                    except (InvalidAnswers, WorkflowConflict) as error:
                        await websocket.send_json(
                            {
                                "type": "request.rejected",
                                "request_type": client_message.type,
                                "message": str(error),
                            }
                        )
                    except LookupError:
                        await websocket.send_json(
                            {
                                "type": "request.rejected",
                                "request_type": client_message.type,
                                "message": "The request is invalid for the current run state.",
                            }
                        )
                receive_task = asyncio.create_task(websocket.receive_text())
    except WebSocketDisconnect:
        return
    finally:
        for task in (listener_task, receive_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (listener_task, receive_task) if task is not None),
            return_exceptions=True,
        )
        await pubsub.unsubscribe(channel_for_run(run_id))
        await pubsub.close()
