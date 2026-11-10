"""Safe formatting for request and WebSocket validation errors."""

from collections.abc import Mapping, Sequence
from typing import Any


def validation_messages(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    """Build readable field messages without returning submitted input values."""
    messages: list[dict[str, object]] = []
    for item in errors:
        raw_message = item.get("msg")
        message = raw_message if isinstance(raw_message, str) else "The submitted data is invalid."
        location = item.get("loc")
        parts = location if isinstance(location, (tuple, list)) else ()
        path = ".".join(str(part) for part in parts)
        cleaned = message.removeprefix("Value error, ")
        messages.append(
            {
                "loc": list(parts),
                "msg": f"{path}: {cleaned}" if path else cleaned,
                "type": "value_error",
            }
        )
    return messages


def validation_message(error: Any) -> str:
    """Format Pydantic validation errors for WebSocket rejection messages."""
    messages = validation_messages(error.errors())
    return "; ".join(str(item["msg"]) for item in messages) or "The submitted data is invalid."
