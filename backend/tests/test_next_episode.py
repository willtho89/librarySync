from datetime import date, datetime, timezone
from types import SimpleNamespace

from librarysync.api.routes_dashboard import order_up_next_items
from librarysync.core.next_episode import episode_to_payload, select_next_episode


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
