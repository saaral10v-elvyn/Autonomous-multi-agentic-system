"""Concurrent authenticated API load probe for a staging/target VPS.

Create a dedicated test account first, then pass its credentials in environment variables.
This script creates runs and therefore must never target production data.
"""

import concurrent.futures
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("FORGE_LOAD_BASE_URL")
EMAIL = os.environ["FORGE_LOAD_EMAIL"]
PASSWORD = os.environ["FORGE_LOAD_PASSWORD"]
CONCURRENCY = int(os.environ.get("FORGE_LOAD_CONCURRENCY", "10"))
if not BASE_URL:
    raise SystemExit("FORGE_LOAD_BASE_URL must explicitly point to a disposable staging target.")
if os.environ.get("FORGE_LOAD_CONFIRM_STAGING") != "I_CONFIRMED_DISPOSABLE_STAGING":
    raise SystemExit(
        "Set FORGE_LOAD_CONFIRM_STAGING=I_CONFIRMED_DISPOSABLE_STAGING after verifying the target."
    )
if CONCURRENCY <= 0:
    raise SystemExit("FORGE_LOAD_CONCURRENCY must be a positive integer.")


def post(
    url: str, payload: dict[str, object], opener: urllib.request.OpenerDirector
) -> tuple[int, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": os.environ.get("WEB_ORIGIN", "http://localhost:3000"),
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as error:
        return error.code, (time.perf_counter() - started) * 1000


def main() -> None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login, _ = post(
        f"{BASE_URL}/api/v1/auth/login",
        {"email": EMAIL, "password": PASSWORD},
        opener,
    )
    if login not in {200, 204}:
        raise SystemExit(f"Login failed with HTTP {login}.")
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(
            pool.map(
                lambda index: post(
                    f"{BASE_URL}/api/v1/runs",
                    {
                        "idea": f"M9 staging load probe {index}",
                        "autonomy_level": "step_by_step",
                        "budget_usd": 0,
                    },
                    opener,
                ),
                range(CONCURRENCY),
            )
        )
    elapsed = time.perf_counter() - started
    statuses = [status for status, _ in results]
    latencies = sorted(latency for _, latency in results)

    def percentile(fraction: float) -> float:
        return latencies[min(len(latencies) - 1, int((len(latencies) - 1) * fraction))]

    counts = {str(code): statuses.count(code) for code in sorted(set(statuses))}
    print(
        json.dumps(
            {
                "concurrency": CONCURRENCY,
                "elapsed_seconds": round(elapsed, 3),
                "http_status_counts": counts,
                "latency_ms": {
                    "p50": round(percentile(0.50), 1),
                    "p95": round(percentile(0.95), 1),
                    "p99": round(percentile(0.99), 1),
                },
            }
        )
    )


if __name__ == "__main__":
    main()
