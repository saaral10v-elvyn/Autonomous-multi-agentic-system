"""Provider key storage and tenant-safe CRUD service."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.models.provider_key import ProviderKey
from app.repos.provider_keys import ProviderKeyRepository


class ProviderKeyService:
    """Encrypt incoming credentials before persisting them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._keys = ProviderKeyRepository(session)

    async def add(self, user_id: UUID, *, provider: str, label: str, api_key: str) -> ProviderKey:
        key_id = uuid4()
        encrypted = encrypt_secret(
            api_key,
            user_id=user_id,
            record_id=key_id,
            master_key=get_settings().master_key,
        )
        key = await self._keys.create_for_user(
            user_id,
            key_id=key_id,
            provider=provider,
            key_ciphertext=encrypted.ciphertext,
            key_nonce=encrypted.nonce,
            label=label.strip(),
            last4=api_key[-4:],
        )
        await self._session.commit()
        await self._session.refresh(key)
        return key

    async def list(self, user_id: UUID) -> list[ProviderKey]:
        return await self._keys.list_for_user(user_id)

    async def get(self, user_id: UUID, key_id: UUID) -> ProviderKey | None:
        return await self._keys.get_for_user(user_id, key_id)

    async def remove(self, user_id: UUID, key_id: UUID) -> bool:
        deleted = await self._keys.delete_for_user(user_id, key_id)
        if deleted:
            await self._session.commit()
        return deleted
