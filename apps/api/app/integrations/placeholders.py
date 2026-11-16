"""Adapter names reserved by the public Integration contract."""

from collections.abc import Mapping


class DeferredHostingAdapter:
    def __init__(self, kind: str) -> None:
        if kind not in {"netlify", "cloudflare_pages", "render"}:
            raise ValueError("Unsupported deferred hosting adapter.")
        self.kind = kind

    async def execute(self, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        del action, payload
        raise NotImplementedError(f"{self.kind} adapter is reserved for a later milestone.")
