"""User repository with explicit identity-scoped reads."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """Queries and writes user account records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_email_for_auth(self, email: str) -> User | None:
        """Find an account by its login identifier."""
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: UUID) -> User | None:
        """Get only the user identified by the required authenticated user ID."""
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def create(self, email: str, password_hash: str) -> User:
        """Add an account and flush it so callers can handle unique-email races."""
        user = User(email=email, password_hash=password_hash)
        self._session.add(user)
        await self._session.flush()
        return user
