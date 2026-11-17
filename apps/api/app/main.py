"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter, time

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import RedisError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api.v1.router import router as v1_router
from app.api.v1.websockets import router as websocket_router
from app.core.config import get_settings
from app.core.crypto import decode_master_key
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.security import OriginCSRFMiddleware
from app.core.telemetry import flush_telemetry, request_count, request_duration, tracer
from app.core.validation import validation_messages
from app.events.redis import close_redis, get_redis

configure_logging()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Validate key material, log lifecycle events, and release the database pool."""
    decode_master_key()
    logger.info("api.started")
    yield
    await engine.dispose()
    await close_redis()
    flush_telemetry()
    logger.info("api.stopped")


app = FastAPI(title="Forge API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def observe_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Record request spans and low-cardinality HTTP metrics without payload data."""
    started = perf_counter()
    status_code = 500
    with tracer.start_as_current_span("http.server.request") as span:
        span.set_attribute("http.request.method", request.method)
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            span.set_attribute("error", True)
            raise
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            attrs: dict[str, str | int] = {
                "http.request.method": request.method,
                "http.response.status_code": status_code,
            }
            request_count.add(1, attrs)
            request_duration.record(elapsed_ms, attrs)


@app.middleware("http")
async def limit_ip_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Protect unauthenticated endpoints with a Redis-backed client-IP bucket."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    settings = get_settings()
    client_host = request.client.host if request.client else "unknown"
    bucket = int(time()) // settings.api_rate_limit_window_seconds
    try:
        redis = get_redis()
        key = f"forge:ip-rate:{client_host}:{bucket}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.api_rate_limit_window_seconds + 1)
    except RedisError:
        return JSONResponse(
            {"detail": "Rate limiting is temporarily unavailable."}, status_code=503
        )
    if count > settings.api_rate_limit_requests * 4:
        return JSONResponse({"detail": "Request rate limit exceeded."}, status_code=429)
    return await call_next(request)


app.include_router(v1_router, prefix="/api/v1")
app.include_router(websocket_router)
app.add_middleware(OriginCSRFMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_origin],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    """Return validation locations without echoing submitted secret-bearing values."""
    is_run_route = request.url.path.startswith("/api/v1/runs/")
    safe_errors = (
        validation_messages(error.errors())
        if is_run_route
        else [
            {
                "loc": list(item.get("loc", ())),
                "msg": "Invalid request value.",
                "type": "value_error",
            }
            for item in error.errors()
        ]
    )
    return JSONResponse({"detail": safe_errors}, status_code=422)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Report that the API process is ready to serve requests."""
    return {"status": "ok"}
