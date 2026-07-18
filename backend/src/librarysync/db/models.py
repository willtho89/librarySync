import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    include_adult_in_search: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_integrations_user_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(32), default="configured")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class IntegrationSecret(Base):
    __tablename__ = "integration_secrets"
    __table_args__ = (
        UniqueConstraint("integration_id", name="uq_integration_secrets_integration_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("integrations.id", ondelete="CASCADE"), index=True
    )
    secret_data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MetadataLookupRequest(Base):
    __tablename__ = "metadata_lookup_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    query: Mapped[str] = mapped_column(String(255))
    query_type: Mapped[str] = mapped_column(String(32))
    search_scope: Mapped[str] = mapped_column(String(16), default="all")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    providers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MetadataLookupCandidate(Base):
    __tablename__ = "metadata_lookup_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    lookup_request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("metadata_lookup_requests.id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_item_id: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(32), default="movie")
    title: Mapped[str] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class BlacklistItem(Base):
    __tablename__ = "blacklist_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "provider_item_id",
            name="uq_blacklist_items_user_provider_item",
        ),
        Index("ix_blacklist_items_user_media_type", "user_id", "media_type"),
        Index("ix_blacklist_items_user_imdb_id", "user_id", "imdb_id"),
        Index("ix_blacklist_items_user_tmdb_id", "user_id", "tmdb_id"),
        Index("ix_blacklist_items_user_tvdb_id", "user_id", "tvdb_id"),
        Index("ix_blacklist_items_user_tvmaze_id", "user_id", "tvmaze_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_type: Mapped[str] = mapped_column(String(32), default="tv")
    provider: Mapped[str] = mapped_column(String(32))
    provider_item_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tmdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvmaze_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("imdb_id", name="uq_media_items_imdb_id"),
        UniqueConstraint("media_type", "tmdb_id", name="uq_media_items_tmdb_id_type"),
        UniqueConstraint("media_type", "tvdb_id", name="uq_media_items_tvdb_id_type"),
        UniqueConstraint("media_type", "kitsu_id", name="uq_media_items_kitsu_id_type"),
        UniqueConstraint("media_type", "tvmaze_id", name="uq_media_items_tvmaze_id_type"),
        UniqueConstraint(
            "media_type",
            "myanimelist_id",
            name="uq_media_items_myanimelist_id_type",
        ),
        UniqueConstraint(
            "media_type",
            "anilist_id",
            name="uq_media_items_anilist_id_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    media_type: Mapped[str] = mapped_column(String(32), default="movie")
    title: Mapped[str] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kitsu_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvmaze_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    myanimelist_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    anilist_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    poster_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    first_air_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    last_air_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    runtime_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genres: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class EpisodeItem(Base):
    __tablename__ = "episode_items"
    __table_args__ = (
        UniqueConstraint(
            "show_media_item_id",
            "season_number",
            "episode_number",
            name="uq_episode_items_show_season_episode",
        ),
        UniqueConstraint("tmdb_id", name="uq_episode_items_tmdb_id"),
        UniqueConstraint("tvdb_id", name="uq_episode_items_tvdb_id"),
        UniqueConstraint("tvmaze_id", name="uq_episode_items_tvmaze_id"),
        UniqueConstraint("imdb_id", name="uq_episode_items_imdb_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    show_media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    season_number: Mapped[int] = mapped_column(Integer)
    episode_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    air_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    tmdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tvmaze_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WatchedItem(Base):
    __tablename__ = "watched_items"
    __table_args__ = (
        CheckConstraint(
            "(media_item_id IS NOT NULL AND episode_item_id IS NULL) OR "
            "(media_item_id IS NULL AND episode_item_id IS NOT NULL)",
            name="ck_watched_items_one_target",
        ),
        CheckConstraint(
            "rating IS NULL OR (rating >= 0.5 AND rating <= 5.0)",
            name="ck_watched_items_rating_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    episode_item_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("episode_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    watched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class WatchEvent(Base):
    __tablename__ = "watch_events"
    __table_args__ = (
        CheckConstraint(
            "(media_item_id IS NOT NULL AND episode_item_id IS NULL) OR "
            "(media_item_id IS NULL AND episode_item_id IS NOT NULL)",
            name="ck_watch_events_one_target",
        ),
        UniqueConstraint(
            "user_id",
            "event_type",
            "entry_key",
            name="uq_watch_events_user_event_entry_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    episode_item_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("episode_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32))
    entry_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class WatchSync(Base):
    __tablename__ = "watch_syncs"
    __table_args__ = (
        UniqueConstraint("watched_item_id", "provider", name="uq_watch_syncs_watched_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    watched_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("watched_items.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    is_rewatch: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class StremioAddonConfig(Base):
    __tablename__ = "stremio_addon_configs"
    __table_args__ = (UniqueConstraint("user_id", name="uq_stremio_addon_configs_user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_catalogs: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class StremioCustomCatalog(Base):
    __tablename__ = "stremio_custom_catalogs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "slug",
            name="uq_stremio_custom_catalogs_user_slug",
        ),
        Index("ix_stremio_custom_catalogs_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(32), default="movie")
    order_by: Mapped[str] = mapped_column(String(32), default="manual")
    order_dir: Mapped[str] = mapped_column(String(8), default="asc")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class StremioCustomCatalogItem(Base):
    __tablename__ = "stremio_custom_catalog_items"
    __table_args__ = (
        UniqueConstraint(
            "catalog_id",
            "media_item_id",
            name="uq_stremio_custom_catalog_items_catalog_media",
        ),
        Index(
            "ix_stremio_custom_catalog_items_catalog_position",
            "catalog_id",
            "position",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    catalog_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stremio_custom_catalogs.id", ondelete="CASCADE"),
        index=True,
    )
    media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StremioExternalCatalog(Base):
    __tablename__ = "stremio_external_catalogs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "slug",
            name="uq_stremio_external_catalogs_user_slug",
        ),
        Index("ix_stremio_external_catalogs_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32), default="manifest")
    source_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    addon_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manifest_url: Mapped[str] = mapped_column(String(500))
    source_catalog_id: Mapped[str] = mapped_column(String(255))
    source_catalog_type: Mapped[str] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(32), default="movie")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    order_by: Mapped[str] = mapped_column(String(32), default="source")
    order_dir: Mapped[str] = mapped_column(String(8), default="asc")
    page_size: Mapped[int] = mapped_column(Integer, default=30)
    show_in_home: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refresh_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class StremioExternalCatalogItem(Base):
    __tablename__ = "stremio_external_catalog_items"
    __table_args__ = (
        UniqueConstraint(
            "catalog_id",
            "position",
            name="uq_stremio_external_catalog_items_catalog_position",
        ),
        UniqueConstraint(
            "catalog_id",
            "stremio_id",
            name="uq_stremio_external_catalog_items_catalog_stremio",
        ),
        Index(
            "ix_stremio_external_catalog_items_catalog_position",
            "catalog_id",
            "position",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    catalog_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stremio_external_catalogs.id", ondelete="CASCADE"),
        index=True,
    )
    media_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="SET NULL"), index=True, nullable=True
    )
    stremio_id: Mapped[str] = mapped_column(String(255))
    stremio_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "media_item_id",
            name="uq_watchlist_items_user_media",
        ),
        Index("ix_watchlist_items_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    # type: movie, show
    type: Mapped[str] = mapped_column(String(32))
    # status: added, in_progress, watched, not_released, hidden, dropped, removed
    status: Mapped[str] = mapped_column(String(32), default="added", index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    rewatch_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rewatch_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WatchlistSource(Base):
    __tablename__ = "watchlist_sources"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "source_type",
            "external_id",
            name="uq_watchlist_sources_user_provider_type_external",
        ),
        Index("ix_watchlist_sources_user_provider", "user_id", "provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WatchlistSourceItem(Base):
    __tablename__ = "watchlist_source_items"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "watchlist_item_id",
            name="uq_watchlist_source_items_source_item",
        ),
        Index("ix_watchlist_source_items_user_media", "user_id", "media_item_id"),
        Index("ix_watchlist_source_items_source_seen", "source_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("watchlist_sources.id", ondelete="CASCADE"), index=True
    )
    watchlist_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    external_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProgressEvent(Base):
    __tablename__ = "progress_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source_provider: Mapped[str] = mapped_column(String(50), index=True)
    item_key: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    progress_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class OutboxJob(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        Index(
            "ix_outbox_user_status_run_after",
            "user_id",
            "status",
            "run_after",
        ),
        Index("ix_outbox_user_provider", "user_id", "target_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_provider: Mapped[str] = mapped_column(String(50), index=True)
    job_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SyncAttempt(Base):
    __tablename__ = "sync_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32))
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_rate_limit_buckets_user_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    tokens: Mapped[float] = mapped_column(Float, default=0.0)
    last_refill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
