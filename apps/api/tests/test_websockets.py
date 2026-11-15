"""Postgres and Redis integration tests for run event WebSockets."""

import asyncio
from collections.abc import Callable
from threading import Event as ThreadEvent
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

import app.api.v1.websockets as websocket_module
from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.models.agent import Agent
from app.repos.events import EventRepository
from tests.conftest import WEB_ORIGIN


def signup(client: TestClient, email: str) -> UUID:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "m2-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def create_run(client: TestClient) -> UUID:
    response = client.post(
        "/api/v1/runs",
        json={"idea": "WebSocket replay test", "autonomy_level": "step_by_step", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def test_unauthenticated_websocket_is_rejected(api_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with api_client.websocket_connect(
            f"/ws/runs/{UUID(int=1)}", headers={"Origin": WEB_ORIGIN}
        ):
            pass
    assert closed.value.code == 4401


def test_websocket_allows_configured_origin(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    run_id = create_run(api_client)
    with api_client.websocket_connect(
        f"/ws/runs/{run_id}?after_seq=0", headers={"Origin": WEB_ORIGIN}
    ) as connection:
        assert connection.receive_json()["seq"] == 1


def test_websocket_rejects_missing_origin(api_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with api_client.websocket_connect(f"/ws/runs/{UUID(int=1)}", headers={}):
            pass
    assert closed.value.code == 4403


def test_websocket_rejects_mismatched_origin(api_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with api_client.websocket_connect(
            f"/ws/runs/{UUID(int=1)}", headers={"Origin": "https://attacker.example"}
        ):
            pass
    assert closed.value.code == 4403


def test_cross_tenant_user_cannot_read_events_or_connect_to_run(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    run_id = create_run(api_client)

    async def create_agent() -> UUID:
        async with SessionFactory() as session:
            agent = Agent(run_id=run_id, role="coder", config={}, status="created")
            session.add(agent)
            await session.commit()
            return agent.id

    agent_id = api_client.portal.call(create_agent)
    signup(api_client, test_emails())

    assert api_client.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert api_client.get(f"/api/v1/runs/{run_id}/events").status_code == 404
    assert api_client.get(f"/api/v1/runs/{run_id}/agents").status_code == 404
    assert api_client.get(f"/api/v1/runs/{run_id}/tasks").status_code == 404
    headers = {"Origin": WEB_ORIGIN}
    assert api_client.post(f"/api/v1/runs/{run_id}/pause", headers=headers).status_code == 404
    assert api_client.post(f"/api/v1/runs/{run_id}/resume", headers=headers).status_code == 404
    assert api_client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 404
    assert (
        api_client.post(
            f"/api/v1/runs/{run_id}/messages",
            json={"content": "hello"},
            headers=headers,
        ).status_code
        == 404
    )
    interrupted = api_client.post(
        f"/api/v1/agents/{agent_id}/interrupt", headers=headers
    )
    assert interrupted.status_code == 404
    with pytest.raises(WebSocketDisconnect) as closed:
        with api_client.websocket_connect(
            f"/ws/runs/{run_id}?after_seq=0", headers={"Origin": WEB_ORIGIN}
        ):
            pass
    assert closed.value.code == 4403


def test_run_controls_reject_wrong_states(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    run_id = create_run(api_client)
    headers = {"Origin": WEB_ORIGIN}
    assert api_client.post(f"/api/v1/runs/{run_id}/resume", headers=headers).status_code == 409
    rejected = api_client.post(
        f"/api/v1/runs/{run_id}/messages",
        json={"content": "Continue the work."},
        headers=headers,
    )
    assert rejected.status_code == 409


def test_reconnect_replays_events_appended_during_replay_without_gaps_or_duplicates(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signup(api_client, test_emails())
    run_id = create_run(api_client)
    user_id = UUID(api_client.get("/api/v1/me").json()["id"])
    original_replay = websocket_module._replay_until
    replay_paused = ThreadEvent()
    resume_replay = ThreadEvent()

    async def append_during_initial_replay(
        websocket: WebSocket,
        replay_user_id: UUID,
        replay_run_id: UUID,
        after_seq: int,
        through_seq: int,
    ) -> int:
        if after_seq > 0:
            return await original_replay(
                websocket, replay_user_id, replay_run_id, after_seq, through_seq
            )
        async with SessionFactory() as session:
            events = await EventRepository(session).list_after(
                replay_user_id, replay_run_id, after_seq, through_seq=through_seq
            )
        last = after_seq
        for event in events:
            await websocket.send_json(websocket_module._event_payload(event))
            last = event.seq
        replay_paused.set()
        assert await asyncio.to_thread(resume_replay.wait, 5)
        return last

    monkeypatch.setattr(websocket_module, "_replay_until", append_during_initial_replay)
    with api_client.websocket_connect(
        f"/ws/runs/{run_id}?after_seq=0", headers={"Origin": WEB_ORIGIN}
    ) as first_connection:
        first = first_connection.receive_json()
        assert first["seq"] == 1
        assert replay_paused.wait(5)

        async def append_racing_event() -> None:
            await EventBus().append(
                user_id,
                run_id,
                event_type="run.replay_race",
                payload={"after": 1},
            )

        api_client.portal.call(append_racing_event)
        resume_replay.set()

    with api_client.websocket_connect(
        f"/ws/runs/{run_id}?after_seq=1", headers={"Origin": WEB_ORIGIN}
    ) as reconnected:
        replayed = reconnected.receive_json()
        assert replayed["seq"] == 2
        reconnected.send_json({"type": "run.pause"})
        accepted = reconnected.receive_json()
        assert accepted == {"type": "request.accepted", "request_type": "run.pause"}
