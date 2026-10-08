from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from librarysync.db.models import Base, MetadataLookupRequest, User
from librarysync.jobs import metadata_lookup
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id="u", username="u", password_hash="x"))
        await db.commit()
    yield session_factory
    await engine.dispose()


async def _add(factory, request_id: str, status: str, updated_at: datetime) -> None:
    async with factory() as db:
        db.add(
            MetadataLookupRequest(
                id=request_id, user_id="u", query="Heat", query_type="title", status=status, updated_at=updated_at
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_stale_in_progress_lookups_are_reclaimed(factory):
    await _add(factory, "stranded", "in_progress", NOW - timedelta(minutes=30))
    await _add(factory, "running", "in_progress", NOW)
    await _add(factory, "queued", "pending", NOW)

    async with factory() as db:
        claimed = await metadata_lookup._claim_pending_requests(db, 10)

    assert sorted(request.id for request in claimed) == ["queued", "stranded"]
    assert {request.status for request in claimed} == {"in_progress"}
