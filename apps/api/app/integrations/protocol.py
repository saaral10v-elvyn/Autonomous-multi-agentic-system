"""Provider-neutral contract used by external service adapters."""

from collections.abc import Mapping
from typing import Protocol


class Integration(Protocol):
    kind: str

    async def execute(self, action: str, payload: Mapping[str, object]) -> Mapping[str, object]: ...
