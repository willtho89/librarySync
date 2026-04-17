from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

TMDB_LIST_ID_RE = re.compile(r"^(\d+)")


@dataclass(frozen=True)
class TraktListRef:
    username: str | None
    list_id: str
    url: str
    external_id: str
    name: str


@dataclass(frozen=True)
class LetterboxdListRef:
    username: str
    slug: str
    url: str
    external_id: str
    name: str


@dataclass(frozen=True)
class TmdbListRef:
    list_id: str
    url: str
    external_id: str
    name: str


@dataclass(frozen=True)
class TmdbChartRef:
    media_type: str
    chart_slug: str
    url: str
    external_id: str
    name: str


@dataclass(frozen=True)
class TvdbListRef:
    list_id: str
    url: str
    external_id: str
    name: str


@dataclass(frozen=True)
class ImdbChartRef:
    chart_slug: str
    url: str


@dataclass(frozen=True)
class MdblistRef:
    username: str
    slug: str
    url: str
    external_id: str
    name: str


def parse_trakt_list_urls(urls: Iterable[str]) -> list[TraktListRef]:
    refs: list[TraktListRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 4 and segments[0] == "users" and segments[2] == "lists":
            username = segments[1]
            list_id = segments[3]
            external_id = f"user:{username}:{list_id}"
            refs.append(
                TraktListRef(
                    username=username,
                    list_id=list_id,
                    url=cleaned,
                    external_id=external_id,
                    name=list_id,
                )
            )
            continue
        if len(segments) >= 2 and segments[0] == "lists":
            list_id = segments[1]
            external_id = f"list:{list_id}"
            refs.append(
                TraktListRef(
                    username=None,
                    list_id=list_id,
                    url=cleaned,
                    external_id=external_id,
                    name=list_id,
                )
            )
    return refs


def parse_letterboxd_list_urls(urls: Iterable[str]) -> list[LetterboxdListRef]:
    refs: list[LetterboxdListRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"letterboxd.com", "www.letterboxd.com"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 3 and segments[1] == "list":
            username = segments[0]
            slug = segments[2]
            external_id = f"{username}:{slug}"
            refs.append(
                LetterboxdListRef(
                    username=username,
                    slug=slug,
                    url=cleaned,
                    external_id=external_id,
                    name=slug,
                )
            )
    return refs


def parse_tmdb_list_urls(urls: Iterable[str]) -> list[TmdbListRef]:
    refs: list[TmdbListRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"themoviedb.org", "www.themoviedb.org"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        match = (
            TMDB_LIST_ID_RE.match(segments[1])
            if len(segments) >= 2 and segments[0] == "list"
            else None
        )
        if match:
            list_id = match.group(1)
            refs.append(
                TmdbListRef(
                    list_id=list_id,
                    url=cleaned,
                    external_id=f"tmdb:{list_id}",
                    name=list_id,
                )
            )
    return refs


def parse_tmdb_chart_urls(urls: Iterable[str]) -> list[TmdbChartRef]:
    refs: list[TmdbChartRef] = []
    chart_names = {
        ("movie", "top-rated"): "Top Rated Movies",
        ("movie", "popular"): "Popular Movies",
        ("movie", "now-playing"): "Now Playing Movies",
        ("movie", "upcoming"): "Upcoming Movies",
        ("tv", "top-rated"): "Top Rated Series",
        ("tv", "popular"): "Popular Series",
        ("tv", "on-the-air"): "On The Air Series",
        ("tv", "airing-today"): "Airing Today Series",
    }
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"themoviedb.org", "www.themoviedb.org"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) < 2:
            continue
        media_type = segments[0]
        chart_slug = segments[1]
        name = chart_names.get((media_type, chart_slug))
        if not name:
            continue
        refs.append(
            TmdbChartRef(
                media_type=media_type,
                chart_slug=chart_slug,
                url=cleaned,
                external_id=f"tmdb-chart:{media_type}:{chart_slug}",
                name=name,
            )
        )
    return refs


def parse_tvdb_list_urls(urls: Iterable[str]) -> list[TvdbListRef]:
    refs: list[TvdbListRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"thetvdb.com", "www.thetvdb.com"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2 and segments[0] == "lists":
            list_id = segments[1]
            refs.append(
                TvdbListRef(
                    list_id=list_id,
                    url=cleaned,
                    external_id=f"tvdb:{list_id}",
                    name=list_id,
                )
            )
    return refs


def parse_imdb_chart_urls(urls: Iterable[str]) -> list[ImdbChartRef]:
    refs: list[ImdbChartRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"imdb.com", "www.imdb.com"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            continue
        if len(segments) >= 3 and len(segments[0]) == 2 and segments[1] == "chart":
            refs.append(ImdbChartRef(chart_slug=segments[2], url=cleaned))
            continue
        if len(segments) >= 2 and segments[0] == "chart":
            refs.append(ImdbChartRef(chart_slug=segments[1], url=cleaned))
    return refs


def parse_mdblist_urls(urls: Iterable[str]) -> list[MdblistRef]:
    refs: list[MdblistRef] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower()
        if host and host not in {"mdblist.com", "www.mdblist.com"}:
            continue
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 3 and segments[0] == "lists":
            username = segments[1]
            slug = segments[2]
            refs.append(
                MdblistRef(
                    username=username,
                    slug=slug,
                    url=cleaned,
                    external_id=f"mdblist:{username}:{slug}",
                    name=slug,
                )
            )
    return refs
