"""sprint 2 consolidated

Revision ID: 4f3e2d1c0b9a
Revises: 1af024f6d6a0
Create Date: 2025-12-28 12:30:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "4f3e2d1c0b9a"
down_revision = "1af024f6d6a0"
branch_labels = None
depends_on = None


WATCH_TARGET_CHECK = (
    "(media_item_id IS NOT NULL AND episode_item_id IS NULL) OR "
    "(media_item_id IS NULL AND episode_item_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "include_adult_in_search",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("users", "include_adult_in_search", server_default=None)

    op.create_table(
        "metadata_lookup_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("query", sa.String(length=255), nullable=False),
        sa.Column("query_type", sa.String(length=32), nullable=False),
        sa.Column("search_scope", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("providers", sa.JSON(), nullable=True),
        sa.Column("selected_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_metadata_lookup_requests_status"),
        "metadata_lookup_requests",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_lookup_requests_user_id"),
        "metadata_lookup_requests",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "metadata_lookup_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lookup_request_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_item_id", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_url", sa.String(length=500), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["lookup_request_id"],
            ["metadata_lookup_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_metadata_lookup_candidates_lookup_request_id"),
        "metadata_lookup_candidates",
        ["lookup_request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_lookup_candidates_provider"),
        "metadata_lookup_candidates",
        ["provider"],
        unique=False,
    )

    op.create_table(
        "media_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.String(length=32), nullable=True),
        sa.Column("tvdb_id", sa.String(length=32), nullable=True),
        sa.Column("kitsu_id", sa.String(length=32), nullable=True),
        sa.Column("tvmaze_id", sa.String(length=32), nullable=True),
        sa.Column("myanimelist_id", sa.String(length=32), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("poster_url", sa.String(length=500), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("imdb_id", name="uq_media_items_imdb_id"),
        sa.UniqueConstraint("media_type", "tmdb_id", name="uq_media_items_tmdb_id_type"),
        sa.UniqueConstraint("media_type", "tvdb_id", name="uq_media_items_tvdb_id_type"),
        sa.UniqueConstraint("media_type", "kitsu_id", name="uq_media_items_kitsu_id_type"),
        sa.UniqueConstraint(
            "media_type", "tvmaze_id", name="uq_media_items_tvmaze_id_type"
        ),
        sa.UniqueConstraint(
            "media_type",
            "myanimelist_id",
            name="uq_media_items_myanimelist_id_type",
        ),
    )
    op.create_index(op.f("ix_media_items_imdb_id"), "media_items", ["imdb_id"], unique=False)
    op.create_index(op.f("ix_media_items_tmdb_id"), "media_items", ["tmdb_id"], unique=False)
    op.create_index(op.f("ix_media_items_tvdb_id"), "media_items", ["tvdb_id"], unique=False)
    op.create_index(op.f("ix_media_items_kitsu_id"), "media_items", ["kitsu_id"], unique=False)
    op.create_index(
        op.f("ix_media_items_tvmaze_id"), "media_items", ["tvmaze_id"], unique=False
    )
    op.create_index(
        op.f("ix_media_items_myanimelist_id"),
        "media_items",
        ["myanimelist_id"],
        unique=False,
    )

    op.create_table(
        "episode_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("show_media_item_id", sa.String(length=36), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("tmdb_id", sa.String(length=32), nullable=True),
        sa.Column("tvdb_id", sa.String(length=32), nullable=True),
        sa.Column("tvmaze_id", sa.String(length=32), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["show_media_item_id"], ["media_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "show_media_item_id",
            "season_number",
            "episode_number",
            name="uq_episode_items_show_season_episode",
        ),
        sa.UniqueConstraint("tmdb_id", name="uq_episode_items_tmdb_id"),
        sa.UniqueConstraint("tvdb_id", name="uq_episode_items_tvdb_id"),
        sa.UniqueConstraint("tvmaze_id", name="uq_episode_items_tvmaze_id"),
        sa.UniqueConstraint("imdb_id", name="uq_episode_items_imdb_id"),
    )
    op.create_index(
        op.f("ix_episode_items_show_media_item_id"),
        "episode_items",
        ["show_media_item_id"],
        unique=False,
    )
    op.create_index(op.f("ix_episode_items_tmdb_id"), "episode_items", ["tmdb_id"])
    op.create_index(op.f("ix_episode_items_tvdb_id"), "episode_items", ["tvdb_id"])
    op.create_index(op.f("ix_episode_items_tvmaze_id"), "episode_items", ["tvmaze_id"])
    op.create_index(op.f("ix_episode_items_imdb_id"), "episode_items", ["imdb_id"])

    op.create_table(
        "watched_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("media_item_id", sa.String(length=36), nullable=True),
        sa.Column("episode_item_id", sa.String(length=36), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(WATCH_TARGET_CHECK, name="ck_watched_items_one_target"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["media_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["episode_item_id"], ["episode_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_watched_items_user_id"), "watched_items", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_watched_items_media_item_id"),
        "watched_items",
        ["media_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watched_items_episode_item_id"),
        "watched_items",
        ["episode_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watched_items_watched_at"),
        "watched_items",
        ["watched_at"],
        unique=False,
    )

    op.create_table(
        "watch_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("media_item_id", sa.String(length=36), nullable=True),
        sa.Column("episode_item_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(WATCH_TARGET_CHECK, name="ck_watch_events_one_target"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["media_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["episode_item_id"], ["episode_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_watch_events_user_id"), "watch_events", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_watch_events_media_item_id"),
        "watch_events",
        ["media_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watch_events_episode_item_id"),
        "watch_events",
        ["episode_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_watch_events_episode_item_id"), table_name="watch_events")
    op.drop_index(op.f("ix_watch_events_media_item_id"), table_name="watch_events")
    op.drop_index(op.f("ix_watch_events_user_id"), table_name="watch_events")
    op.drop_table("watch_events")

    op.drop_index(op.f("ix_watched_items_watched_at"), table_name="watched_items")
    op.drop_index(op.f("ix_watched_items_episode_item_id"), table_name="watched_items")
    op.drop_index(op.f("ix_watched_items_media_item_id"), table_name="watched_items")
    op.drop_index(op.f("ix_watched_items_user_id"), table_name="watched_items")
    op.drop_table("watched_items")

    op.drop_index(op.f("ix_episode_items_imdb_id"), table_name="episode_items")
    op.drop_index(op.f("ix_episode_items_tvmaze_id"), table_name="episode_items")
    op.drop_index(op.f("ix_episode_items_tvdb_id"), table_name="episode_items")
    op.drop_index(op.f("ix_episode_items_tmdb_id"), table_name="episode_items")
    op.drop_index(
        op.f("ix_episode_items_show_media_item_id"), table_name="episode_items"
    )
    op.drop_table("episode_items")

    op.drop_index(op.f("ix_media_items_myanimelist_id"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_tvmaze_id"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_kitsu_id"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_tvdb_id"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_tmdb_id"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_imdb_id"), table_name="media_items")
    op.drop_table("media_items")

    op.drop_index(
        op.f("ix_metadata_lookup_candidates_provider"),
        table_name="metadata_lookup_candidates",
    )
    op.drop_index(
        op.f("ix_metadata_lookup_candidates_lookup_request_id"),
        table_name="metadata_lookup_candidates",
    )
    op.drop_table("metadata_lookup_candidates")

    op.drop_index(
        op.f("ix_metadata_lookup_requests_user_id"),
        table_name="metadata_lookup_requests",
    )
    op.drop_index(
        op.f("ix_metadata_lookup_requests_status"),
        table_name="metadata_lookup_requests",
    )
    op.drop_table("metadata_lookup_requests")

    op.drop_column("users", "include_adult_in_search")
