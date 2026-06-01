from datetime import timezone

from countbeans.db._mixins import _now


def test_now_is_timezone_aware() -> None:
    assert _now().tzinfo is not None


def test_now_is_utc() -> None:
    assert _now().tzinfo == timezone.utc


def test_now_is_monotonic() -> None:
    first = _now()
    second = _now()
    assert second >= first
