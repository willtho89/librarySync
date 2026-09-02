from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from librarysync.api.routes_dashboard import order_up_next_items
from librarysync.core.next_episode import (
    episode_to_payload,
    find_next_episode,
    find_next_episodes_bulk,
    mark_next_episode_watched,
    select_next_episode,
)
from librarysync.db.models import Base, EpisodeItem, MediaItem, OutboxJob, User, WatchedItem, WatchEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _episode(episode_id, season, episode_number, air_date=None, title="Episode"):
    return SimpleNamespace(
        id=episode_id,
        show_media_item_id="show-1",
        season_number=season,
        episode_number=episode_number,
        title=title,
        air_date=air_date,
    )


class TestSelectNextEpisode:
    def test_returns_first_unwatched_episode(self):
        episodes = [_episode("e1", 1, 1), _episode("e2", 1, 2), _episode("e3", 1, 3)]
        assert select_next_episode(episodes, {"e1", "e2"}) is episodes[2]

    def test_returns_first_episode_when_nothing_watched(self):
        episodes = [_episode("e1", 1, 1), _episode("e2", 1, 2)]
        assert select_next_episode(episodes, set()) is episodes[0]

    def test_uses_latest_watched_episode_instead_of_earliest_unwatched(self):
        episodes = [
            _episode("s1e1", 1, 1),
            _episode("s1e2", 1, 2),
            _episode("s1e3", 1, 3),
            _episode("s2e1", 2, 1),
            _episode("s2e2", 2, 2),
            _episode("s2e3", 2, 3),
        ]
        assert select_next_episode(episodes, {"s2e2"}) is episodes[5]

    def test_returns_none_when_all_watched(self):
        episodes = [_episode("e1", 1, 1), _episode("e2", 1, 2)]
        assert select_next_episode(episodes, {"e1", "e2"}) is None

    def test_returns_none_for_empty_list(self):
        assert select_next_episode([], set()) is None


class TestEpisodeToPayload:
    def test_serializes_episode_fields(self):
        episode = _episode("e1", 2, 5, air_date=date(2024, 3, 1), title="Finale")
        payload = episode_to_payload(episode)
        assert payload == {
            "episode_item_id": "e1",
            "season_number": 2,
            "episode_number": 5,
            "title": "Finale",
            "air_date": "2024-03-01",
        }

    def test_handles_missing_air_date(self):
        episode = _episode("e1", 1, 1, air_date=None)
        assert episode_to_payload(episode)["air_date"] is None


class TestOrderUpNextItems:
    def _item(self, media_item_id, last_watched_at, is_new_release):
        return {
            "media_item_id": media_item_id,
            "last_watched_at": datetime(2024, 1, last_watched_at, tzinfo=timezone.utc),
            "is_new_release": is_new_release,
        }

    def test_continue_watching_comes_before_new_releases(self):
        binge = self._item("binge", 10, False)
        new_release = self._item("new", 20, True)
        ordered = order_up_next_items([new_release, binge])
        assert [item["media_item_id"] for item in ordered] == ["binge", "new"]

    def test_groups_are_ordered_by_last_watched_desc(self):
        items = [
            self._item("old-binge", 5, False),
            self._item("recent-binge", 12, False),
            self._item("stale-new", 2, True),
            self._item("fresh-new", 8, True),
        ]
        ordered = order_up_next_items(items)
        assert [item["media_item_id"] for item in ordered] == [
            "recent-binge",
            "old-binge",
            "fresh-new",
            "stale-new",
        ]

    def test_new_release_for_recently_watched_show_outranks_old_show(self):
        # A new episode for a show watched a week ago ranks above a new
        # episode for a show not watched in two years.
        week_ago = self._item("week-ago", 11, True)
        two_years_ago = self._item("two-years-ago", 1, True)
        ordered = order_up_next_items([two_years_ago, week_ago])
        assert [item["media_item_id"] for item in ordered] == ["week-ago", "two-years-ago"]

    def test_ordering_does_not_mutate_input(self):
        first = self._item("first", 1, True)
        second = self._item("second", 2, False)
        original = [first, second]
        ordered = order_up_next_items(original)
        assert [item["media_item_id"] for item in original] == ["first", "second"]
        assert [item["media_item_id"] for item in ordered] == ["second", "first"]


@pytest_asyncio.fixture
async def db_session(tmp_path) -> AsyncSession:
    pytest.importorskip("aiosqlite")
    db_path = tmp_path / "next-episode-test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                MediaItem.__table__,
                EpisodeItem.__table__,
                WatchedItem.__table__,
                WatchEvent.__table__,
                OutboxJob.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _create_user(db: AsyncSession, username: str = "tester") -> User:
    user = User(username=username, password_hash="hash")
    db.add(user)
    await db.flush()
    return user


async def _create_show(
    db: AsyncSession,
    title: str,
    *,
    media_type: str = "tv",
) -> MediaItem:
    show = MediaItem(media_type=media_type, title=title)
    db.add(show)
    await db.flush()
    return show


async def _create_episode(
    db: AsyncSession,
    show_id: str,
    season_number: int,
    episode_number: int,
    air_date: date,
    *,
    title: str,
) -> EpisodeItem:
    episode = EpisodeItem(
        show_media_item_id=show_id,
        season_number=season_number,
        episode_number=episode_number,
        air_date=air_date,
        title=title,
    )
    db.add(episode)
    await db.flush()
    return episode


