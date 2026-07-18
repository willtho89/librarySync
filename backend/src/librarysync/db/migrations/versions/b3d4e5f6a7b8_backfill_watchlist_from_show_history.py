"""backfill watchlist from show history

Revision ID: b3d4e5f6a7b8
Revises: e4f6a8b1c2d3
Create Date: 2026-07-18 12:00:00.000000

"""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "b3d4e5f6a7b8"
down_revision = "e4f6a8b1c2d3"
branch_labels = None
depends_on = None


_SHOW_HISTORY_SELECT = sa.text(
    """
SELECT DISTINCT user_id, media_item_id, media_type
FROM (
    SELECT
        w.user_id,
        m.id AS media_item_id,
        m.media_type AS media_type
    FROM watched_items w
    JOIN media_items m ON m.id = w.media_item_id
    WHERE m.media_type IN ('tv', 'anime')

    UNION

    SELECT
        w.user_id,
        e.show_media_item_id AS media_item_id,
        m.media_type AS media_type
    FROM watched_items w
    JOIN episode_items e ON e.id = w.episode_item_id
    JOIN media_items m ON m.id = e.show_media_item_id
    WHERE m.media_type IN ('tv', 'anime')
) show_history
"""
)

_INSERT_WATCHLIST_ITEM = sa.text(
    """
INSERT INTO watchlist_items (
    id,
    user_id,
    media_item_id,
    type,
    status,
    source,
    rewatch_requested,
    rewatch_requested_at,
    created_at,
    updated_at
)
VALUES (
    :id,
    :user_id,
    :media_item_id,
    :media_type,
    'added',
    'auto_from_history',
    false,
    NULL,
    :now,
    :now
)
ON CONFLICT (user_id, media_item_id) DO NOTHING
"""
)

# Mirrors _base_watchlist_payload in core/watchlist_sync.py (null ids included).
_BASE_WATCHLIST_PAYLOAD_JSONB = """jsonb_build_object(
        'watchlist_item_id', wi.id,
        'media_item_id', m.id,
        'media_type', wi.type,
        'imdb_id', m.imdb_id,
        'tmdb_id', m.tmdb_id,
        'tvdb_id', m.tvdb_id
    )"""

# Mirrors collect_external_ids in core/watch_pipeline.py: keys imdb/tmdb/tvdb,
# imdb lowercased, each key present only when the source value is non-empty.
_EXTERNAL_IDS_JSONB = """(
        CASE WHEN NULLIF(m.imdb_id, '') IS NOT NULL
            THEN jsonb_build_object('imdb', lower(m.imdb_id))
            ELSE jsonb_build_object()
        END
        || CASE WHEN NULLIF(m.tmdb_id, '') IS NOT NULL
            THEN jsonb_build_object('tmdb', m.tmdb_id)
            ELSE jsonb_build_object()
        END
        || CASE WHEN NULLIF(m.tvdb_id, '') IS NOT NULL
            THEN jsonb_build_object('tvdb', m.tvdb_id)
            ELSE jsonb_build_object()
        END
    )"""

# movie_ids for watchlist types movie/anime, show_ids otherwise (payload builders).
_EXTERNAL_IDS_BY_TYPE_JSONB = (
    "CASE WHEN wi.type IN ('movie', 'anime') "
    f"THEN jsonb_build_object('movie_ids', {_EXTERNAL_IDS_JSONB}) "
    f"ELSE jsonb_build_object('show_ids', {_EXTERNAL_IDS_JSONB}) "
    "END"
)

# Builders return None when no external id is available: skip enqueueing then.
_HAS_EXTERNAL_IDS = """(
        NULLIF(m.imdb_id, '') IS NOT NULL
        OR NULLIF(m.tmdb_id, '') IS NOT NULL
        OR NULLIF(m.tvdb_id, '') IS NOT NULL
    )"""

