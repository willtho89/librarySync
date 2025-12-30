from __future__ import annotations

import math


def normalize_star_rating(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Rating must be a number between 0.5 and 5.0 in 0.5 steps")
    try:
        rating = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Rating must be a number between 0.5 and 5.0 in 0.5 steps"
        ) from exc
    if not math.isfinite(rating):
        raise ValueError("Rating must be a finite number between 0.5 and 5.0")
    if rating < 0.5 or rating > 5.0:
        raise ValueError("Rating must be between 0.5 and 5.0")
    steps = rating * 2
    if abs(steps - round(steps)) > 1e-6:
        raise ValueError("Rating must be in 0.5 star increments")
    return round(steps) / 2


def coerce_star_rating(value: object | None) -> float | None:
    try:
        return normalize_star_rating(value)
    except ValueError:
        return None


def normalize_ten_point_rating(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rating):
        return None
    if rating < 1 or rating > 10:
        return None
    return coerce_star_rating(rating / 2)
