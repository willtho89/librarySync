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


_PROVIDERS = ["trakt", "simkl", "letterboxd", "publicmetadb"]


def _run_upgrade_with_one_row() -> _FakeBind:
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
    return bind


def test_upgrade_enqueues_push_watchlist_outbox_jobs_per_provider() -> None:
    bind = _run_upgrade_with_one_row()

    outbox_calls = bind.calls[2:]
    assert len(outbox_calls) == len(_PROVIDERS)
    for (sql, params), provider in zip(outbox_calls, _PROVIDERS):
        assert "INSERT INTO outbox" in sql
        assert "'push_watchlist'" in sql
        assert f"'{provider}'" in sql
        assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql
        assert f"wi.user_id || ':{provider}:push_watchlist:' || wi.id" in sql
        assert isinstance(params, list)
        assert len(params) == 1
        assert params[0]["id"] == migration._migration_push_id("user-1", "show-1", provider)
        assert params[0]["user_id"] == "user-1"
        assert params[0]["media_item_id"] == "show-1"
        assert "now" in params[0]


def test_outbox_insert_mirrors_runtime_provider_gating() -> None:
    bind = _run_upgrade_with_one_row()

    for sql, _params in bind.calls[2:]:
        # Connected integration with secrets (mirrors _enqueue_watchlist_job).
        assert "JOIN integrations i" in sql
        assert "i.status != 'disconnected'" in sql
        assert "JOIN integration_secrets s ON s.integration_id = i.id" in sql
        # Personal watchlist source not disabled (missing row = enabled).
        assert "ws.source_type = 'personal'" in sql
        assert "ws.external_id = 'watchlist'" in sql
        assert "COALESCE(ws.is_enabled, true)" in sql
        # Payload identity mirrors _base_watchlist_payload.
        assert "'watchlist_item_id', wi.id" in sql
        assert "'media_item_id', m.id" in sql
        assert "'media_type', wi.type" in sql


def test_outbox_payload_guards_mirror_payload_builders() -> None:
    bind = _run_upgrade_with_one_row()
    sql_by_provider = {provider: sql for (sql, _), provider in zip(bind.calls[2:], _PROVIDERS)}

    trakt = sql_by_provider["trakt"]
    # collect_external_ids shape: imdb lowercased, keys only when non-empty.
    assert "jsonb_build_object('imdb', lower(m.imdb_id))" in trakt
    assert "jsonb_build_object('tmdb', m.tmdb_id)" in trakt
    assert "jsonb_build_object('tvdb', m.tvdb_id)" in trakt
    assert "NULLIF(m.imdb_id, '') IS NOT NULL" in trakt
    assert "NULLIF(m.tmdb_id, '') IS NOT NULL" in trakt
    assert "NULLIF(m.tvdb_id, '') IS NOT NULL" in trakt
    assert "'movie_ids'" in trakt
    assert "'show_ids'" in trakt

    simkl = sql_by_provider["simkl"]
    assert "m.raw->>'simkl_id'" in simkl
    assert "jsonb_build_object('simkl_id', m.raw->>'simkl_id')" in simkl
    assert "jsonb_build_object('simkl', m.raw->>'simkl_id')" in simkl

    letterboxd = sql_by_provider["letterboxd"]
    assert "wi.type IN ('movie', 'anime')" in letterboxd
    assert "m.raw->>'letterboxd_film_id'" in letterboxd
    assert "NULLIF(m.imdb_id, '') IS NOT NULL" in letterboxd
    assert "NULLIF(m.tmdb_id, '') IS NOT NULL" in letterboxd
    assert "NULLIF(m.raw->>'letterboxd_film_id', '') IS NOT NULL" in letterboxd

    publicmetadb = sql_by_provider["publicmetadb"]
    assert "NULLIF(m.tmdb_id, '') IS NOT NULL" in publicmetadb
    # is_publicmetadb_sync_enabled config key precedence.
    assert "? 'sync_enabled'" in publicmetadb
    assert "? 'metadata_enabled'" in publicmetadb
    assert "? 'enabled'" in publicmetadb
    assert "jsonb_typeof" in publicmetadb


def test_upgrade_without_history_rows_enqueues_nothing() -> None:
    bind = _FakeBind([])

    with patch.object(migration.op, "get_bind", return_value=bind):
        migration.upgrade()

    assert len(bind.calls) == 1  # only the history select


def test_downgrade_leaves_watchlist_rows_untouched() -> None:
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

    assert bind.calls == []
