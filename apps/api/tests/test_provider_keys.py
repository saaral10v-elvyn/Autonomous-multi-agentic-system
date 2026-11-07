"""Tenant isolation and credential leak tests against PostgreSQL."""

from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.llm.fake import FakeProvider
from app.llm.types import LLMResponse, Provider
from tests.conftest import WEB_ORIGIN


def register(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "safe-password-1"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201


def test_cross_tenant_provider_key_access_fails(
    api_client: TestClient,
    test_emails: Callable[[], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-known-secret-never-expose-7Qz"
    first_user = test_emails()
    second_user = test_emails()
    register(api_client, first_user)
    created = api_client.post(
        "/api/v1/provider-keys",
        json={"provider": "openai", "label": "Personal", "api_key": secret},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    assert secret not in created.text
    assert created.json()["last4"] == secret[-4:]
    key_id = created.json()["id"]
    assert secret not in api_client.get("/api/v1/provider-keys").text

    register(api_client, second_user)
    assert api_client.get("/api/v1/provider-keys").json() == []
    assert (
        api_client.delete(
            f"/api/v1/provider-keys/{key_id}", headers={"Origin": WEB_ORIGIN}
        ).status_code
        == 404
    )
    assert (
        api_client.post(
            f"/api/v1/provider-keys/{key_id}/test", headers={"Origin": WEB_ORIGIN}
        ).status_code
        == 404
    )

    api_client.post("/api/v1/auth/logout", headers={"Origin": WEB_ORIGIN})
    invalid = api_client.post(
        "/api/v1/provider-keys",
        json={"provider": "openai", "label": "bad", "api_key": secret},
        headers={"Origin": WEB_ORIGIN},
    )
    assert invalid.status_code == 401
    assert secret not in invalid.text
    assert all(secret not in record.getMessage() for record in caplog.records)


def test_secret_leak_known_provider_key_never_in_responses_logs_or_errors(
    api_client: TestClient,
    test_emails: Callable[[], str],
    fake_provider: FakeProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-known-gateway-secret-3Yq"
    register(api_client, test_emails())
    response = LLMResponse(
        provider=Provider.OPENAI,
        model="openai/gpt-6-luna",
        content="OK",
        tokens_in=3,
        tokens_out=1,
        cost_usd=Decimal("0.0001"),
    )
    fake_provider.responses.append(response)
    created = api_client.post(
        "/api/v1/provider-keys",
        json={"provider": "openai", "label": "Gateway", "api_key": secret},
        headers={"Origin": WEB_ORIGIN},
    )
    tested = api_client.post(
        f"/api/v1/provider-keys/{created.json()['id']}/test", headers={"Origin": WEB_ORIGIN}
    )
    assert tested.status_code == 200
    assert tested.json()["ok"] is True
    assert secret not in tested.text
    assert fake_provider.received_api_keys == [secret]
    assert all(secret not in str(message) for message in fake_provider.messages)
    assert all(secret not in record.getMessage() for record in caplog.records)

    fake_provider.responses.append(RuntimeError(f"provider rejected {secret}"))
    failed_test = api_client.post(
        f"/api/v1/provider-keys/{created.json()['id']}/test", headers={"Origin": WEB_ORIGIN}
    )
    assert failed_test.status_code == 502
    assert secret not in failed_test.text
    assert all(secret not in record.getMessage() for record in caplog.records)
