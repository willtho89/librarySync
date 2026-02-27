import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core import rate_limiter  # noqa: E402


def _mock_settings(**overrides):
    base = {
        "tmdb_rate_limit_per_minute": 150,
        "tvdb_rate_limit_per_minute": 150,
        "trakt_rate_limit_per_minute": 60,
        "simkl_rate_limit_per_minute": 60,
        "letterboxd_rate_limit_per_minute": 30,
        "stremio_rate_limit_per_minute": 120,
        "anilist_rate_limit_per_minute": 90,
        "publicmetadb_rate_limit_per_minute": 120,
        "publicmetadb_rate_limit_max_requests": 300,
        "publicmetadb_rate_limit_interval_seconds": 10.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_publicmetadb_uses_window_limit() -> None:
    with patch.object(rate_limiter, "settings", _mock_settings()):
        configs = rate_limiter._build_rate_limit_configs()
    publicmetadb = configs["publicmetadb"]
    assert publicmetadb.capacity == 300.0
    assert publicmetadb.refill_per_second == 30.0


def test_publicmetadb_falls_back_to_per_minute_when_window_disabled() -> None:
    with patch.object(
        rate_limiter,
        "settings",
        _mock_settings(publicmetadb_rate_limit_max_requests=0),
    ):
        configs = rate_limiter._build_rate_limit_configs()
    publicmetadb = configs["publicmetadb"]
    assert publicmetadb.capacity == 120.0
    assert publicmetadb.refill_per_second == 2.0
