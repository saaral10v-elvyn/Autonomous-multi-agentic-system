"""Best-effort terminal cleanup; periodic TTL cleanup is the fallback."""

from uuid import UUID

import structlog

from app.sandbox.factory import get_sandbox_provider

logger = structlog.get_logger(__name__)


async def destroy_run_sandbox(run_id: UUID) -> None:
    try:
        provider = get_sandbox_provider()
        handles = await provider.list_active()
    except Exception as error:
        logger.warning(
            "sandbox.cleanup_lookup_failed", run_id=str(run_id), error_type=type(error).__name__
        )
        return
    for handle in handles:
        if handle.run_id == str(run_id):
            try:
                await provider.destroy(handle)
            except Exception as error:
                logger.warning(
                    "sandbox.cleanup_destroy_failed",
                    run_id=str(run_id),
                    error_type=type(error).__name__,
                )
