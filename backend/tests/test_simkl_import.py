import asyncio
from datetime import date

from librarysync.db.models import EpisodeItem
from librarysync.jobs import simkl_import


def test_extract_episode_summary_parses_utc_air_date_and_preserves_finale_type() -> None:
    summary = simkl_import._extract_episode_summary(
        {
            "episode": {
                "season": 2,
                "episode": 8,
                "title": "Finale",
                "aired": "2026-01-31T23:15:00Z",
                "finale_type": 2,
                "ids": {"simkl": 456},
            }
        }
    )

    assert summary is not None
    assert summary.air_date == date(2026, 1, 31)
    assert summary.raw["aired"] == "2026-01-31T23:15:00Z"
    assert summary.raw["finale_type"] == 2


def test_apply_episode_updates_sets_air_date_and_merges_simkl_raw() -> None:
    item = EpisodeItem(
        show_media_item_id="show-1",
        season_number=1,
        episode_number=1,
        title=None,
        air_date=None,
        raw=None,
    )
    summary = simkl_import.EpisodeSummary(
        season_number=1,
        episode_number=1,
        title="Pilot",
        imdb_id=None,
        tmdb_id=None,
        tvdb_id=None,
        simkl_id="789",
        air_date=date(2026, 2, 1),
        raw={"finale_type": 1},
    )

    asyncio.run(simkl_import._apply_episode_updates(item=item, episode=summary, db=None))

    assert item.title == "Pilot"
    assert item.air_date == date(2026, 2, 1)
    assert item.raw == {"simkl_id": "789", "simkl": {"finale_type": 1}}
