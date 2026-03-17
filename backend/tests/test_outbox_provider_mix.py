import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.jobs import process_outbox  # noqa: E402


def test_mixed_provider_limits_even_split() -> None:
    limits = process_outbox._mixed_provider_limits(
        40, process_outbox.MIXED_PROVIDER_ORDER
    )
    assert limits == {
        "trakt": 8,
        "simkl": 8,
        "publicmetadb": 8,
        "letterboxd": 8,
        "stremio": 8,
    }


def test_mixed_provider_limits_remainder() -> None:
    limits = process_outbox._mixed_provider_limits(
        12, process_outbox.MIXED_PROVIDER_ORDER
    )
    assert limits == {
        "trakt": 3,
        "simkl": 3,
        "publicmetadb": 2,
        "letterboxd": 2,
        "stremio": 2,
    }


def test_mixed_provider_limits_small_limit() -> None:
    limits = process_outbox._mixed_provider_limits(
        2, process_outbox.MIXED_PROVIDER_ORDER
    )
    assert limits == {
        "trakt": 1,
        "simkl": 1,
    }


def test_mixed_provider_limits_zero_limit() -> None:
    limits = process_outbox._mixed_provider_limits(
        0, process_outbox.MIXED_PROVIDER_ORDER
    )
    assert limits == {}
