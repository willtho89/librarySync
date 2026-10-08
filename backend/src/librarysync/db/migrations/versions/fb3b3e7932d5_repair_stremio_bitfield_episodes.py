"""Repair Stremio bitfield imports that attached every episode to the last one.

A closure bug in the Stremio series-bitfield import resolved every episode
candidate of a show to the final episode of the batch. Each candidate still
wrote a ``stremio_imported`` watch event whose ``video_id``
(``tt<id>:<season>:<episode>``) names the episode that was really watched, but
the event, the watch and its Stremio sync all point at the last episode. Every
affected batch wrote at least two events with the same timestamp.

The import events are the source of truth because history merging may already
have collapsed the misattributed watches into one. Events are grouped by user,
linked episode and timestamp; a group needs repair when one of its video ids
names a different, existing episode of the same show. Per group:

- an event naming the linked episode itself keeps a watch there (preferably the
  one whose Stremio sync carries its video id, else the merge survivor) so its
  downstream deliveries stay valid; its Stremio sync id is corrected;
- every other event takes the watch whose Stremio sync carries its video id
  (unmerged case) and moves it, or gets a new watch, Stremio sync and
  ``new_item_added`` job (merged case). Moved watches lose their stale
  downstream sync rows and queued jobs and are re-delivered;
- watches the user deleted by hand or cleared through Watch State are not
  recreated;
- the events are repointed to the episode they describe.
"""

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "fb3b3e7932d5"
down_revision = "8e9f0a1b2c3d"
branch_labels = None
depends_on = None

VIDEO_ID_RE = re.compile(r"^tt\d+:(\d{1,4}):(\d{1,5})$")

