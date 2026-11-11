"""Approval decisions and autonomy changes for active runs."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.events.bus import EventBus
from app.models.approval import Approval
from app.models.event import Event
from app.models.run import Run
from app.orchestrator.state import stage_transition
from app.repos.approvals import ApprovalRepository
from app.repos.runs import RunRepository


class ApprovalConflict(ValueError):
    """Raised when a decision is incompatible with the current approval state."""


class ApprovalService:
    """Handle user-owned approval listing, resolution, and autonomy changes."""

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._bus = event_bus or EventBus()

    async def list_for_run(self, user_id: UUID, run_id: UUID) -> list[Approval]:
        run = await RunRepository(self._session).get_for_user(user_id, run_id)
        if run is None:
            raise LookupError("Run not found.")
        return await ApprovalRepository(self._session).list_for_run(user_id, run_id)

    async def decide(
        self,
        user_id: UUID,
        approval_id: UUID,
        decision: str,
        *,
        edited_payload: dict[str, object] | None = None,
    ) -> tuple[Approval, dict[str, object] | None]:
        """Resolve a pending approval and resume its run transactionally."""
        approval = await ApprovalRepository(self._session).get_for_user(user_id, approval_id)
        if approval is None:
            raise LookupError("Approval not found.")
        run = await RunRepository(self._session).get_for_user(user_id, approval.run_id, lock=True)
        if run is None:
            raise LookupError("Approval not found.")
        approval = await ApprovalRepository(self._session).get_for_user(
            user_id, approval_id, lock=True
        )
        if approval is None:
            raise LookupError("Approval not found.")
        if approval.status != "pending":
            raise ApprovalConflict("This approval has already been decided.")
        if run.status != "AWAITING_APPROVAL":
            raise ApprovalConflict("The run is not waiting for this approval.")
        if decision not in {"approve", "reject", "edit"}:
            raise ValueError("Decision must be approve, reject, or edit.")
        if decision == "edit" and edited_payload is None:
            raise ValueError("An edited payload is required for an edit decision.")
        if decision != "edit" and edited_payload is not None:
            raise ValueError("An edited payload is accepted only for an edit decision.")

        now = datetime.now(UTC)
        resolved_status = {"approve": "approved", "reject": "rejected", "edit": "edited"}[decision]
        approval.status = resolved_status
        approval.edited_payload = edited_payload
        approval.decided_at = now
        approval.decided_by = user_id
        feedback: dict[str, object] | None = None
        event_payload: dict[str, object] = {
            "approval_id": str(approval.id),
            "decision": decision,
            "status": resolved_status,
            "agent_id": str(approval.agent_id) if approval.agent_id else None,
        }
        if decision == "reject":
            feedback = {
                "kind": "approval_rejected",
                "approval_id": str(approval.id),
                "action": approval.action_type,
                "instruction": "Do not execute this action; replan using the user's rejection.",
            }
            event_payload["feedback"] = feedback

        resolved_event = await self._bus.persist(
            self._session,
            user_id,
            approval.run_id,
            event_type="approval.resolved",
            payload=event_payload,
        )
        state_event = await stage_transition(
            self._session,
            user_id,
            run,
            "EXECUTING",
            event_bus=self._bus,
            payload={"approval_id": str(approval.id), "decision": decision},
        )
        await self._session.commit()
        events = await _refresh_events(self._session, [resolved_event, state_event])
        await _publish_all(self._bus, events)
        return approval, feedback

    async def update_autonomy(self, user_id: UUID, run_id: UUID, level: str) -> Run:
        """Update the user's selected level; the next gate reads the new value."""
        run = await RunRepository(self._session).get_for_user(user_id, run_id, lock=True)
        if run is None:
            raise LookupError("Run not found.")
        if run.status not in {
            "CLARIFYING",
            "AWAITING_ANSWERS",
            "PLANNING",
            "AWAITING_PLAN_APPROVAL",
            "EXECUTING",
            "AWAITING_APPROVAL",
        }:
            raise ApprovalConflict("Autonomy can be changed only while a run is active.")
        previous = run.autonomy_level
        if previous == level:
            return run
        run.autonomy_level = level
        event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="run.autonomy_changed",
            payload={"from": previous, "to": level},
        )
        if event is None:
            raise LookupError("Run not found.")
        await self._session.commit()
        await self._session.refresh(run)
        await self._session.refresh(event)
        await self._bus.publish(event)
        return run


async def enqueue_approval_resume(user_id: UUID, run_id: UUID, approval_id: UUID) -> None:
    """Queue the resolved tool once; no job is created while approval is pending."""
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(
            "resume_approval",
            str(user_id),
            str(run_id),
            str(approval_id),
            _job_id=f"approval-resume:{approval_id}",
        )
    finally:
        await pool.close()


async def _refresh_events(session: AsyncSession, events: list[Event | None]) -> list[Event]:
    refreshed: list[Event] = []
    for event in events:
        if isinstance(event, Event):
            await session.refresh(event)
            refreshed.append(event)
    return refreshed


async def _publish_all(bus: EventBus, events: list[Event]) -> None:
    for event in events:
        await bus.publish(event)
