"""Exercise the M2 HTTP, WebSocket, restart, and web-server smoke paths."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"
RUN_DIR = ROOT / ".run"
sys.path.insert(0, str(API_DIR))

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.events.bus import EventBus


API_URL = "http://127.0.0.1:8000"
WEB_URL = "http://127.0.0.1:3000"
ORIGIN = "http://localhost:3000"
PASSWORD = "SmokeTestPassword-2026"
PROCESSES: list[tuple[subprocess.Popen[bytes], Any]] = []


def start_process(name: str, command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    log = (RUN_DIR / f"smoke-m2-{name}.log").open("ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    PROCESSES.append((process, log))
    print(f"START {name}: pid={process.pid}", flush=True)
    return process


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_all() -> None:
    for process, log in reversed(PROCESSES):
        stop_process(process)
        log.close()
    PROCESSES.clear()


async def wait_http(url: str, process: subprocess.Popen[bytes]) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(90):
            if process.poll() is not None:
                raise RuntimeError(f"Server exited with code {process.returncode}: {url}")
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise TimeoutError(f"Server did not become ready: {url}")


def assert_status(response: httpx.Response, expected: int, label: str) -> None:
    if response.status_code != expected:
        raise AssertionError(
            f"{label}: expected HTTP {expected}, got {response.status_code}: {response.text}"
        )


async def expect_ws_rejected(url: str, *, cookie: str | None, origin: str | None) -> None:
    headers = {"Cookie": f"forge_session={cookie}"} if cookie else None
    try:
        async with connect(
            url,
            origin=origin,
            additional_headers=headers,
            open_timeout=5,
        ):
            raise AssertionError(f"WebSocket unexpectedly accepted: origin={origin!r}")
    except InvalidStatus as error:
        if error.response.status_code not in (403, 4401, 4403):
            raise AssertionError(
                f"Unexpected WebSocket rejection status {error.response.status_code}"
            ) from error
    except ConnectionClosed as error:
        if error.code not in (4401, 4403):
            raise AssertionError(f"Unexpected WebSocket close code {error.code}") from error


async def smoke() -> None:
    RUN_DIR.mkdir(exist_ok=True)
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Missing root .env; the API requires the configured MASTER_KEY.")

    api_command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1"]
    worker_command = [sys.executable, "-m", "arq", "app.worker.WorkerSettings"]
    api = start_process("api", api_command, API_DIR)
    worker = start_process("worker", worker_command, API_DIR)
    web = start_process("web", ["pnpm.cmd" if os.name == "nt" else "pnpm", "dev"], WEB_DIR)

    try:
        await wait_http(f"{API_URL}/healthz", api)
        print("PASS API started and /healthz returned 200", flush=True)
        await wait_http(WEB_URL, web)
        print("PASS web server returned HTTP 200", flush=True)
        await asyncio.sleep(2)
        if worker.poll() is not None:
            raise RuntimeError(f"Worker exited with code {worker.returncode}")
        worker_log = (RUN_DIR / "smoke-m2-worker.log").read_text(errors="replace")
        if "Starting worker" not in worker_log:
            raise RuntimeError("Arq worker did not report that it started; inspect .run log.")
        print("PASS Arq worker started", flush=True)

        run_marker = uuid.uuid4().hex
        async with (
            httpx.AsyncClient(
                base_url=API_URL,
                headers={"Origin": ORIGIN},
                timeout=15,
            ) as user_a,
            httpx.AsyncClient(
                base_url=API_URL,
                headers={"Origin": ORIGIN},
                timeout=15,
            ) as user_b,
        ):
            signup_a = await user_a.post(
                "/api/v1/auth/signup",
                json={"email": f"smoke-a-{run_marker}@example.test", "password": PASSWORD},
            )
            assert_status(signup_a, 201, "signup user A")
            signup_b = await user_b.post(
                "/api/v1/auth/signup",
                json={"email": f"smoke-b-{run_marker}@example.test", "password": PASSWORD},
            )
            assert_status(signup_b, 201, "signup user B")
            print("PASS two users signed up", flush=True)

            session_a = user_a.cookies.get("forge_session")
            session_b = user_b.cookies.get("forge_session")
            if not session_a or not session_b:
                raise AssertionError("Signup did not set both session cookies")

            login_a = await user_a.post(
                "/api/v1/auth/login",
                json={"email": signup_a.json()["email"], "password": PASSWORD},
            )
            assert_status(login_a, 200, "login user A")
            login_b = await user_b.post(
                "/api/v1/auth/login",
                json={"email": signup_b.json()["email"], "password": PASSWORD},
            )
            assert_status(login_b, 200, "login user B")
            session_a = user_a.cookies.get("forge_session") or session_a
            session_b = user_b.cookies.get("forge_session") or session_b
            print("PASS both users logged in", flush=True)

            created = await user_a.post(
                "/api/v1/runs",
                json={
                    "idea": f"M2 live smoke {run_marker}",
                    "autonomy_level": "step_by_step",
                    "budget_usd": "1.00",
                },
            )
            assert_status(created, 201, "create run")
            run_id = created.json()["id"]
            ws_url = f"ws://127.0.0.1:8000/ws/runs/{run_id}?after_seq=0"
            event_url = f"/api/v1/runs/{run_id}/events?after_seq=0"

            async with connect(
                ws_url,
                origin=ORIGIN,
                additional_headers={"Cookie": f"forge_session={session_a}"},
                open_timeout=5,
            ) as socket:
                first = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                if (
                    first.get("seq") != 1
                    or first.get("type") != "run.state_changed"
                    or first.get("payload", {}).get("status") != "CREATED"
                ):
                    raise AssertionError(f"Unexpected initial WebSocket event: {first}")
            print("PASS user A created a run and received CREATED event seq=1", flush=True)

            events_a = await user_a.get(event_url)
            assert_status(events_a, 200, "user A read events")
            run_a = await user_a.get(f"/api/v1/runs/{run_id}")
            assert_status(run_a, 200, "user A read run")
            events_b = await user_b.get(event_url)
            assert_status(events_b, 404, "user B cross-tenant events")
            run_b = await user_b.get(f"/api/v1/runs/{run_id}")
            assert_status(run_b, 404, "user B cross-tenant run")
            print("PASS cross-tenant run and event reads were denied", flush=True)

            await expect_ws_rejected(ws_url, cookie=session_b, origin=ORIGIN)
            print("PASS user B WebSocket connection was denied", flush=True)
            await expect_ws_rejected(ws_url, cookie=None, origin=ORIGIN)
            print("PASS unauthenticated WebSocket connection was denied", flush=True)
            await expect_ws_rejected(
                ws_url, cookie=session_a, origin="http://invalid-origin.example"
            )
            print("PASS mismatched-Origin WebSocket connection was denied", flush=True)

            stop_process(api)
            print("RESTART API: stopped original process", flush=True)
            api = start_process("api-restarted", api_command, API_DIR)
            await wait_http(f"{API_URL}/healthz", api)

            user_id = uuid.UUID(signup_a.json()["id"])
            second = await EventBus().append(
                user_id,
                uuid.UUID(run_id),
                event_type="smoke.restart_probe",
                payload={"probe": run_marker},
            )
            if second is None or second.seq != 2:
                raise AssertionError("Restart probe did not receive seq=2")

            ws_url_after_seq = f"ws://127.0.0.1:8000/ws/runs/{run_id}?after_seq=1"
            async with connect(
                ws_url_after_seq,
                origin=ORIGIN,
                additional_headers={"Cookie": f"forge_session={session_a}"},
                open_timeout=5,
            ) as socket:
                replayed = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                if replayed.get("seq") != 2 or replayed.get("type") != "smoke.restart_probe":
                    raise AssertionError(f"Unexpected replay after restart: {replayed}")
                third = await EventBus().append(
                    user_id,
                    uuid.UUID(run_id),
                    event_type="smoke.live_probe",
                    payload={"probe": run_marker},
                )
                if third is None or third.seq != 3:
                    raise AssertionError("Live probe did not receive seq=3")
                live = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                if live.get("seq") != 3 or live.get("type") != "smoke.live_probe":
                    raise AssertionError(f"Unexpected live event: {live}")
                try:
                    duplicate = await asyncio.wait_for(socket.recv(), timeout=0.7)
                except TimeoutError:
                    duplicate = None
                if duplicate is not None:
                    raise AssertionError(f"Unexpected duplicate event after reconnect: {duplicate}")

            final_events = await user_a.get(f"/api/v1/runs/{run_id}/events?after_seq=0")
            assert_status(final_events, 200, "final event replay")
            sequences = [event["seq"] for event in final_events.json()]
            if sequences != [1, 2, 3]:
                raise AssertionError(f"Expected contiguous, unique events [1, 2, 3], got {sequences}")
            print("PASS API restart + after_seq replay had no gaps or duplicate events", flush=True)

        print("SMOKE M2: ALL CHECKS PASSED", flush=True)
    finally:
        stop_all()
        print("STOPPED all smoke test processes", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(smoke())
    except Exception as error:
        print(f"SMOKE M2: FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        raise