watch_events = sa.table(
    "watch_events",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("episode_item_id", sa.String),
    sa.column("event_type", sa.String),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("raw", sa.JSON),
)
episode_items = sa.table(
    "episode_items",
    sa.column("id", sa.String),
    sa.column("show_media_item_id", sa.String),
    sa.column("season_number", sa.Integer),
    sa.column("episode_number", sa.Integer),
)
watched_items = sa.table(
    "watched_items",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("media_item_id", sa.String),
    sa.column("episode_item_id", sa.String),
    sa.column("watched_at", sa.DateTime(timezone=True)),
    sa.column("source", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
watch_syncs = sa.table(
    "watch_syncs",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("watched_item_id", sa.String),
    sa.column("provider", sa.String),
    sa.column("status", sa.String),
    sa.column("is_rewatch", sa.Boolean),
    sa.column("external_id", sa.String),
    sa.column("last_synced_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
outbox = sa.table(
    "outbox",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("target_provider", sa.String),
    sa.column("job_type", sa.String),
    sa.column("payload", sa.JSON),
    sa.column("status", sa.String),
    sa.column("run_after", sa.DateTime(timezone=True)),
    sa.column("attempts", sa.Integer),
    sa.column("last_error", sa.Text),
    sa.column("dedupe_key", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
watch_state_entries = sa.table(
    "watch_state_entries",
    sa.column("user_id", sa.String),
    sa.column("category", sa.String),
    sa.column("episode_item_id", sa.String),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("payload", sa.JSON),
)


@dataclass(frozen=True)
class ImportEvent:
    event_id: str
    video_id: str
    target_id: str | None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    repair(bind)


def downgrade() -> None:
    # Data repair only; the previous (incorrect) episode links are not restored.
    pass


def repair(bind: sa.Connection) -> int:
    """Repair all affected groups; returns the number of events repointed."""
    now = datetime.now(timezone.utc)
    rows = bind.execute(
        sa.select(
            watch_events.c.id,
            watch_events.c.user_id,
            watch_events.c.episode_item_id,
            watch_events.c.occurred_at,
            watch_events.c.raw,
            episode_items.c.show_media_item_id,
            episode_items.c.season_number,
            episode_items.c.episode_number,
        )
        .join(episode_items, episode_items.c.id == watch_events.c.episode_item_id)
        .where(watch_events.c.event_type == "stremio_imported")
    ).all()

    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        raw = json.loads(row.raw) if isinstance(row.raw, str) else row.raw
        video_id = raw.get("video_id") if isinstance(raw, dict) else None
        match = VIDEO_ID_RE.match(video_id or "")
        if match:
            groups[(row.user_id, row.episode_item_id, row.occurred_at)].append((row, video_id, match))

    repointed = 0
    for (user_id, current_id, occurred_at), members in groups.items():
        # The bug always wrote several events per batch; a lone mismatch may be a
        # legitimate numbering difference and is left alone.
        if len(members) < 2:
            continue
        if all((int(m[1]), int(m[2])) == (row.season_number, row.episode_number) for row, _, m in members):
            continue
        events = [
            ImportEvent(row.id, video_id, _episode_id(bind, row.show_media_item_id, int(m[1]), int(m[2])))
            for row, video_id, m in members
        ]
        repointed += _repair_group(bind, user_id, current_id, occurred_at, events, now)
    return repointed


def _episode_id(bind: sa.Connection, show_id: str, season: int, episode: int) -> str | None:
    return bind.execute(
        sa.select(episode_items.c.id).where(
            episode_items.c.show_media_item_id == show_id,
            episode_items.c.season_number == season,
            episode_items.c.episode_number == episode,
        )
    ).scalar()


def _repair_group(
    bind: sa.Connection,
    user_id: str,
    current_id: str,
    occurred_at: datetime,
    events: list[ImportEvent],
    now: datetime,
) -> int:
    rows = bind.execute(
        sa.select(watched_items.c.id, watch_syncs.c.external_id)
        .select_from(
            watched_items.outerjoin(
                watch_syncs,
                sa.and_(watch_syncs.c.watched_item_id == watched_items.c.id, watch_syncs.c.provider == "stremio"),
            )
        )
        .where(
            watched_items.c.user_id == user_id,
            watched_items.c.episode_item_id == current_id,
            watched_items.c.watched_at == occurred_at,
            watched_items.c.source == "stremio",
        )
        .order_by(watched_items.c.id)
    ).all()
    # Unclaimed watches on the linked episode -> their Stremio video id (if any).
    available: dict[str, str | None] = {}
    for watched_id, external_id in rows:
        available.setdefault(watched_id, external_id)

    def take(video_id: str) -> str | None:
        for watched_id, external_id in available.items():
            if external_id == video_id:
                del available[watched_id]
                return watched_id
        return None

    resolvable = [event for event in events if event.target_id is not None]
    # Events that belong to the linked episode keep a watch there first, so the merge
    # survivor (whose downstream deliveries were made for this episode) stays put.
    for event in [event for event in resolvable if event.target_id == current_id]:
        watched_id = take(event.video_id)
        if watched_id is None and available:
            watched_id = next(iter(available))
            del available[watched_id]
            bind.execute(
                watch_syncs.update()
                .where(watch_syncs.c.watched_item_id == watched_id, watch_syncs.c.provider == "stremio")
                .values(external_id=event.video_id, updated_at=now)
            )
    for event in [event for event in resolvable if event.target_id != current_id]:
        watched_id = take(event.video_id)
        if watched_id is not None:
            _move_watch(bind, user_id, watched_id, event.target_id, now)
        elif _may_create_watch(bind, user_id, event.target_id, occurred_at):
            _create_watch(bind, user_id, event, occurred_at, now)
    for event in resolvable:
        bind.execute(
            watch_events.update().where(watch_events.c.id == event.event_id).values(episode_item_id=event.target_id)
        )
    return len(resolvable)


def _move_watch(bind: sa.Connection, user_id: str, watched_id: str, target_id: str, now: datetime) -> None:
    bind.execute(watched_items.update().where(watched_items.c.id == watched_id).values(episode_item_id=target_id))
    # Downstream providers received the wrong episode: forget those deliveries,
    # cancel anything still queued and re-deliver the corrected watch.
    bind.execute(
        watch_syncs.delete().where(watch_syncs.c.watched_item_id == watched_id, watch_syncs.c.provider != "stremio")
    )
    queued = bind.execute(
        sa.select(outbox.c.id, outbox.c.payload).where(
            outbox.c.user_id == user_id, outbox.c.status.in_(("pending", "failed_retryable"))
        )
    ).all()
    for job_id, payload in queued:
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(payload, dict) and payload.get("watched_item_id") == watched_id:
            bind.execute(
                outbox.update()
                .where(outbox.c.id == job_id)
                .values(
                    status="failed_permanent",
                    dedupe_key=None,
                    run_after=None,
                    last_error="Superseded by Stremio episode repair",
                    updated_at=now,
                )
            )
    _enqueue_delivery(bind, user_id, watched_id, now)


def _may_create_watch(bind: sa.Connection, user_id: str, target_id: str, occurred_at: datetime) -> bool:
    exists = bind.execute(
        sa.select(watched_items.c.id).where(
            watched_items.c.user_id == user_id,
            watched_items.c.episode_item_id == target_id,
            watched_items.c.watched_at == occurred_at,
        )
    ).first()
    if exists is not None:
        return False
    deleted = bind.execute(
        sa.select(watch_events.c.id).where(
            watch_events.c.user_id == user_id,
            watch_events.c.episode_item_id == target_id,
            watch_events.c.event_type == "manual_watched_deleted",
        )
    ).first()
    if deleted is not None:
        return False
    # Respect an explicit Watch State "unplayed" clear made after this watch.
    cleared = bind.execute(
        sa.select(watch_state_entries.c.payload).where(
            watch_state_entries.c.user_id == user_id,
            watch_state_entries.c.category == "watched",
            watch_state_entries.c.episode_item_id == target_id,
            watch_state_entries.c.occurred_at >= occurred_at,
        )
    ).all()
    for (payload,) in cleared:
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(payload, dict) and payload.get("event") == "unplayed":
            return False
    return True


def _create_watch(bind: sa.Connection, user_id: str, event: ImportEvent, occurred_at: datetime, now: datetime) -> None:
    watched_id = str(uuid.uuid4())
    bind.execute(
        watched_items.insert().values(
            id=watched_id,
            user_id=user_id,
            media_item_id=None,
            episode_item_id=event.target_id,
            watched_at=occurred_at,
            source="stremio",
            created_at=now,
        )
    )
    bind.execute(
        watch_syncs.insert().values(
            id=str(uuid.uuid4()),
            user_id=user_id,
            watched_item_id=watched_id,
            provider="stremio",
            status="synced_from_stremio",
            is_rewatch=False,
            external_id=event.video_id,
            last_synced_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    _enqueue_delivery(bind, user_id, watched_id, now)


def _enqueue_delivery(bind: sa.Connection, user_id: str, watched_id: str, now: datetime) -> None:
    bind.execute(
        outbox.insert().values(
            id=str(uuid.uuid4()),
            user_id=user_id,
            target_provider="internal",
            job_type="new_item_added",
            payload={"watched_item_id": watched_id, "is_rewatch": False},
            status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )
