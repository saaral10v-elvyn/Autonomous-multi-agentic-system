"""Arq worker with telemetry and sandbox cleanup jobs."""

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings

from app.api.v1.deps import get_llm_client
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.core.telemetry import (
    shutdown_telemetry,
    tracer,
    worker_job_count,
    worker_job_duration,
)
from app.repos.runs import RunRepository
from app.sandbox.factory import get_sandbox_provider
from app.services.approvals import expire_pending_approvals
from app.services.orchestration import advance_run_job, resume_approval_job


async def noop_job(_: Any, message: str = "") -> str:
    """Return successfully without side effects as a worker smoke job."""
    return message


async def advance_run(_: Any, user_id: str, run_id: str) -> str:
    """Advance one user-owned run until it needs an answer or approval."""
    return await _observe_job(
        "advance_run",
        run_id,
        lambda: advance_run_job(UUID(user_id), UUID(run_id), get_llm_client()),
    )


async def resume_approval(_: Any, user_id: str, run_id: str, approval_id: str) -> str:
    """Resume the durable tool action linked to a resolved approval."""

    async def resume() -> str:
        result = await resume_approval_job(
            UUID(user_id), UUID(run_id), UUID(approval_id), get_llm_client()
        )
        return result.status

    return await _observe_job("resume_approval", run_id, resume)


async def _observe_job(name: str, run_id: str, job: Callable[[], Awaitable[str]]) -> str:
    started = perf_counter()
    status = "error"
    try:
        with tracer.start_as_current_span(f"worker.{name}") as span:
            span.set_attribute("forge.run_id", run_id)
            result = await job()
            status = "ok"
            return result
    finally:
        attrs = {"job": name, "status": status}
        worker_job_count.add(1, attrs)
        worker_job_duration.record((perf_counter() - started) * 1000, attrs)


async def expire_approvals(ctx: dict[Any, Any], *args: Any, **kwargs: Any) -> int:
    """Resolve approvals that exceeded the configured human-response TTL."""
    del ctx, args, kwargs
    return await expire_pending_approvals()


async def cleanup_sandboxes(ctx: dict[Any, Any], *args: Any, **kwargs: Any) -> int:
    del ctx, args, kwargs
    provider = get_sandbox_provider()
    cleaned = await provider.cleanup_expired()
    for handle in await provider.list_active():
        try:
            owner = UUID(handle.owner_user_id) if handle.owner_user_id else None
            run_id = UUID(handle.run_id)
        except ValueError:
            owner, run_id = None, UUID(int=0)
        run = None
        if owner is not None:
            async with SessionFactory() as session:
                run = await RunRepository(session).get_for_user(owner, run_id)
        if run is None or run.status in {"COMPLETED", "CANCELLED"}:
            await provider.destroy(handle)
            cleaned += 1
    return cleaned


async def shutdown_worker(ctx: Any) -> None:
    """Flush telemetry after the Arq process stops accepting work."""
    del ctx
    shutdown_telemetry()


class WorkerSettings:
    """Arq worker configuration."""

    functions = [noop_job, advance_run, resume_approval]
    cron_jobs = [
        cron(expire_approvals, minute=set(range(60))),
        cron(cleanup_sandboxes, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]
    on_shutdown = shutdown_worker
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
