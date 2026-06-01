"""Unit tests for apportion() and compute_shares() — no database needed."""
import uuid

import pytest

from countbeans.services.add_expense import apportion, compute_shares


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_apportion_equal_two() -> None:
    a, b = _ids(2)
    result = apportion(10, {a: 1, b: 1})
    assert sum(result.values()) == 10
    assert abs(result[a] - result[b]) <= 1


def test_apportion_odd_cent_three_ways() -> None:
    a, b, c = _ids(3)
    result = apportion(10, {a: 1, b: 1, c: 1})
    assert sum(result.values()) == 10
    assert sorted(result.values()) == [3, 3, 4]


def test_apportion_weighted() -> None:
    a, b = _ids(2)
    result = apportion(10, {a: 2, b: 1})
    assert sum(result.values()) == 10
    assert result[a] == 7
    assert result[b] == 3


def test_apportion_single_participant() -> None:
    (a,) = _ids(1)
    assert apportion(100, {a: 1}) == {a: 100}


def test_apportion_zero_weights_raises() -> None:
    (a,) = _ids(1)
    with pytest.raises(ValueError, match="weights"):
        apportion(100, {a: 0})


def test_compute_shares_equal() -> None:
    participants = _ids(3)
    result = compute_shares(90, participants, "equal")
    assert sum(result.values()) == 90
    assert all(v == 30 for v in result.values())


def test_compute_shares_percent() -> None:
    a, b = _ids(2)
    result = compute_shares(100, [a, b], "percent", {a: 60, b: 40})
    assert result == {a: 60, b: 40}


def test_compute_shares_percent_wrong_sum() -> None:
    a, b = _ids(2)
    with pytest.raises(ValueError, match="100"):
        compute_shares(100, [a, b], "percent", {a: 50, b: 40})


def test_compute_shares_exact() -> None:
    a, b = _ids(2)
    result = compute_shares(100, [a, b], "exact", {a: 70, b: 30})
    assert result == {a: 70, b: 30}


def test_compute_shares_exact_wrong_sum() -> None:
    a, b = _ids(2)
    with pytest.raises(ValueError, match="sum"):
        compute_shares(100, [a, b], "exact", {a: 60, b: 30})


def test_all_modes_sum_invariant() -> None:
    participants = _ids(5)
    for amount in [7, 100, 333, 10000]:
        result = compute_shares(amount, participants, "equal")
        assert sum(result.values()) == amount, f"failed for amount={amount}"
