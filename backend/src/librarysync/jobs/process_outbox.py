"""Process outbox jobs for downstream services."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.simkl import (
    SimklClient,
    SimklError,
    has_required_simkl_fields,
)
from librarysync.connectors.services.simkl import (
    is_token_expired as is_simkl_token_expired,
)
from librarysync.connectors.services.simkl import (
    parse_expires_at as parse_simkl_expires_at,
)
from librarysync.connectors.services.simkl import (
    token_to_secret_payload as simkl_token_to_secret_payload,
)
from librarysync.connectors.services.trakt import (
    TraktClient,
    TraktError,
    has_required_trakt_fields,
    is_token_expired,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import coerce_star_rating
from librarysync.core.security import encrypt_value
from librarysync.core.watch_pipeline import process_new_item_job
from librarysync.db.models import (
    IntegrationSecret,
    OutboxJob,
    SyncAttempt,
    WatchedItem,
    WatchSync,
)
from librarysync.db.session import SessionLocal, init_session_factory

RETRYABLE_STATUSES = ("pending", "failed_retryable")
logger = logging.getLogger(__name__)


async def process_outbox_once(limit: int = 10) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        jobs = await _claim_jobs(db, limit)
        if not jobs:
            return 0
        logger.info("Processing %s outbox job(s)", len(jobs))
        for job in jobs:
            await _process_job(db, job)
        return len(jobs)


async def _claim_jobs(db: AsyncSession, limit: int) -> list[OutboxJob]:
    now = datetime.now(timezone.utc)
    async with db.begin():
        result = await db.execute(
            select(OutboxJob)
            .where(
                OutboxJob.status.in_(RETRYABLE_STATUSES),
                or_(OutboxJob.run_after.is_(None), OutboxJob.run_after <= now),
            )
            .order_by(OutboxJob.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        jobs = result.scalars().all()
        for job in jobs:
            job.status = "in_progress"
            job.updated_at = now
    return jobs


async def _process_job(db: AsyncSession, job: OutboxJob) -> None:
    now = datetime.now(timezone.utc)
    job.attempts += 1
    status = "succeeded"
    response_code: int | None = None
    error_message: str | None = None
    external_id: str | None = None
    resolved_rewatch: bool | None = None

    try:
        if job.target_provider == "letterboxd" and job.job_type in {
            "push_watched",
            "push_rating",
            "update_log_entry",
        }:
            if job.job_type == "update_log_entry":
                response_code, external_id = await _deliver_letterboxd_log_update(db, job)
            else:
                (
                    response_code,
                    external_id,
                    resolved_rewatch,
                ) = await _deliver_letterboxd_watch(
                    db, job, force_update_rating=job.job_type == "push_rating"
                )
        elif job.target_provider == "trakt" and job.job_type in {
            "push_watched",
            "push_rating",
            "update_history",
        }:
            if job.job_type == "push_rating":
                response_code, external_id = await _deliver_trakt_rating(db, job)
            elif job.job_type == "update_history":
                response_code, external_id = await _deliver_trakt_update(db, job)
            else:
                response_code, external_id = await _deliver_trakt_watch(db, job)
        elif job.target_provider == "simkl" and job.job_type in {
            "push_watched",
            "push_rating",
            "update_history",
        }:
            if job.job_type == "push_rating":
                response_code, external_id = await _deliver_simkl_rating(db, job)
            elif job.job_type == "update_history":
                response_code, external_id = await _deliver_simkl_update(db, job)
            else:
                response_code, external_id = await _deliver_simkl_watch(db, job)
        elif job.target_provider == "internal" and job.job_type == "new_item_added":
            await process_new_item_job(db, job)
        else:
            raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")
    except LetterboxdError as exc:
        response_code = exc.status_code
        error_message = _format_letterboxd_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except TraktError as exc:
        response_code = exc.status_code
        error_message = _format_trakt_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except SimklError as exc:
        response_code = exc.status_code
        error_message = _format_simkl_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except ValueError as exc:
        error_message = str(exc)
        status = "failed_permanent"
    except Exception as exc:
        error_message = str(exc)
        status = "failed_retryable"

    job.status = status
    job.last_error = error_message
    if status == "failed_retryable":
        job.run_after = now + _next_retry_delay(job.attempts)
    else:
        job.run_after = None
    job.updated_at = now

    await _update_watch_sync(
        db,
        job,
        status,
        error_message,
        external_id,
        now,
        resolved_rewatch,
    )
    logger.info(
        "Outbox job %s %s -> %s (attempt %s)",
        job.id,
        f"{job.target_provider}:{job.job_type}",
        status,
        job.attempts,
    )
    if error_message:
        logger.warning("Outbox job %s error: %s", job.id, error_message)
    db.add(
        SyncAttempt(
            job_id=job.id,
            status=status,
            response_code=response_code,
            error=error_message,
        )
    )
    await db.commit()


async def _deliver_letterboxd_watch(
    db: AsyncSession, job: OutboxJob, force_update_rating: bool = False
) -> tuple[int | None, str | None, bool | None]:
    payload = job.payload or {}
    imdb_id = payload.get("imdb_id")
    tmdb_id = payload.get("tmdb_id")
    rating = coerce_star_rating(payload.get("rating"))
    tags = _normalize_tags(payload.get("tags"))
    like = _normalize_like(payload.get("like"))
    watched_at_raw = payload.get("watched_at")
    is_rewatch = bool(payload.get("is_rewatch"))
    entry_id = payload.get("entry_id")
    force_update_rating = force_update_rating or bool(payload.get("force_update_rating"))
    imdb_id = imdb_id.lower() if isinstance(imdb_id, str) and imdb_id else None
    tmdb_id = str(tmdb_id).strip() if tmdb_id is not None else None
    if tmdb_id == "":
        tmdb_id = None
    if not imdb_id and not tmdb_id:
        raise ValueError("Letterboxd sync requires an IMDb or TMDB ID")

    watched_at = _parse_datetime(watched_at_raw)
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await client.refresh_access_token()
    film_id = await client.resolve_film_id(access_token, imdb_id, tmdb_id)
    if force_update_rating and rating is not None and entry_id:
        _, response_code = await client.update_log_entry_rating(
            str(entry_id), rating, access_token=access_token
        )
        return response_code, str(entry_id), None

    log_check = await client.check_log_entries_for_date(
        access_token,
        film_id,
        watched_at.date(),
    )
    if log_check.already_logged_today:
        if force_update_rating and rating is not None and log_check.entry_id:
            _, response_code = await client.update_log_entry_rating(
                log_check.entry_id,
                rating,
                access_token=access_token,
            )
            return response_code, log_check.entry_id, None
        return 200, log_check.entry_id, None
    effective_rewatch = is_rewatch or log_check.has_any_entries
    response, response_code = await client.log_watch(
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        watched_at=watched_at,
        rewatch=effective_rewatch,
        rating=rating,
        tags=tags,
        like=like,
        access_token=access_token,
        film_id=film_id,
    )
    external_id = _extract_entry_id(response)
    return response_code, external_id, effective_rewatch


async def _deliver_letterboxd_log_update(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    entry_id = payload.get("entry_id")
    if not entry_id:
        raise ValueError("Letterboxd update requires a log entry id")
    watched_at = None
    if "watched_at" in payload and payload.get("watched_at") is not None:
        watched_at = _parse_datetime(payload.get("watched_at"))
    rating = None
    if "rating" in payload and payload.get("rating") is not None:
        rating = coerce_star_rating(payload.get("rating"))
    tags = _normalize_tags(payload.get("tags"))
    like = _normalize_like(payload.get("like"))
    if watched_at is None and rating is None and tags is None and like is None:
        raise ValueError("Letterboxd update requires at least one field to change")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await client.refresh_access_token()
    response, response_code = await client.update_log_entry(
        str(entry_id),
        watched_at=watched_at,
        rating=rating,
        tags=tags,
        like=like,
        access_token=access_token,
    )
    return response_code, str(entry_id)


async def _deliver_trakt_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    existing_history_id = None
    existing_watched_at = None
    try:
        existing = await _find_trakt_history_for_day(
            client, access_token, payload, watched_at
        )
        if existing:
            existing_history_id, existing_watched_at = existing
    except TraktError as exc:
        logger.info("Trakt history precheck failed: %s", exc)
    if existing_history_id:
        if existing_watched_at:
            await _sync_local_watched_at(db, payload, existing_watched_at)
        return 200, existing_history_id
    history_payload = _build_trakt_history_payload(payload, watched_at)
    response, response_code = await client.add_history(history_payload, access_token)
    external_id = _extract_trakt_history_id(response, _coerce_str(payload.get("media_type")))
    return response_code, external_id


async def _deliver_trakt_rating(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    rating = _normalize_trakt_rating(payload.get("rating"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    ratings_payload = _build_trakt_rating_payload(payload, rating)
    response, response_code = await client.add_ratings(ratings_payload, access_token)
    external_id = _extract_trakt_history_id(response, _coerce_str(payload.get("media_type")))
    return response_code, external_id


async def _deliver_trakt_update(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    history_id = _coerce_str(payload.get("history_id")) or _coerce_str(payload.get("external_id"))
    if not history_id:
        return await _deliver_trakt_watch(db, job)
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    try:
        _, response_code = await client.update_history(history_id, watched_at, access_token)
        return response_code, history_id
    except TraktError as exc:
        if exc.status_code in {404, 405}:
            history_payload = _build_trakt_history_payload(payload, watched_at)
            response, response_code = await client.add_history(history_payload, access_token)
            external_id = _extract_trakt_history_id(
                response, _coerce_str(payload.get("media_type"))
            )
            return response_code, external_id
        raise


async def _deliver_simkl_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    history_payload = _build_simkl_history_payload(payload, watched_at)
    response, response_code = await client.add_history(history_payload, access_token)
    external_id = _extract_simkl_history_id(response)
    return response_code, external_id


async def _deliver_simkl_rating(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    rating = _normalize_simkl_rating(payload.get("rating"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    ratings_payload = _build_simkl_rating_payload(payload, rating)
    response, response_code = await client.add_ratings(ratings_payload, access_token)
    external_id = _extract_simkl_history_id(response)
    return response_code, external_id


async def _deliver_simkl_update(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    return await _deliver_simkl_watch(db, job)


def _build_trakt_history_payload(
    payload: dict[str, object], watched_at: datetime
) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    watched_at_value = watched_at.isoformat()
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("Trakt sync requires movie ids")
        return {
            "movies": [
                {
                    "ids": movie_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [
                                {
                                    "number": episode_number,
                                    "watched_at": watched_at_value,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    if episode_ids:
        return {
            "episodes": [
                {
                    "ids": episode_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }
    raise ValueError("Trakt sync requires show or episode ids")


def _build_simkl_history_payload(
    payload: dict[str, object], watched_at: datetime
) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    watched_at_value = watched_at.isoformat()
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("SIMKL sync requires movie ids")
        return {
            "movies": [
                {
                    "ids": movie_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    episode_ids = _normalize_simkl_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [
                                {
                                    "number": episode_number,
                                    "watched_at": watched_at_value,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    if episode_ids:
        return {
            "episodes": [
                {
                    "ids": episode_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }
    raise ValueError("SIMKL sync requires show or episode ids")


def _build_trakt_rating_payload(payload: dict[str, object], rating: int) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("Trakt rating requires movie ids")
        return {"movies": [{"ids": movie_ids, "rating": rating}]}

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids, "rating": rating}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number, "rating": rating}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("Trakt rating requires show or episode ids")


def _build_simkl_rating_payload(payload: dict[str, object], rating: int) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("SIMKL rating requires movie ids")
        return {"movies": [{"ids": movie_ids, "rating": rating}]}

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    episode_ids = _normalize_simkl_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids, "rating": rating}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number, "rating": rating}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("SIMKL rating requires show or episode ids")


def _normalize_trakt_ids(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    ids: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "trakt"):
        if key not in value:
            continue
        entry = value.get(key)
        if entry is None or entry == "":
            continue
        if key == "imdb":
            ids[key] = str(entry).lower()
        else:
            ids[key] = entry
    return ids


def _normalize_simkl_ids(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    ids: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "simkl"):
        if key not in value:
            continue
        entry = value.get(key)
        if entry is None or entry == "":
            continue
        if key == "imdb":
            ids[key] = str(entry).lower()
        else:
            ids[key] = entry
    return ids


def _normalize_trakt_rating(value: object) -> int:
    rating = coerce_star_rating(value)
    if rating is None:
        raise ValueError("Trakt rating must be between 0.5 and 5.0 stars")
    normalized = int(round(rating * 2))
    if normalized < 1 or normalized > 10:
        raise ValueError("Trakt rating must be between 1 and 10")
    return normalized


def _normalize_simkl_rating(value: object) -> int:
    rating = coerce_star_rating(value)
    if rating is None:
        raise ValueError("SIMKL rating must be between 0.5 and 5.0 stars")
    normalized = int(round(rating * 2))
    if normalized < 1 or normalized > 10:
        raise ValueError("SIMKL rating must be between 1 and 10")
    return normalized


async def _find_trakt_history_for_day(
    client: TraktClient,
    access_token: str,
    payload: dict[str, object],
    watched_at: datetime,
) -> tuple[str, datetime] | None:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    history_type = "movies" if media_type == "movie" else "episodes"
    start_at, end_at = _day_bounds(watched_at)
    page = 1
    limit = 50
    target_date = watched_at.date()
    while True:
        items, headers = await client.fetch_history(
            access_token,
            history_type=history_type,
            start_at=start_at,
            end_at=end_at,
            page=page,
            limit=limit,
        )
        if not items:
            return None
        for entry in items:
            matched_watched_at = _match_trakt_entry_for_payload(
                entry, payload, media_type, target_date
            )
            if matched_watched_at:
                history_id = _coerce_str(entry.get("id"))
                if history_id:
                    return history_id, matched_watched_at
        page_count = _parse_trakt_page_count(headers)
        if page_count is not None and page >= page_count:
            break
        if len(items) < limit:
            break
        page += 1
    return None


def _day_bounds(value: datetime) -> tuple[datetime, datetime]:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    start_at = value.replace(hour=0, minute=0, second=0, microsecond=0)
    end_at = start_at + timedelta(days=1)
    return start_at, end_at


def _match_trakt_entry_for_payload(
    entry: object,
    payload: dict[str, object],
    media_type: str,
    target_date: date,
) -> datetime | None:
    if not isinstance(entry, dict):
        return None
    entry_watched_at = _parse_trakt_datetime(entry.get("watched_at"))
    if not entry_watched_at or entry_watched_at.date() != target_date:
        return None
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        entry_movie_ids = _extract_trakt_entry_ids(entry.get("movie"))
        if _ids_match(movie_ids, entry_movie_ids):
            return entry_watched_at
        return None

    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    entry_episode = entry.get("episode") if isinstance(entry.get("episode"), dict) else {}
    entry_show = entry.get("show") if isinstance(entry.get("show"), dict) else {}
    if episode_ids:
        if _ids_match(episode_ids, _extract_trakt_entry_ids(entry_episode)):
            return entry_watched_at
    if show_ids and _ids_match(show_ids, _extract_trakt_entry_ids(entry_show)):
        season_number = _coerce_int(payload.get("season_number"))
        episode_number = _coerce_int(payload.get("episode_number"))
        if season_number is None and episode_number is None:
            return entry_watched_at
        entry_season = _coerce_int(entry_episode.get("season"))
        entry_number = _coerce_int(entry_episode.get("number"))
        if season_number is not None and entry_season != season_number:
            return None
        if episode_number is not None and entry_number != episode_number:
            return None
        return entry_watched_at
    return None


def _extract_trakt_entry_ids(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        return {}
    ids = entry.get("ids")
    if not isinstance(ids, dict):
        return {}
    normalized: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "trakt"):
        value = ids.get(key)
        if value is None or value == "":
            continue
        if key == "imdb":
            normalized[key] = str(value).lower()
        else:
            normalized[key] = value
    return normalized


def _ids_match(left: dict[str, object], right: dict[str, object]) -> bool:
    if not left or not right:
        return False
    for key in ("trakt", "imdb", "tmdb", "tvdb"):
        if key not in left or key not in right:
            continue
        left_value = left[key]
        right_value = right[key]
        if key == "imdb":
            if str(left_value).lower() == str(right_value).lower():
                return True
        else:
            if str(left_value) == str(right_value):
                return True
    return False


def _parse_trakt_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_trakt_page_count(headers: dict[str, str]) -> int | None:
    value = headers.get("x-pagination-page-count") or headers.get(
        "X-Pagination-Page-Count"
    )
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _sync_local_watched_at(
    db: AsyncSession, payload: dict[str, object], watched_at: datetime
) -> None:
    watched_item_id = _coerce_str(payload.get("watched_item_id"))
    if not watched_item_id:
        return
    watched = await db.get(WatchedItem, watched_item_id)
    if not watched:
        return
    if watched_at.tzinfo is None:
        watched_at = watched_at.replace(tzinfo=timezone.utc)
    if watched.watched_at != watched_at:
        watched.watched_at = watched_at
        db.add(watched)


async def _ensure_trakt_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: TraktClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise TraktError("Trakt access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TraktError("Trakt refresh token is missing", status_code=401)
    expires_at = parse_expires_at(secret_data.get("expires_at"))
    if not is_token_expired(expires_at):
        return access_token
    token = await client.refresh_access_token(refresh_token)
    updated = dict(secret_data)
    updated.update(token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


async def _ensure_simkl_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: SimklClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise SimklError("SIMKL access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        return access_token
    expires_at = parse_simkl_expires_at(secret_data.get("expires_at"))
    if not is_simkl_token_expired(expires_at):
        return access_token
    token = await client.refresh_access_token(refresh_token)
    updated = dict(secret_data)
    updated.update(simkl_token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


async def _save_integration_secret(
    db: AsyncSession, integration_id: str, secret_data: dict[str, object]
) -> None:
    encrypted = encrypt_value(json.dumps(secret_data))
    result = await db.execute(
        select(IntegrationSecret).where(IntegrationSecret.integration_id == integration_id)
    )
    secret = result.scalars().first()
    if not secret:
        secret = IntegrationSecret(integration_id=integration_id, secret_data=encrypted)
    else:
        secret.secret_data = encrypted
    db.add(secret)


def _extract_trakt_history_id(payload: object, media_type: str | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    history = payload.get("history")
    if isinstance(history, dict):
        key = "movies" if media_type == "movie" else "episodes"
        entries = history.get(key)
        if isinstance(entries, list) and entries:
            return _coerce_str(entries[0])
        for fallback in ("movies", "episodes"):
            entries = history.get(fallback)
            if isinstance(entries, list) and entries:
                return _coerce_str(entries[0])
    if isinstance(history, list) and history:
        return _coerce_str(history[0])
    for key in ("id", "history_id", "historyId"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _extract_simkl_history_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("history", "id", "history_id", "historyId"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    ids = payload.get("ids")
    if isinstance(ids, dict):
        for key in ("simkl", "imdb", "tmdb", "tvdb"):
            value = ids.get(key)
            if isinstance(value, (str, int)):
                return str(value)
    return None


async def _update_watch_sync(
    db: AsyncSession,
    job: OutboxJob,
    status: str,
    error_message: str | None,
    external_id: str | None,
    now: datetime,
    resolved_rewatch: bool | None = None,
) -> None:
    payload = job.payload or {}
    watch_sync_id = payload.get("watch_sync_id")
    if not watch_sync_id:
        return
    result = await db.execute(select(WatchSync).where(WatchSync.id == str(watch_sync_id)))
    watch_sync = result.scalars().first()
    if not watch_sync:
        return
    if resolved_rewatch is not None:
        watch_sync.is_rewatch = resolved_rewatch
    watch_sync.status = status
    watch_sync.last_error = error_message
    if status == "succeeded":
        watch_sync.last_synced_at = now
        if external_id:
            watch_sync.external_id = external_id
    watch_sync.updated_at = now


def _classify_failure(status_code: int | None, message: str | None) -> str:
    if status_code in {400, 401, 403, 404, 405, 409, 422}:
        return "failed_permanent"
    if message and "unsupported" in message.lower():
        return "failed_permanent"
    return "failed_retryable"


def _next_retry_delay(attempts: int) -> timedelta:
    delay = min(60 * (2 ** max(attempts - 1, 0)), 3600)
    return timedelta(seconds=delay)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(timezone.utc)


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _normalize_tags(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [entry.strip() for entry in value.split(",")]
    elif isinstance(value, list):
        items = [str(entry).strip() for entry in value]
    else:
        return None
    tags = [entry for entry in items if entry]
    return tags or None


def _normalize_like(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes"}:
            return True
        if cleaned in {"false", "0", "no"}:
            return False
    return None


def _safe_cookies(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    cookies: dict[str, str] = {}
    for key, entry in value.items():
        if entry is None:
            continue
        cookies[str(key)] = str(entry)
    return cookies or None


def _extract_entry_id(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "entryId", "diaryEntryId", "logEntryId"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        entry = payload.get("entry")
        if isinstance(entry, dict):
            for key in ("id", "entryId", "logEntryId"):
                value = entry.get(key)
                if isinstance(value, (str, int)):
                    return str(value)
    return None


def _format_letterboxd_error(error: LetterboxdError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_trakt_error(error: TraktError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_simkl_error(error: SimklError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _shorten(value: str, limit: int = 300) -> str:
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