@pytest.mark.asyncio
async def test_find_next_episode_and_bulk_return_first_released_unwatched(db_session: AsyncSession):
    user = await _create_user(db_session)
    first_show = await _create_show(db_session, "Show One")
    second_show = await _create_show(db_session, "Show Two")

    first_show_ep1 = await _create_episode(
        db_session,
        first_show.id,
        1,
        1,
        date(2024, 1, 1),
        title="Show One E1",
    )
    first_show_ep2 = await _create_episode(
        db_session,
        first_show.id,
        1,
        2,
        date(2024, 1, 2),
        title="Show One E2",
    )
    second_show_ep1 = await _create_episode(
        db_session,
        second_show.id,
        1,
        1,
        date(2024, 1, 1),
        title="Show Two E1",
    )
    second_show_ep2 = await _create_episode(
        db_session,
        second_show.id,
        1,
        2,
        date(2024, 1, 3),
        title="Show Two E2",
    )

    db_session.add_all(
        [
            # Episode-level watched rows should be respected.
            WatchedItem(
                user_id=user.id,
                media_item_id=None,
                episode_item_id=first_show_ep1.id,
                watched_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
                source="manual",
            ),
            WatchedItem(
                user_id=user.id,
                media_item_id=None,
                episode_item_id=second_show_ep1.id,
                watched_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
                source="manual",
            ),
            # Media-level watched row should be ignored for next-episode selection.
            WatchedItem(
                user_id=user.id,
                media_item_id=first_show.id,
                episode_item_id=None,
                watched_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
                source="manual",
            ),
        ]
    )
    await db_session.flush()

    now_date = date(2024, 1, 10)
    next_first_show = await find_next_episode(db_session, user.id, first_show.id, now_date)
    assert next_first_show is not None
    assert next_first_show.id == first_show_ep2.id

    bulk_next = await find_next_episodes_bulk(
        db_session,
        user.id,
        [first_show.id, second_show.id],
        now_date,
    )
    assert bulk_next[first_show.id].id == first_show_ep2.id
    assert bulk_next[second_show.id].id == second_show_ep2.id


@pytest.mark.asyncio
async def test_find_next_episode_uses_last_watched_episode_across_seasons(db_session: AsyncSession):
    user = await _create_user(db_session)
    show = await _create_show(db_session, "Season Hopping Show")

    await _create_episode(db_session, show.id, 1, 1, date(2024, 1, 1), title="S1E1")
    await _create_episode(db_session, show.id, 1, 2, date(2024, 1, 2), title="S1E2")
    season_two_ep1 = await _create_episode(db_session, show.id, 2, 1, date(2024, 2, 1), title="S2E1")
    season_two_ep2 = await _create_episode(db_session, show.id, 2, 2, date(2024, 2, 2), title="S2E2")
    season_two_ep3 = await _create_episode(db_session, show.id, 2, 3, date(2024, 2, 3), title="S2E3")

    db_session.add(
        WatchedItem(
            user_id=user.id,
            media_item_id=None,
            episode_item_id=season_two_ep2.id,
            watched_at=datetime(2024, 2, 10, tzinfo=timezone.utc),
            source="manual",
        )
    )
    await db_session.flush()

    next_episode = await find_next_episode(db_session, user.id, show.id, date(2024, 2, 10))
    assert next_episode is not None
    assert next_episode.id == season_two_ep3.id


@pytest.mark.asyncio
async def test_mark_next_episode_watched_creates_watched_event_and_internal_outbox_job(
    db_session: AsyncSession,
):
    user = await _create_user(db_session)
    show = await _create_show(db_session, "Up Next Show")
    first_episode = await _create_episode(
        db_session,
        show.id,
        1,
        1,
        date(2024, 1, 1),
        title="Pilot",
    )
    await _create_episode(
        db_session,
        show.id,
        1,
        2,
        date(2024, 1, 2),
        title="Episode 2",
    )

    watched, marked_episode = await mark_next_episode_watched(db_session, user.id, show)

    assert marked_episode.id == first_episode.id

    watched_row = await db_session.get(WatchedItem, watched.id)
    assert watched_row is not None
    assert watched_row.episode_item_id == first_episode.id
    assert watched_row.media_item_id is None

    events = (
        await db_session.execute(
            select(WatchEvent).where(
                WatchEvent.user_id == user.id,
                WatchEvent.episode_item_id == first_episode.id,
                WatchEvent.event_type == "manual_watched",
            )
        )
    ).scalars().all()
    assert len(events) == 1

    outbox_jobs = (
        await db_session.execute(
            select(OutboxJob).where(
                OutboxJob.user_id == user.id,
                OutboxJob.target_provider == "internal",
                OutboxJob.job_type == "new_item_added",
            )
        )
    ).scalars().all()
    assert len(outbox_jobs) == 1
    assert outbox_jobs[0].payload.get("watched_item_id") == watched.id


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["all_watched", "no_released"])
async def test_mark_next_episode_watched_raises_value_error_when_no_next_episode(
    db_session: AsyncSession,
    setup: str,
):
    user = await _create_user(db_session)
    show = await _create_show(db_session, "No Next Show")

    episode = await _create_episode(
        db_session,
        show.id,
        1,
        1,
        date(2024, 1, 1) if setup == "all_watched" else date(2100, 1, 1),
        title="Episode",
    )
    if setup == "all_watched":
        db_session.add(
            WatchedItem(
                user_id=user.id,
                media_item_id=None,
                episode_item_id=episode.id,
                watched_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                source="manual",
            )
        )
        await db_session.flush()

    with pytest.raises(ValueError, match="No released, unwatched episode found for this show"):
        await mark_next_episode_watched(db_session, user.id, show)
