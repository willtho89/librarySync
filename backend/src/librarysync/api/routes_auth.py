from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.core.auth import create_access_token, hash_password_async, verify_password_async
from librarysync.core.login_throttle import LOGIN_THROTTLE
from librarysync.db.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"\S")
    password: str = Field(min_length=1, max_length=1024)


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
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> UserOut:
    if not settings.allow_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration disabled")
    if db.bind.dialect.name == "postgresql":
        # Serialize registrations so concurrent requests cannot exceed max_users.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('librarysync:register'))"))
    if settings.max_users >= 0:
        result = await db.execute(select(func.count(User.id)))
        user_count = result.scalar_one()
        if user_count >= settings.max_users:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="User limit reached"
            )
    username = _normalize_username(payload.username)
    result = await db.execute(select(User).where(User.username == username))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username already registered"
        )

    try:
        password_hash = await hash_password_async(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = User(username=username, password_hash=password_hash)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username already registered"
        ) from exc
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
async def login(
    payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    username = _normalize_username(payload.username)
    address = request.client.host if request.client else None
    retry_after = LOGIN_THROTTLE.retry_after(username, address)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    valid = await verify_password_async(payload.password, user.password_hash if user else None)
    if not user or not valid or not user.is_active:
        LOGIN_THROTTLE.record_failure(username, address)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    LOGIN_THROTTLE.reset(username)

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
