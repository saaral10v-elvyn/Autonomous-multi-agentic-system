"""Small injectable JSON HTTP transport; errors never include headers or bodies."""

import asyncio
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


async def request_json(
    url: str, token: str, method: str, payload: dict[str, object] | None = None
) -> Any:
    def send() -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "Forge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                value = response.read(2_000_000)
                return json.loads(value) if value else {}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError("External integration request failed.") from None

    return await asyncio.to_thread(send)
