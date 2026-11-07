"""Authentication service operations."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import burn_dummy_hash, hash_password, verify_password
from app.models.user import User
from app.repos.users import UserRepository


class DuplicateEmailError(ValueError):
    """An account already exists for the normalized email."""


class AuthService:
    """Signup and password authentication without request or secret logging."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def signup(self, email: str, password: str) -> User:
        normalized_email = email.strip().casefold()
        if await self._users.find_by_email_for_auth(normalized_email) is not None:
            raise DuplicateEmailError
        try:
            user = await self._users.create(normalized_email, hash_password(password))
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise DuplicateEmailError from None
        await self._session.refresh(user)
        return user

    async def authenticate(self, email: str, password: str) -> User | None:
        normalized_email = email.strip().casefold()
        user = await self._users.find_by_email_for_auth(normalized_email)
        if user is None:
            burn_dummy_hash(password)
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user
