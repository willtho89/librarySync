from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse


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

