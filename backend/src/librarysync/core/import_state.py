from __future__ import annotations

from typing import Any


def normalize_queue(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(entry) for entry in value if entry]
    return []


def coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None


def coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
