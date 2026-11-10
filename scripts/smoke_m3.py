"""Exercise the M3 Clarifier/Planner flow using only the local FakeProvider."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

API_PORT = int(os.environ.setdefault("FORGE_SMOKE_API_PORT", "8000"))
WEB_PORT = int(os.environ.setdefault("FORGE_SMOKE_WEB_PORT", "3000"))
POSTGRES_PORT = int(os.environ.setdefault("FORGE_SMOKE_POSTGRES_PORT", "15432"))
REDIS_PORT = int(os.environ.setdefault("FORGE_SMOKE_REDIS_PORT", "16379"))
API_URL = f"http://127.0.0.1:{API_PORT}"
WEB_URL = f"http://127.0.0.1:{WEB_PORT}"
ORIGIN = f"http://localhost:{WEB_PORT}"
os.environ.setdefault(
    "DATABASE_URL",
    f"postgresql+asyncpg://forge:forge@127.0.0.1:{POSTGRES_PORT}/forge",
)
os.environ.setdefault("REDIS_URL", f"redis://127.0.0.1:{REDIS_PORT}/0")
os.environ.setdefault("WEB_ORIGIN", ORIGIN)
os.environ.setdefault("NEXT_PUBLIC_API_URL", API_URL)

from app.services.orchestration import enqueue_advance  # noqa: E402
from smoke_m2 import start_process, stop_all, stop_process, wait_http  # noqa: E402


PASSWORD = "M3SmokeTestPassword-2026"


def set_fake_mode(enabled: bool, *, step_delay: float = 0) -> None:
    os.environ["APP_ENV"] = "development"
    os.environ["FAKE_PROVIDER_ENABLED"] = str(enabled).lower()
    os.environ["FAKE_PROVIDER_STEP_DELAY_SECONDS"] = str(step_delay)


def start_api() -> Any:
    return start_process(
        "api-m3",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        API_DIR,
    )


def start_worker() -> Any:
    return start_process(
        "worker-m3",
        [sys.executable, "-m", "arq", "app.worker.WorkerSettings"],
        API_DIR,
    )


async def wait_events(
    client: httpx.AsyncClient,
    run_id: str,
    predicate: Any,
    *,
    timeout: float = 45,
) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/v1/runs/{run_id}/events?after_seq=0")
        if response.status_code != 200:
            raise AssertionError(f"Could not read run events: HTTP {response.status_code}")
        events = response.json()
        if predicate(events):
            return events
        await asyncio.sleep(0.15)
    raise TimeoutError(f"Run {run_id} did not reach the expected event state.")


async def create_user(client: httpx.AsyncClient, label: str) -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": f"m3-smoke-{label}-{os.urandom(5).hex()}@example.test", "password": PASSWORD},
    )
    if response.status_code != 201:
        raise AssertionError(f"Signup {label} failed: HTTP {response.status_code} {response.text}")


async def create_run(
    client: httpx.AsyncClient, *, autonomy: str = "step_by_step"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/runs",
        json={
            "idea": "Build a small journal for a community team",
            "autonomy_level": autonomy,
            "budget_usd": "10.00",
        },
    )
    if response.status_code != 201:
        raise AssertionError(f"Run creation failed: HTTP {response.status_code} {response.text}")
    return response.json()


def has_event(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(event.get("type") == event_type for event in events)


def has_state(events: list[dict[str, Any]], state: str) -> bool:
    return any(
        event.get("type") == "run.state_changed"
        and event.get("payload", {}).get("status") == state
        for event in events
    )


async def finish_answers(client: httpx.AsyncClient, run_id: str) -> list[dict[str, Any]]:
    events = await wait_events(client, run_id, lambda rows: has_event(rows, "question.asked"))
    question_event = next(row for row in events if row["type"] == "question.asked")
    question = question_event["payload"]["questions"][0]
    answer = await client.post(
        f"/api/v1/runs/{run_id}/answers",
        json={"answers": {question["id"]: "decide_for_me"}},
    )
    if answer.status_code != 200:
        raise AssertionError(f"Answer submission failed: HTTP {answer.status_code} {answer.text}")
    return events


def event_identity(event: dict[str, Any]) -> tuple[str, str]:
    """Build a stable identity for detecting duplicate persisted events."""
    event_type = str(event.get("type", ""))
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return event_type, json.dumps(payload, sort_keys=True)
    if event_type == "agent.step_completed":
        key = f"{payload.get('agent_id')}:{payload.get('idx')}"
    else:
        key_value = next(
            (
                payload[field]
                for field in ("question_id", "artifact_id", "approval_id", "status")
                if field in payload
            ),
            None,
        )
        key = str(key_value) if key_value is not None else json.dumps(payload, sort_keys=True)
    return event_type, key


async def smoke() -> None:
    (ROOT / ".run").mkdir(exist_ok=True)
    set_fake_mode(True, step_delay=10)
    api = start_api()
    worker = start_worker()
    web = start_process(
        "web-m3",
        ["pnpm.cmd" if os.name == "nt" else "pnpm", "dev", "--", "--port", str(WEB_PORT)],
        WEB_DIR,
    )
    try:
        await wait_http(f"{API_URL}/healthz", api)
        await wait_http(WEB_URL, web)
        async with httpx.AsyncClient(
            base_url=API_URL,
            headers={"Origin": ORIGIN},
            timeout=20,
        ) as client:
            await create_user(client, "recovery")
            run = await create_run(client)
            run_id = run["id"]
            events = await finish_answers(client, run_id)
            if not has_state(events, "AWAITING_ANSWERS"):
                raise AssertionError("Clarifier did not move the run to AWAITING_ANSWERS")
            print("PASS idea -> question event -> answer submission", flush=True)

            events = await wait_events(
                client,
                run_id,
                lambda rows: any(
                    row.get("type") == "agent.step_completed"
                    and row.get("payload", {}).get("role") == "planner"
                    for row in rows
                ),
            )
            planner_steps_before = sum(
                row.get("type") == "agent.step_completed"
                and row.get("payload", {}).get("role") == "planner"
                for row in events
            )
            if worker.poll() is not None:
                raise RuntimeError(
                    f"Worker exited before restart injection: code={worker.returncode}"
                )
            stop_process(worker)
            if worker.poll() is None:
                raise RuntimeError("The original worker process was not stopped.")
            print("PASS worker stopped after the planner step was durably recorded", flush=True)
            worker = start_worker()
            await asyncio.sleep(0.4)
            user_response = await client.get("/api/v1/me")
            if user_response.status_code != 200:
                raise AssertionError("Could not resolve the smoke-test user after worker restart")
            user_id = UUID(user_response.json()["id"])
            # The worker restart may not reclaim the old in-progress job until its lease expires.
            # Enqueue the same run explicitly; persisted steps make both deliveries idempotent.
            await enqueue_advance(user_id, UUID(run_id))
            events = await wait_events(client, run_id, lambda rows: has_state(rows, "AWAITING_PLAN_APPROVAL"))
            planner_steps_after = sum(
                row.get("type") == "agent.step_completed"
                and row.get("payload", {}).get("role") == "planner"
                for row in events
            )
            if planner_steps_after != 1 or planner_steps_after != planner_steps_before:
                raise AssertionError(
                    "Worker restart duplicated or lost the persisted planner step: "
                    f"before={planner_steps_before}, after={planner_steps_after}"
                )
            if not has_event(events, "plan.created"):
                raise AssertionError("Restart duplicated a planner step or failed to persist the plan")
            identities = [event_identity(row) for row in events]
            if len(identities) != len(set(identities)):
                raise AssertionError("Restart produced duplicate (type, key) events")
            sequences = [row["seq"] for row in events]
            if sequences != list(range(1, len(sequences) + 1)):
                raise AssertionError(f"Restart produced non-contiguous event sequences: {sequences}")
            approved = await client.post(f"/api/v1/runs/{run_id}/plan/approve")
            if approved.status_code != 200 or approved.json()["status"] != "EXECUTING":
                raise AssertionError("Plan approval did not move the run to EXECUTING")
            print("PASS worker restart resumed plan without duplicate steps or events", flush=True)

            autopilot = await create_run(client, autonomy="autopilot")
            auto_id = autopilot["id"]
            await finish_answers(client, auto_id)
            auto_events = await wait_events(
                client, auto_id, lambda rows: has_state(rows, "EXECUTING")
            )
            auto_index = next(i for i, row in enumerate(auto_events) if row["type"] == "plan.auto_approved")
            exec_index = next(
                i for i, row in enumerate(auto_events)
                if row["type"] == "run.state_changed" and row["payload"].get("status") == "EXECUTING"
            )
            if auto_index >= exec_index:
                raise AssertionError("Autopilot did not record auto-approval before EXECUTING")
            print("PASS autopilot auto-approved its plan and reached EXECUTING", flush=True)

            stop_process(worker)
            stop_process(api)
            set_fake_mode(False)
            api = start_api()
            worker = start_worker()
            await wait_http(f"{API_URL}/healthz", api)
            await create_user(client, "missing-key")
            missing_key_run = await create_run(client)
            failed_events = await wait_events(
                client,
                missing_key_run["id"],
                lambda rows: has_event(rows, "run.failed"),
            )
            failure = next(row for row in failed_events if row["type"] == "run.failed")
            if "No provider key is configured" not in failure["payload"].get("message", ""):
                raise AssertionError("Missing-key failure did not include the expected safe message")
            print("PASS fake mode off + missing key produced a clear FAILED event", flush=True)

            web_response = await client.get(WEB_URL)
            if web_response.status_code != 200:
                raise AssertionError(f"Web landing returned HTTP {web_response.status_code}")
            print("PASS web landing returned HTTP 200", flush=True)
        print("SMOKE M3: ALL CHECKS PASSED", flush=True)
    finally:
        stop_all()
        print("STOPPED all M3 smoke test processes", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(smoke())
    except Exception as error:
        print(f"SMOKE M3: FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        raise
