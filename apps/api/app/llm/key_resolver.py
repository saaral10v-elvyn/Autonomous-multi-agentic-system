"""Tenant-scoped encrypted provider-key resolution."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.types import EncryptedProviderCredential, Provider, ProviderKeyResolver
from app.repos.provider_keys import ProviderKeyRepository


class RepositoryProviderKeyResolver(ProviderKeyResolver):
    """Read ciphertext only through the user-scoped provider-key repository."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_for_user(
        self, user_id: UUID, provider_key_id: UUID
    ) -> EncryptedProviderCredential | None:
        async with self._sessions() as session:
            key = await ProviderKeyRepository(session).get_for_user(user_id, provider_key_id)
            if key is None:
                return None
            return EncryptedProviderCredential(
                id=key.id,
                user_id=key.user_id,
                provider=Provider(key.provider),
                ciphertext=key.key_ciphertext,
                nonce=key.key_nonce,
            )
