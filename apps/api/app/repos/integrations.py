"""Tenant-scoped integration credential repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import Integration


class IntegrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: UUID) -> list[Integration]:
        result = await self._session.scalars(
            select(Integration)
            .where(Integration.user_id == user_id)
            .order_by(Integration.created_at)
        )
        return list(result.all())

    async def get_for_user(self, user_id: UUID, integration_id: UUID) -> Integration | None:
        return await self._session.scalar(
            select(Integration).where(
                Integration.user_id == user_id, Integration.id == integration_id
            )
        )

    async def create_for_user(
        self,
        user_id: UUID,
        *,
        integration_id: UUID,
        kind: str,
        ciphertext: bytes,
        nonce: bytes,
        account_label: str,
        last4: str,
    ) -> Integration:
        item = Integration(
            id=integration_id,
            user_id=user_id,
            kind=kind,
            token_ciphertext=ciphertext,
            token_nonce=nonce,
            account_label=account_label,
            last4=last4,
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def delete_for_user(self, user_id: UUID, integration_id: UUID) -> bool:
        item = await self.get_for_user(user_id, integration_id)
        if item is None:
            return False
        await self._session.delete(item)
        await self._session.flush()
        return True
