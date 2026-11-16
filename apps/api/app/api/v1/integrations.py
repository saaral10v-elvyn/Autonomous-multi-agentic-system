"""Tenant-scoped GitHub and Vercel credential endpoints."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import IntegrationCreateRequest, IntegrationKind, IntegrationResponse
from app.services.integrations import IntegrationService

router = APIRouter(prefix="/integrations")


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(user: CurrentUser, session: DbSession) -> list[IntegrationResponse]:
    return [
        IntegrationResponse.model_validate(item)
        for item in await IntegrationService(session).list(user.id)
    ]


@router.post(
    "/{kind}/connect", response_model=IntegrationResponse, status_code=status.HTTP_201_CREATED
)
async def connect_integration(
    kind: IntegrationKind, payload: IntegrationCreateRequest, user: CurrentUser, session: DbSession
) -> IntegrationResponse:
    item = await IntegrationService(session).add(
        user.id,
        kind.value,
        payload.token.get_secret_value(),
        payload.account_label,
    )
    return IntegrationResponse.model_validate(item)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: UUID, user: CurrentUser, session: DbSession
) -> Response:
    if not await IntegrationService(session).remove(user.id, integration_id):
        raise HTTPException(status_code=404, detail="Integration not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