_SIMKL_ID_JSON = "m.raw->>'simkl_id'"
_SIMKL_IDS_JSONB = (
    f"{_EXTERNAL_IDS_JSONB} || CASE WHEN NULLIF({_SIMKL_ID_JSON}, '') IS NOT NULL "
    f"THEN jsonb_build_object('simkl', {_SIMKL_ID_JSON}) ELSE jsonb_build_object() END"
)
_SIMKL_PAYLOAD_JSONB = (
    f"{_BASE_WATCHLIST_PAYLOAD_JSONB} "
    f"|| jsonb_build_object('simkl_id', {_SIMKL_ID_JSON}) "
    f"|| CASE WHEN wi.type IN ('movie', 'anime') "
    f"THEN jsonb_build_object('movie_ids', {_SIMKL_IDS_JSONB}) "
    f"ELSE jsonb_build_object('show_ids', {_SIMKL_IDS_JSONB}) "
    "END"
)

_LETTERBOXD_FILM_ID_JSON = "m.raw->>'letterboxd_film_id'"
_LETTERBOXD_PAYLOAD_JSONB = (
    f"{_BASE_WATCHLIST_PAYLOAD_JSONB} "
    f"|| jsonb_build_object('letterboxd_film_id', {_LETTERBOXD_FILM_ID_JSON})"
)

_PUBLICMETADB_PAYLOAD_JSONB = (
    f"{_BASE_WATCHLIST_PAYLOAD_JSONB} "
    "|| jsonb_build_object("
    "'tmdb_id', m.tmdb_id, "
    "'media_type', CASE WHEN wi.type = 'tv' THEN 'tv' ELSE 'movie' END"
    ")"
)

_CONFIG_JSONB = "CAST(i.config AS jsonb)"


def _jsonb_truthy(expr: str) -> str:
    """Replicates _coerce_enabled in core/publicmetadb.py for a jsonb value."""
    return f"""CASE
        WHEN jsonb_typeof({expr}) = 'boolean' THEN {expr} = CAST('true' AS jsonb)
        WHEN jsonb_typeof({expr}) = 'number' THEN CAST(CAST({expr} AS text) AS numeric) <> 0
        WHEN jsonb_typeof({expr}) = 'string'
            THEN lower(btrim(btrim(CAST({expr} AS text), '"'))) IN ('1', 'true', 'yes', 'on')
        ELSE false
    END"""


# Replicates is_publicmetadb_sync_enabled against integrations.config:
# sync_enabled, else legacy enabled, else metadata_enabled, else False.
_PUBLICMETADB_SYNC_ENABLED = f"""CASE
        WHEN i.config IS NULL OR jsonb_typeof({_CONFIG_JSONB}) <> 'object' THEN false
        WHEN {_CONFIG_JSONB} ? 'sync_enabled' THEN {_jsonb_truthy(f"{_CONFIG_JSONB} -> 'sync_enabled'")}
        WHEN {_CONFIG_JSONB} ? 'enabled' THEN {_jsonb_truthy(f"{_CONFIG_JSONB} -> 'enabled'")}
        WHEN {_CONFIG_JSONB} ? 'metadata_enabled'
            THEN {_jsonb_truthy(f"{_CONFIG_JSONB} -> 'metadata_enabled'")}
        ELSE false
    END"""

# Mirrors _enqueue_watchlist_job in core/watchlist_sync.py: connected integration
# (status != 'disconnected') with stored secrets, personal watchlist source not
# disabled (missing row = runtime default enabled). wi.status != 'removed' keeps
# pre-existing user-removed items from being re-added at providers. Payload and
# per-provider guards mirror the payload builders exactly.
_OUTBOX_PUSH_INSERT_TEMPLATE = """INSERT INTO outbox (
    id,
    user_id,
    target_provider,
    job_type,
    payload,
    status,
    run_after,
    attempts,
    last_error,
    dedupe_key,
    created_at,
    updated_at
)
SELECT
    :id,
    wi.user_id,
    '{provider}',
    'push_watchlist',
    CAST(
        {payload}
    AS json),
    'pending',
    NULL,
    0,
    NULL,
    wi.user_id || ':{provider}:push_watchlist:' || wi.id,
    :now,
    :now
FROM watchlist_items wi
JOIN media_items m ON m.id = wi.media_item_id
JOIN integrations i
    ON i.user_id = wi.user_id
    AND i.provider = '{provider}'
    AND i.status != 'disconnected'
JOIN integration_secrets s ON s.integration_id = i.id
LEFT JOIN watchlist_sources ws
    ON ws.user_id = wi.user_id
    AND ws.provider = '{provider}'
    AND ws.source_type = 'personal'
    AND ws.external_id = 'watchlist'
WHERE wi.user_id = :user_id
    AND wi.media_item_id = :media_item_id
    AND wi.status != 'removed'
    AND COALESCE(ws.is_enabled, true)
    AND {guard}
ON CONFLICT (dedupe_key) DO NOTHING
"""


