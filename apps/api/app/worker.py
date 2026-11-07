"""Arq worker scaffold with a no-op job."""

from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings


async def noop_job(_: Any, message: str = "") -> str:
    """Return successfully without side effects as a worker smoke job."""
    return message


class WorkerSettings:
    """Arq worker configuration."""

    functions = [noop_job]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
