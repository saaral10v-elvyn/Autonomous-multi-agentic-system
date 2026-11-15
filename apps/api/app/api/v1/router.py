"""Version 1 route registry."""

from fastapi import APIRouter

from app.api.v1.agents import router as agents_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.auth import router as auth_router
from app.api.v1.provider_keys import router as provider_keys_router
from app.api.v1.runs import router as runs_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(provider_keys_router)
router.include_router(runs_router)
router.include_router(approvals_router)
router.include_router(agents_router)