def _build_outbox_push_insert(provider: str, payload: str, guard: str) -> sa.TextClause:
    return sa.text(
        _OUTBOX_PUSH_INSERT_TEMPLATE.format(provider=provider, payload=payload, guard=guard)
    )


_OUTBOX_PUSH_INSERTS = {
    "trakt": _build_outbox_push_insert(
        "trakt",
        f"{_BASE_WATCHLIST_PAYLOAD_JSONB} || {_EXTERNAL_IDS_BY_TYPE_JSONB}",
        _HAS_EXTERNAL_IDS,
    ),
    "simkl": _build_outbox_push_insert(
        "simkl",
        _SIMKL_PAYLOAD_JSONB,
        f"({_HAS_EXTERNAL_IDS} OR NULLIF({_SIMKL_ID_JSON}, '') IS NOT NULL)",
    ),
    "letterboxd": _build_outbox_push_insert(
        "letterboxd",
        _LETTERBOXD_PAYLOAD_JSONB,
        "wi.type IN ('movie', 'anime') AND ("
        "NULLIF(m.imdb_id, '') IS NOT NULL "
        "OR NULLIF(m.tmdb_id, '') IS NOT NULL "
        f"OR NULLIF({_LETTERBOXD_FILM_ID_JSON}, '') IS NOT NULL)",
    ),
    "publicmetadb": _build_outbox_push_insert(
        "publicmetadb",
        _PUBLICMETADB_PAYLOAD_JSONB,
        f"NULLIF(m.tmdb_id, '') IS NOT NULL AND ({_PUBLICMETADB_SYNC_ENABLED})",
    ),
}


def _migration_id(user_id: str, media_item_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"librarysync:auto_from_history:{user_id}:{media_item_id}"))


def _migration_push_id(user_id: str, media_item_id: str, provider: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"librarysync:auto_from_history_push:{user_id}:{media_item_id}:{provider}",
        )
    )


def _history_rows(bind) -> list[dict[str, str]]:
    rows = bind.execute(_SHOW_HISTORY_SELECT).mappings().all()
    return [
        {
            "id": _migration_id(row["user_id"], row["media_item_id"]),
            "user_id": row["user_id"],
            "media_item_id": row["media_item_id"],
            "media_type": row["media_type"],
        }
        for row in rows
    ]


def upgrade() -> None:
    bind = op.get_bind()
    rows = _history_rows(bind)
    if not rows:
        return
    now = datetime.now(timezone.utc)
    bind.execute(_INSERT_WATCHLIST_ITEM, [{**row, "now": now} for row in rows])
    # Push every history-derived watchlist item to connected providers, mirroring
    # enqueue_personal_watchlist_sync. The dedupe-key conflict clause skips rows
    # whose push was already enqueued at runtime (e.g. manual adds).
    for provider, statement in _OUTBOX_PUSH_INSERTS.items():
        bind.execute(
            statement,
            [
                {
                    "id": _migration_push_id(row["user_id"], row["media_item_id"], provider),
                    "user_id": row["user_id"],
                    "media_item_id": row["media_item_id"],
                    "now": now,
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    # Intentionally a no-op: watchlist rows are user data once created. Rows
    # backfilled by upgrade() stay in place on downgrade, regardless of
    # whether they came from this migration, manual adds, or provider syncs.
    pass
