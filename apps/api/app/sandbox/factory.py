"""Guarded provider factory and configured sandbox limits."""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.sandbox.base import SandboxProvider, SandboxSpec
from app.sandbox.docker_provider import DockerSandboxProvider
from app.sandbox.fake_provider import FakeSandboxProvider


@lru_cache
def get_sandbox_provider() -> SandboxProvider:
    settings = get_settings()
    if settings.sandbox_provider == "fake":
        if settings.app_env == "production":
            raise ValueError("Fake sandbox provider is disabled in production.")
        return FakeSandboxProvider(max_output_bytes=settings.sandbox_max_output_bytes)
    return DockerSandboxProvider(settings.sandbox_max_output_bytes)


def sandbox_spec(
    settings: Settings | None = None, *, owner_user_id: str | None = None
) -> SandboxSpec:
    active = settings or get_settings()
    return SandboxSpec(
        image=active.sandbox_image,
        cpu=active.sandbox_cpu,
        memory_mb=active.sandbox_memory_mb,
        pids_limit=active.sandbox_pids_limit,
        disk_mb=active.sandbox_disk_mb,
        ttl_s=active.sandbox_ttl_seconds,
        network_mode=active.sandbox_network_mode,
        allowlist=active.sandbox_allowlist_hosts,
        owner_user_id=owner_user_id,
    )
