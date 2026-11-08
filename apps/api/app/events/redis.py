"""Shared Redis connection used for run event notifications."""

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis[str] | None = None


def get_redis() -> Redis[str]:
    """Return the process-wide async Redis client."""
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Close Redis resources during API shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
