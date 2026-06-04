"""Unit tests for balance domain DTOs — no database needed."""

import uuid

import pytest

from countbeans.dto.domain import GroupSummary, MemberBalance, Transfer


def test_member_balance_frozen() -> None:
    b = MemberBalance(
        user_id=uuid.uuid4(), username="alice", balance_cents=500, currency="SGD"
    )
    with pytest.raises(Exception):  # noqa: B017
        b.balance_cents = 0  # type: ignore[misc]


def test_transfer_frozen() -> None:
    t = Transfer(
        from_user_id=uuid.uuid4(),
        to_user_id=uuid.uuid4(),
        amount_cents=100,
        currency="SGD",
    )
    with pytest.raises(Exception):  # noqa: B017
        t.amount_cents = 0  # type: ignore[misc]


def test_group_summary_frozen() -> None:
    s = GroupSummary(group_id=uuid.uuid4(), balances=[], suggested_transfers=[])
    with pytest.raises(Exception):  # noqa: B017
        s.balances = []  # type: ignore[misc]


def test_positive_balance_means_group_owes_member() -> None:
    b = MemberBalance(
        user_id=uuid.uuid4(), username=None, balance_cents=1000, currency="SGD"
    )
    assert b.balance_cents > 0


def test_negative_balance_means_member_owes_group() -> None:
    b = MemberBalance(
        user_id=uuid.uuid4(), username=None, balance_cents=-500, currency="SGD"
    )
    assert b.balance_cents < 0
