from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.core.auth import create_access_token, hash_password, verify_password
from librarysync.db.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str


def _normalize_username(username: str) -> str:
    return username.strip().lower()


@router.post(
    "/register",
    response_model=UserOut,
    summary="Register a new user",
    description="Create a local account when registration is enabled.",
)
async def register(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> UserOut:
    if not settings.allow_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration disabled")
    username = _normalize_username(payload.username)
    result = await db.execute(select(User).where(User.username == username))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username already registered"
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = User(username=username, password_hash=password_hash)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserOut(id=user.id, username=user.username)


@router.post(
    "/login",
    summary="Log in",
    description=(
        "Validate credentials and return an access token. Also sets the "
        "`access_token` HttpOnly cookie for browser sessions."
    ),
)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    username = _normalize_username(payload.username)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(user.id)
    response = JSONResponse({"access_token": token, "token_type": "bearer"})
    max_age = settings.jwt_access_token_minutes * 60
    secure_cookie = bool(settings.base_url and settings.base_url.startswith("https"))
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
        max_age=max_age,
    )
    return response


@router.post(
    "/logout",
    summary="Log out",
    description="Clear the authentication cookie for the current session.",
)
async def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("access_token")
    return response


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get current user",
    description="Return the authenticated user's profile.",
)
async def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=current_user.id, username=current_user.username)
