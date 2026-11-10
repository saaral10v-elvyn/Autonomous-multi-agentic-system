"""Request-scoped authentication and gateway dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionFactory, get_db_session
from app.core.security import SESSION_COOKIE, verify_session_token
from app.llm.client import LLMClient
from app.llm.fake import DemoWorkflowFakeProvider, DevelopmentFakeKeyResolver
from app.llm.key_resolver import RepositoryProviderKeyResolver
from app.llm.types import Provider
from app.models.user import User
from app.repos.users import UserRepository

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(request: Request, session: DbSession) -> User:
    """Resolve the cookie session to a database user or return a generic 401."""
    session_claims = verify_session_token(request.cookies.get(SESSION_COOKIE))
    if session_claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
        )
    user_id, token_version = session_claims
    user = await UserRepository(session).get_by_id(user_id)
    if user is None or user.session_version != token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_llm_client() -> LLMClient:
    """Build the gateway with a repository-based, tenant-scoped key resolver."""
    if get_settings().fake_provider_enabled:
        provider = DemoWorkflowFakeProvider()
        return LLMClient(
            DevelopmentFakeKeyResolver(),
            providers={provider_name: provider for provider_name in Provider},
        )
    return LLMClient(RepositoryProviderKeyResolver(SessionFactory))


LLMGateway = Annotated[LLMClient, Depends(get_llm_client)]
