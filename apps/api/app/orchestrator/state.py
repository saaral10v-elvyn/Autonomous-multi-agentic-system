"""Single source of truth for legal run state transitions."""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.bus import EventBus
from app.models.event import Event
from app.models.run import Run
from app.repos.runs import RunRepository

ACTIVE_STATES = {
    "CREATED",
    "CLARIFYING",
    "AWAITING_ANSWERS",
    "PLANNING",
    "AWAITING_PLAN_APPROVAL",
    "EXECUTING",
    "AWAITING_APPROVAL",
}
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"CLARIFYING"},
    "CLARIFYING": {"AWAITING_ANSWERS"},
    "AWAITING_ANSWERS": {"PLANNING"},
    "PLANNING": {"AWAITING_PLAN_APPROVAL", "EXECUTING"},
    "AWAITING_PLAN_APPROVAL": {"EXECUTING"},
    "EXECUTING": {"AWAITING_APPROVAL"},
    "AWAITING_APPROVAL": {"EXECUTING"},
    "PAUSED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}


class IllegalRunTransition(ValueError):
    """Raised if a run transition is not permitted by the state machine."""


def validate_transition(current: str, target: str) -> None:
    """Raise for an illegal state transition."""
    if target in {"FAILED", "CANCELLED"} and current in ACTIVE_STATES | {"PAUSED"}:
        return
    if target == "PAUSED" and current in ACTIVE_STATES:
        return
    if current == "PAUSED" and target in ACTIVE_STATES:
        return
    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise IllegalRunTransition(f"Cannot transition run from {current} to {target}.")


async def stage_transition(
    session: AsyncSession,
    user_id: UUID,
    run: Run,
    target: str,
    *,
    event_bus: EventBus,
    payload: Mapping[str, object] | None = None,
) -> Event:
    """Stage a state update and event in the caller's transaction."""
    validate_transition(run.status, target)
    previous = run.status
    run.status = target
    if target == "PAUSED":
        run.paused_from_status = previous
    elif previous == "PAUSED":
        run.paused_from_status = None
    body: dict[str, object] = {"from": previous, "status": target}
    if payload:
        body.update(payload)
    event = await event_bus.persist(
        session, user_id, run.id, event_type="run.state_changed", payload=body
    )
    if event is None:
        raise LookupError("Run not found.")
    return event


async def transition_run(
    session: AsyncSession,
    user_id: UUID,
    run_id: UUID,
    target: str,
    *,
    event_bus: EventBus | None = None,
    payload: Mapping[str, object] | None = None,
) -> Run:
    """Update a user-owned run and append its transition event atomically."""
    run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
    if run is None:
        raise LookupError("Run not found.")
    bus = event_bus or EventBus()
    event = await stage_transition(session, user_id, run, target, event_bus=bus, payload=payload)
    await session.commit()
    await session.refresh(run)
    await session.refresh(event)
    await bus.publish(event)
    return run


async def resume_run(
    session: AsyncSession,
    user_id: UUID,
    run_id: UUID,
    *,
    event_bus: EventBus | None = None,
) -> Run:
    """Resume a paused run to the state it had before the pause."""
    run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
    if run is None:
        raise LookupError("Run not found.")
    if run.status != "PAUSED" or run.paused_from_status is None:
        raise IllegalRunTransition("Run is not paused with a resumable state.")
    target = run.paused_from_status
    bus = event_bus or EventBus()
    event = await stage_transition(session, user_id, run, target, event_bus=bus)
    await session.commit()
    await session.refresh(run)
    await session.refresh(event)
    await bus.publish(event)
    return run
