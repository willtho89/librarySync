from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

MediaType = Literal["movie", "episode"]
EventType = Literal["progress", "completed"]
OutboxStatus = Literal[
    "pending",
    "in_progress",
    "succeeded",
    "failed_permanent",
    "failed_retryable",
]


@dataclass(frozen=True)
class PlaybackSession:
    session_key: str
    user_id: str
    provider: str
    imdb_id_raw: str
    imdb_id: str
    media_type: MediaType
    season: int | None
    episode: int | None
    progress_percent: float
    first_seen_at: datetime
    last_seen_at: datetime
    url: str | None
    filename: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProgressEvent:
    event_id: str
    user_id: str
    source_provider: str
    item_key: str
    event_type: EventType
    progress_percent: float | None
    occurred_at: datetime
    session_key: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ItemState:
    user_id: str
    item_key: str
    last_progress_percent: float | None
    completed_at: datetime | None
    last_seen_at: datetime


@dataclass(frozen=True)
class OutboxJob:
    job_id: str
    user_id: str
    target_provider: str
    job_type: str
    payload: dict[str, Any]
    status: OutboxStatus
    run_after: datetime | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class ItemMapping:
    item_key: str
    provider: str
    raw: dict[str, Any]
    updated_at: datetime
