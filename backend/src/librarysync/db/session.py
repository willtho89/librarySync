from typing import AsyncIterator

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from librarysync.config import settings

SessionLocal = async_sessionmaker(autoflush=False, expire_on_commit=False)
_ENGINE: AsyncEngine | None = None


def get_async_database_url() -> str:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    url = make_url(settings.database_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
        return url.render_as_string(hide_password=False)
    if url.drivername == "postgresql+psycopg2":
        url = url.set(drivername="postgresql+psycopg")
        return url.render_as_string(hide_password=False)
    return settings.database_url


def get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    _ENGINE = create_async_engine(get_async_database_url(), pool_pre_ping=True)
    return _ENGINE


def init_session_factory() -> None:
    engine = get_engine()
    SessionLocal.configure(bind=engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    init_session_factory()
    async with SessionLocal() as session:
        yield session
