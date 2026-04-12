import pytest
from librarysync.core.ratings import (
    coerce_star_rating,
    normalize_star_rating,
    normalize_ten_point_rating,
)


class TestNormalizeStarRating:
    def test_none_returns_none(self) -> None:
        assert normalize_star_rating(None) is None

    def test_valid_half_star_increments(self) -> None:
        assert normalize_star_rating(0.5) == 0.5
        assert normalize_star_rating(1.0) == 1.0
        assert normalize_star_rating(1.5) == 1.5
        assert normalize_star_rating(2.0) == 2.0
        assert normalize_star_rating(2.5) == 2.5
        assert normalize_star_rating(3.0) == 3.0
        assert normalize_star_rating(3.5) == 3.5
        assert normalize_star_rating(4.0) == 4.0
        assert normalize_star_rating(4.5) == 4.5
        assert normalize_star_rating(5.0) == 5.0

    def test_rejects_rating_below_half(self) -> None:
        with pytest.raises(ValueError, match="between 0.5 and 5.0"):
            normalize_star_rating(0.0)
        with pytest.raises(ValueError, match="between 0.5 and 5.0"):
            normalize_star_rating(0.49)

    def test_rejects_rating_above_five(self) -> None:
        with pytest.raises(ValueError, match="between 0.5 and 5.0"):
            normalize_star_rating(5.1)
        with pytest.raises(ValueError, match="between 0.5 and 5.0"):
            normalize_star_rating(10.0)

    def test_rejects_non_half_increments(self) -> None:
        with pytest.raises(ValueError, match="0.5 star increments"):
            normalize_star_rating(1.3)
        with pytest.raises(ValueError, match="0.5 star increments"):
            normalize_star_rating(2.7)
        with pytest.raises(ValueError, match="0.5 star increments"):
            normalize_star_rating(0.51)

    def test_rejects_non_finite_values(self) -> None:
        with pytest.raises(ValueError):
            normalize_star_rating(float("inf"))
        with pytest.raises(ValueError):
            normalize_star_rating(float("-inf"))
        with pytest.raises(ValueError):
            normalize_star_rating(float("nan"))

    def test_rejects_boolean(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            normalize_star_rating(True)
        with pytest.raises(ValueError, match="must be a number"):
            normalize_star_rating(False)

    def test_rejects_non_numeric_strings(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            normalize_star_rating("PG-13")
        with pytest.raises(ValueError, match="must be a number"):
            normalize_star_rating("not-a-number")

    def test_accepts_integer(self) -> None:
        assert normalize_star_rating(3) == 3.0
        assert normalize_star_rating(5) == 5.0

    def test_rejects_dict(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            normalize_star_rating({"foo": "bar"})


class TestCoerceStarRating:
    def test_none_returns_none(self) -> None:
        assert coerce_star_rating(None) is None

    def test_valid_rating_passthrough(self) -> None:
        assert coerce_star_rating(4.0) == 4.0

    def test_invalid_rating_returns_none_not_exception(self) -> None:
        assert coerce_star_rating(0.0) is None
        assert coerce_star_rating(6.0) is None
        assert coerce_star_rating(True) is None
        assert coerce_star_rating("PG-13") is None


class TestNormalizeTenPointRating:
    def test_none_returns_none(self) -> None:
        assert normalize_ten_point_rating(None) is None

    def test_valid_ten_point_scale_converts_to_stars(self) -> None:
        assert normalize_ten_point_rating(1) == 0.5
        assert normalize_ten_point_rating(5) == 2.5
        assert normalize_ten_point_rating(10) == 5.0

    def test_rounds_to_nearest_half_star(self) -> None:
        assert normalize_ten_point_rating(6) == 3.0
        assert normalize_ten_point_rating(7) == 3.5

    def test_rejects_below_one(self) -> None:
        assert normalize_ten_point_rating(0) is None
        assert normalize_ten_point_rating(-1) is None

    def test_rejects_above_ten(self) -> None:
        assert normalize_ten_point_rating(11) is None
        assert normalize_ten_point_rating(100) is None

    def test_rejects_non_finite(self) -> None:
        assert normalize_ten_point_rating(float("inf")) is None
        assert normalize_ten_point_rating(float("nan")) is None

    def test_rejects_boolean(self) -> None:
        assert normalize_ten_point_rating(True) is None
        assert normalize_ten_point_rating(False) is None

    def test_non_half_resulting_star_rejected(self) -> None:
        assert normalize_ten_point_rating(7) == 3.5
        assert normalize_ten_point_rating(8) == 4.0
        assert normalize_ten_point_rating(9) == 4.5
