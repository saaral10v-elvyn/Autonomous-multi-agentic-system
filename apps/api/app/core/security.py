"""Signed cookie sessions and origin-based CSRF enforcement."""

import base64
import hashlib
import hmac
import json
import time
from uuid import UUID

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.crypto import decode_master_key, derive_subkey

SESSION_COOKIE = "forge_session"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session_token(user_id: UUID, session_version: int) -> str:
    """Create a time-limited, signed session token with purpose-derived signing material."""
    settings = get_settings()
    now = int(time.time())
    payload = json.dumps(
        {
            "sub": str(user_id),
            "ver": session_version,
            "iat": now,
            "exp": now + settings.session_max_age_seconds,
        },
        separators=(",", ":"),
    ).encode()
    encoded = _b64encode(payload)
    signing_key = derive_subkey(decode_master_key(), "session-cookie")
    signature = hmac.new(signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_session_token(token: str | None) -> tuple[UUID, int] | None:
    """Validate a signed token and return its subject and revocation version."""
    if not token:
        return None
    try:
        encoded, signature_text = token.split(".", maxsplit=1)
        signing_key = derive_subkey(decode_master_key(), "session-cookie")
        expected = hmac.new(signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_text)):
            return None
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or int(payload["exp"]) < int(time.time()):
            return None
        session_version = int(payload["ver"])
        if session_version < 0:
            return None
        return UUID(str(payload["sub"])), session_version
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def set_session_cookie(response: Response, user_id: UUID, session_version: int) -> None:
    """Set the signed HttpOnly session cookie with the specified SameSite policy."""
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user_id, session_version),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie from the browser."""
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=get_settings().cookie_secure, samesite="lax"
    )


class OriginCSRFMiddleware(BaseHTTPMiddleware):
    """Reject mutating API requests whose Origin differs from the configured web origin."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/v1/") and request.method.upper() in _UNSAFE_METHODS:
            origin = request.headers.get("origin", "").rstrip("/")
            expected_origin = get_settings().web_origin.rstrip("/")
            if not origin or not hmac.compare_digest(origin, expected_origin):
                return JSONResponse({"detail": "Cross-site request rejected."}, status_code=403)
        return await call_next(request)
