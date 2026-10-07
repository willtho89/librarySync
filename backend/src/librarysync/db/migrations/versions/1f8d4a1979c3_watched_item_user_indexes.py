"""Index watched items per user and drop indexes duplicated by unique constraints.

History listing, imports and dedupe filter watched_items by user first; the
composite indexes also cover lookups by user_id alone. The dropped indexes
duplicate unique constraints (or their leading column) on the same columns.
"""

from alembic import op

revision = "1f8d4a1979c3"
down_revision = "58fe69244040"
branch_labels = None
depends_on = None

NEW_INDEXES = (
    ("ix_watched_items_user_watched_at", "watched_items", ["user_id", "watched_at"]),
    ("ix_watched_items_user_media", "watched_items", ["user_id", "media_item_id"]),
    ("ix_watched_items_user_episode", "watched_items", ["user_id", "episode_item_id"]),
)
REDUNDANT_INDEXES = (
    ("ix_watched_items_user_id", "watched_items", ["user_id"]),
    ("ix_media_items_imdb_id", "media_items", ["imdb_id"]),
    ("ix_episode_items_tmdb_id", "episode_items", ["tmdb_id"]),
    ("ix_episode_items_tvdb_id", "episode_items", ["tvdb_id"]),
    ("ix_episode_items_tvmaze_id", "episode_items", ["tvmaze_id"]),
    ("ix_episode_items_imdb_id", "episode_items", ["imdb_id"]),
    ("ix_episode_items_show_media_item_id", "episode_items", ["show_media_item_id"]),
)


def upgrade() -> None:
    for name, table, columns in NEW_INDEXES:
        op.create_index(name, table, columns, unique=False, if_not_exists=True)
    for name, table, _columns in REDUNDANT_INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)


def downgrade() -> None:
    for name, table, columns in REDUNDANT_INDEXES:
        op.create_index(name, table, columns, unique=False, if_not_exists=True)
    for name, table, _columns in NEW_INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)
