"""Tenant-scoped BYOK provider key endpoints."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.v1.deps import CurrentUser, DbSession, LLMGateway
from app.api.v1.schemas import (
    ProviderKeyCreateRequest,
    ProviderKeyResponse,
    ProviderKeyTestResponse,
)
from app.llm.errors import LLMRequestError, ProviderKeyUnavailable
from app.llm.types import LLMRunContext, ModelTier, Provider
from app.services.provider_keys import ProviderKeyService

router = APIRouter(prefix="/provider-keys")


@router.get("", response_model=list[ProviderKeyResponse])
async def list_provider_keys(user: CurrentUser, session: DbSession) -> list[ProviderKeyResponse]:
    keys = await ProviderKeyService(session).list(user.id)
    return [ProviderKeyResponse.model_validate(key) for key in keys]


@router.post("", response_model=ProviderKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_provider_key(
    payload: ProviderKeyCreateRequest,
    user: CurrentUser,
    session: DbSession,
) -> ProviderKeyResponse:
    key = await ProviderKeyService(session).add(
        user.id,
        provider=payload.provider.value,
        label=payload.label,
        api_key=payload.api_key.get_secret_value(),
    )
    return ProviderKeyResponse.model_validate(key)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_key(key_id: UUID, user: CurrentUser, session: DbSession) -> Response:
    deleted = await ProviderKeyService(session).remove(user.id, key_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider key not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{key_id}/test", response_model=ProviderKeyTestResponse)
async def test_provider_key(
    key_id: UUID,
    user: CurrentUser,
    session: DbSession,
    llm: LLMGateway,
) -> ProviderKeyTestResponse:
    key = await ProviderKeyService(session).get(user.id, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider key not found.")
    provider = Provider(key.provider)
    try:
        result = await llm.complete(
            messages=[{"role": "user", "content": "Reply with OK."}],
            tools=None,
            model_tier=ModelTier.FAST,
            run_ctx=LLMRunContext(user_id=user.id, provider=provider, provider_key_id=key.id),
        )
    except ProviderKeyUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Provider key not found."
        ) from None
    except LLMRequestError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Provider test failed.",
        ) from None
    return ProviderKeyTestResponse(
        ok=True,
        provider=provider,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd if result.cost_usd.is_finite() else Decimal("0"),
    )
