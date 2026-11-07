"""Tenant-scoped provider key repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_key import ProviderKey


class ProviderKeyRepository:
    """All user-owned key access requires an explicit user ID."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: UUID) -> list[ProviderKey]:
        result = await self._session.execute(
            select(ProviderKey)
            .where(ProviderKey.user_id == user_id)
            .order_by(ProviderKey.created_at)
        )
        return list(result.scalars().all())

    async def get_for_user(self, user_id: UUID, key_id: UUID) -> ProviderKey | None:
        result = await self._session.execute(
            select(ProviderKey).where(ProviderKey.user_id == user_id, ProviderKey.id == key_id)
        )
        return result.scalar_one_or_none()

    async def create_for_user(
        self,
        user_id: UUID,
        *,
        key_id: UUID,
        provider: str,
        key_ciphertext: bytes,
        key_nonce: bytes,
        label: str,
        last4: str,
    ) -> ProviderKey:
        key = ProviderKey(
            id=key_id,
            user_id=user_id,
            provider=provider,
            key_ciphertext=key_ciphertext,
            key_nonce=key_nonce,
            label=label,
            last4=last4,
        )
        self._session.add(key)
        await self._session.flush()
        return key

    async def delete_for_user(self, user_id: UUID, key_id: UUID) -> bool:
        key = await self.get_for_user(user_id, key_id)
        if key is None:
            return False
        await self._session.delete(key)
        await self._session.flush()
        return True
