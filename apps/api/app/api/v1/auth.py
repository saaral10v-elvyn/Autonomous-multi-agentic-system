"""Signup, login, logout, and current-user routes."""

from fastapi import APIRouter, HTTPException, Response, status

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import CredentialsRequest, UserResponse
from app.core.security import clear_session_cookie, set_session_cookie
from app.models.user import User
from app.services.auth import AuthService, DuplicateEmailError

router = APIRouter()


@router.post("/auth/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: CredentialsRequest,
    response: Response,
    session: DbSession,
) -> User:
    try:
        user = await AuthService(session).signup(payload.email, payload.password.get_secret_value())
    except DuplicateEmailError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An account already exists."
        ) from None
    set_session_cookie(response, user.id, user.session_version)
    return user


@router.post("/auth/login", response_model=UserResponse)
async def login(
    payload: CredentialsRequest,
    response: Response,
    session: DbSession,
) -> User:
    user = await AuthService(session).authenticate(
        payload.email, payload.password.get_secret_value()
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )
    set_session_cookie(response, user.id, user.session_version)
    return user


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user: CurrentUser, session: DbSession) -> Response:
    user.session_version += 1
    await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> User:
    return user
