"""Sanitized gateway errors."""


class ProviderError(Exception):
    """A provider adapter failure safe to pass to gateway retry logic."""

    def __init__(
        self, message: str = "Provider request failed.", *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMRequestError(RuntimeError):
    """A sanitized failure returned to application services."""


class ProviderKeyUnavailable(RuntimeError):
    """The requesting user does not own the selected provider key."""
