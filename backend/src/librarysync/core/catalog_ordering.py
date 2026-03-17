from __future__ import annotations

from datetime import date
from typing import Literal

from sqlalchemy import Float, cast, func, select
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import Select

from librarysync.core.release_dates import get_release_now_date
from librarysync.db.models import EpisodeItem, WatchedItem

CatalogOrderBy = Literal[
    "date_added",
    "release_date",
    "last_watched",
    "episodes_left",
    "progress",
    "last_episode_air_date",
    "next_episode_air_date",
]
CatalogOrderDirection = Literal["asc", "desc"]


def apply_catalog_ordering(
    query: Select,
    *,
    order_by: CatalogOrderBy,
    order_dir: CatalogOrderDirection,
    user_id: str,
    date_added_col: ColumnElement,
    release_date_col: ColumnElement,
    base_media_id_col: ColumnElement,
    last_watched_col: ColumnElement | None = None,
    tie_breaker_col: ColumnElement | None = None,
    now_date: date | None = None,
) -> Select:
    order_expression: ColumnElement
    updated_query = query
    if order_by == "date_added":
        order_expression = date_added_col
    elif order_by == "release_date":
        order_expression = release_date_col
    elif order_by == "last_watched" and last_watched_col is not None:
        order_expression = last_watched_col
    elif order_by == "last_watched":
        last_watched_subq = _build_last_watched_subquery(user_id)
        updated_query = updated_query.outerjoin(
            last_watched_subq,
            last_watched_subq.c.media_item_id == base_media_id_col,
        )
        order_expression = last_watched_subq.c.last_watched_at
    elif order_by in {"episodes_left", "progress"}:
        now_date = now_date or get_release_now_date()
        progress_subq = build_show_progress_subquery(user_id, now_date)
        updated_query = updated_query.outerjoin(
            progress_subq,
            progress_subq.c.media_item_id == base_media_id_col,
        )
        if order_by == "episodes_left":
            order_expression = progress_subq.c.total_released - progress_subq.c.watched_count
        else:
            order_expression = cast(progress_subq.c.watched_count, Float) / func.nullif(
                progress_subq.c.total_released, 0
            )
    elif order_by == "last_episode_air_date":
        now_date = now_date or get_release_now_date()
        last_episode_subq = _build_last_episode_air_subquery(now_date)
        updated_query = updated_query.outerjoin(
            last_episode_subq,
            last_episode_subq.c.media_item_id == base_media_id_col,
        )
        order_expression = last_episode_subq.c.last_episode_air_date
    elif order_by == "next_episode_air_date":
        now_date = now_date or get_release_now_date()
        next_episode_subq = _build_next_episode_air_subquery(now_date)
        updated_query = updated_query.outerjoin(
            next_episode_subq,
            next_episode_subq.c.media_item_id == base_media_id_col,
        )
        order_expression = next_episode_subq.c.next_episode_air_date
    else:
        order_expression = date_added_col

    if order_dir == "asc":
        order_clause = order_expression.asc().nulls_last()
    else:
        order_clause = order_expression.desc().nulls_last()

    if tie_breaker_col is not None:
        return updated_query.order_by(order_clause, tie_breaker_col)
    return updated_query.order_by(order_clause)


def _build_last_watched_subquery(user_id: str):
    media_item_id = func.coalesce(
        WatchedItem.media_item_id, EpisodeItem.show_media_item_id
    ).label("media_item_id")
    return (
        select(
            media_item_id,
            func.max(WatchedItem.watched_at).label("last_watched_at"),
        )
        .select_from(WatchedItem)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(WatchedItem.user_id == user_id)
        .group_by(media_item_id)
        .subquery()
    )


def build_show_progress_subquery(user_id: str, now_date: date):
    base = (
        select(EpisodeItem.show_media_item_id.label("media_item_id"))
        .where(EpisodeItem.show_media_item_id.is_not(None))
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    released_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(EpisodeItem.id).label("total_released"),
        )
        .where(
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    watched_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(func.distinct(WatchedItem.episode_item_id)).label("watched_count"),
        )
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id.is_(None),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    return (
        select(
            base.c.media_item_id,
            func.coalesce(released_subq.c.total_released, 0).label("total_released"),
            func.coalesce(watched_subq.c.watched_count, 0).label("watched_count"),
        )
        .select_from(base)
        .outerjoin(released_subq, released_subq.c.media_item_id == base.c.media_item_id)
        .outerjoin(watched_subq, watched_subq.c.media_item_id == base.c.media_item_id)
        .subquery()
    )


def _build_last_episode_air_subquery(now_date: date):
    return (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.max(EpisodeItem.air_date).label("last_episode_air_date"),
        )
        .where(
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )


def _build_next_episode_air_subquery(now_date: date):
    return (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.min(EpisodeItem.air_date).label("next_episode_air_date"),
        )
        .where(
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date > now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
