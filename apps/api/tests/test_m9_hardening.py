"""M9 rate, concurrency, telemetry configuration, and billing groundwork checks."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

import app.api.v1.deps as deps_module
import app.main as main_module
from app.core.config import get_settings


def test_active_run_limit_is_enforced_per_user(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_PER_USER", "1")
    get_settings.cache_clear()
    email = test_emails()
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "long-test-password"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert signup.status_code == 201
    headers = {"Origin": "http://localhost:3000"}
    payload = {
        "idea": "M9 concurrency limit test",
        "autonomy_level": "step_by_step",
        "budget_usd": 1,
    }
    assert api_client.post("/api/v1/runs", json=payload, headers=headers).status_code == 201
    limited = api_client.post("/api/v1/runs", json=payload, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Concurrent run limit reached."


def test_simultaneous_run_requests_cannot_exceed_user_limit(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_PER_USER", "1")
    get_settings.cache_clear()
    response = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "long-test-password"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 201
    payload = {
        "idea": "Simultaneous M9 run limit test",
        "autonomy_level": "step_by_step",
        "budget_usd": 1,
    }

    def create_run(_: int) -> int:
        return api_client.post(
            "/api/v1/runs", json=payload, headers={"Origin": "http://localhost:3000"}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(create_run, range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(429) == 7


def test_authenticated_rate_limiter_fails_closed_when_redis_is_unavailable(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "long-test-password"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 201

    def unavailable_redis():
        raise RedisConnectionError("unavailable")

    monkeypatch.setattr(deps_module, "get_redis", unavailable_redis)
    monkeypatch.setattr(main_module, "get_redis", unavailable_redis)
    limited = api_client.get("/api/v1/me")
    assert limited.status_code == 503
    assert limited.json() == {"detail": "Rate limiting is temporarily unavailable."}


def test_platform_paid_mode_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_PAID_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().platform_paid_enabled is False
    get_settings.cache_clear()


def test_billing_groundwork_is_user_owned_and_empty() -> None:
    from app.models import CreditLedger, Subscription

    assert CreditLedger.__table__.c.user_id.foreign_keys
    assert Subscription.__table__.c.user_id.foreign_keys
