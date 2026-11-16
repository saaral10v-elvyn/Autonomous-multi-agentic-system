"""Encrypted integration credential storage and metadata operations."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.models.integration import Integration
from app.repos.integrations import IntegrationRepository


class IntegrationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = IntegrationRepository(session)

    async def add(self, user_id: UUID, kind: str, token: str, label: str) -> Integration:
        integration_id = uuid4()
        encrypted = encrypt_secret(
            token,
            user_id=user_id,
            record_id=integration_id,
            master_key=get_settings().master_key,
        )
        item = await self._repo.create_for_user(
            user_id,
            integration_id=integration_id,
            kind=kind,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            account_label=label.strip(),
            last4=token[-4:],
        )
        await self._session.commit()
        await self._session.refresh(item)
        return item

    async def list(self, user_id: UUID) -> list[Integration]:
        return await self._repo.list_for_user(user_id)

    async def remove(self, user_id: UUID, integration_id: UUID) -> bool:
        removed = await self._repo.delete_for_user(user_id, integration_id)
        if removed:
            await self._session.commit()
        return removed
