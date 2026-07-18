from unittest.mock import patch

from librarysync.db.migrations.versions import b3d4e5f6a7b8_backfill_watchlist_from_show_history as migration


class _FakeMappings:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, str]]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeBind:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        if "FROM watched_items" in str(statement):
            return _FakeResult(self.rows)
        return None


def test_upgrade_inserts_auto_from_history_watchlist_rows() -> None:
    bind = _FakeBind(
        [
            {
                "user_id": "user-1",
                "media_item_id": "show-1",
                "media_type": "tv",
            }
        ]
    )

    with patch.object(migration.op, "get_bind", return_value=bind):
        migration.upgrade()

    select_sql, select_params = bind.calls[0]
    insert_sql, insert_params = bind.calls[1]
    assert select_params is None
    assert "JOIN episode_items" in select_sql
    assert "m.media_type IN ('tv', 'anime')" in select_sql
    assert "INSERT INTO watchlist_items" in insert_sql
    assert "'auto_from_history'" in insert_sql
    assert "ON CONFLICT (user_id, media_item_id) DO NOTHING" in insert_sql
    assert isinstance(insert_params, list)
    assert insert_params[0]["id"] == migration._migration_id("user-1", "show-1")
    assert insert_params[0]["user_id"] == "user-1"
    assert insert_params[0]["media_item_id"] == "show-1"
    assert insert_params[0]["media_type"] == "tv"
    assert "now" in insert_params[0]


def test_downgrade_deletes_only_migration_backfilled_rows() -> None:
    bind = _FakeBind(
        [
            {
                "user_id": "user-1",
                "media_item_id": "show-1",
                "media_type": "tv",
            }
        ]
    )

    with patch.object(migration.op, "get_bind", return_value=bind):
        migration.downgrade()

    delete_sql, delete_params = bind.calls[1]
    assert "DELETE FROM watchlist_items" in delete_sql
    assert "source = 'auto_from_history'" in delete_sql
    assert "id = :id" in delete_sql
    assert delete_params == [{"id": migration._migration_id("user-1", "show-1")}]
