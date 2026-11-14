"""Arq worker scaffold with a no-op job."""

from typing import Any
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings

from app.api.v1.deps import get_llm_client
from app.core.config import get_settings
from app.services.approvals import expire_pending_approvals
from app.services.orchestration import advance_run_job, resume_approval_job


async def noop_job(_: Any, message: str = "") -> str:
    """Return successfully without side effects as a worker smoke job."""
    return message


async def advance_run(_: Any, user_id: str, run_id: str) -> str:
    """Advance one user-owned run until it needs an answer or approval."""
    return await advance_run_job(UUID(user_id), UUID(run_id), get_llm_client())


async def resume_approval(_: Any, user_id: str, run_id: str, approval_id: str) -> str:
    """Resume the durable tool action linked to a resolved approval."""
    result = await resume_approval_job(
        UUID(user_id), UUID(run_id), UUID(approval_id), get_llm_client()
    )
    return result.status


async def expire_approvals(ctx: dict[Any, Any], *args: Any, **kwargs: Any) -> int:
    """Resolve approvals that exceeded the configured human-response TTL."""
    del ctx, args, kwargs
    return await expire_pending_approvals()


class WorkerSettings:
    """Arq worker configuration."""

    functions = [noop_job, advance_run, resume_approval]
    cron_jobs = [cron(expire_approvals, minute=set(range(60)))]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
