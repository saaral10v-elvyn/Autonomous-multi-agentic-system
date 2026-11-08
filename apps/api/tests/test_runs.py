"""Postgres-backed M2 run API and event allocation tests."""

import asyncio
from collections.abc import Callable
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.models.event import Event
from tests.conftest import WEB_ORIGIN


def signup(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "m2-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201


def test_run_creation_history_and_event_replay(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    created = api_client.post(
        "/api/v1/runs",
        json={
            "idea": "  Build a simple notes app  ",
            "autonomy_level": "milestones",
            "budget_usd": "25.00",
        },
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run = created.json()
    assert run["idea"] == "Build a simple notes app"
    assert run["status"] == "CREATED"
    assert run["budget_usd"] == "25.00"
    assert api_client.get("/api/v1/runs").json()[0]["id"] == run["id"]
    assert api_client.get(f"/api/v1/runs/{run['id']}").json()["id"] == run["id"]
    events = api_client.get(f"/api/v1/runs/{run['id']}/events?after_seq=0").json()
    assert [(event["seq"], event["type"]) for event in events] == [(1, "run.state_changed")]
    assert events[0]["payload"]["status"] == "CREATED"


def test_run_validation_rejects_invalid_autonomy_and_negative_budget(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    headers = {"Origin": WEB_ORIGIN}
    invalid_level = api_client.post(
        "/api/v1/runs",
        json={"idea": "A valid idea", "autonomy_level": "hands_off", "budget_usd": 2},
        headers=headers,
    )
    negative_budget = api_client.post(
        "/api/v1/runs",
        json={"idea": "A valid idea", "autonomy_level": "autopilot", "budget_usd": -1},
        headers=headers,
    )
    assert invalid_level.status_code == 422
    assert negative_budget.status_code == 422


def test_event_sequences_are_contiguous_under_concurrent_appends(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    signup(api_client, test_emails())
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Concurrent event test", "autonomy_level": "autopilot", "budget_usd": 0},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])
    user_id = UUID(api_client.get("/api/v1/me").json()["id"])

    async def append_and_read() -> list[int]:
        bus = EventBus()
        await asyncio.gather(
            *(
                bus.append(
                    user_id,
                    run_id,
                    event_type="test.concurrent",
                    payload={"index": index},
                )
                for index in range(32)
            )
        )
        async with SessionFactory() as session:
            rows = (
                await session.scalars(
                    select(Event.seq).where(Event.run_id == run_id).order_by(Event.seq)
                )
            ).all()
        return list(rows)

    sequences = api_client.portal.call(append_and_read)
    assert sequences == list(range(1, 34))
