"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.v1.router import router as v1_router
from app.api.v1.websockets import router as websocket_router
from app.core.config import get_settings
from app.core.crypto import decode_master_key
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.security import OriginCSRFMiddleware
from app.core.validation import validation_messages
from app.events.redis import close_redis

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
    logger.info("api.stopped")


app = FastAPI(title="Forge API", version="0.1.0", lifespan=lifespan)
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
