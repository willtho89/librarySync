from typing import AsyncIterator

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.auth import decode_access_token
from librarysync.db.models import User
from librarysync.db.session import get_session

bearer_scheme = HTTPBearer(auto_error=False)
cookie_scheme = APIKeyCookie(name="access_token", auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    bearer_credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    cookie_token: str | None = Security(cookie_scheme),
) -> User:
    token = bearer_credentials.credentials if bearer_credentials else None
    if not token:
        token = cookie_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    return user
