"""Bridge explicit history edits into the independent Watch State projection."""

import uuid
from datetime import datetime, timezone

from librarysync.core.stremio_addon import resolve_meta_id
from librarysync.core.watch_state_events import _write_entry


async def record_manual_state(db, watched, media, episode, *, watch_changed=False, rating_changed=False):
    if not media:
        return
    meta_id = resolve_meta_id(media)
    if not meta_id:
        return
    from librarysync.core.watch_state_pull import _video_id

    body = {
        "id": str(uuid.uuid4()),
        "at": int(datetime.now(timezone.utc).timestamp()),
        "metaId": meta_id,
        "scope": "episode" if episode else "movie",
        "videoId": _video_id(meta_id, episode) if episode else None,
        "season": episode.season_number if episode else None,
        "episode": episode.episode_number if episode else None,
        "event": "played",
    }
    if not episode and media.media_type != "movie":
        body["scope"] = "series"
        watch_changed = False
    if watch_changed:
        await _write_entry(db, watched.user_id, "watched", media, episode, body)
        await _write_entry(db, watched.user_id, "playback", media, episode, body)
    if rating_changed:
        body.update(
            event="rated" if watched.rating is not None else "unrated",
            rating=watched.rating * 2 if watched.rating is not None else None,
        )
        entry = await _write_entry(db, watched.user_id, "rating", media, episode, body)
        if entry:
            from librarysync.core.watch_state_ratings import enqueue_rating_delivery

            await enqueue_rating_delivery(db, entry, media, episode)
