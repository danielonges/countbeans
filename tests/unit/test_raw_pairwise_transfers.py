"""Unit tests for _raw_pairwise_transfers — no database needed."""

import uuid

from countbeans.dto.domain import BalanceKey
from countbeans.services.balance import _raw_pairwise_transfers


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def test_one_debtor_one_creditor() -> None:
    a, b = _uid(), _uid()
    transfers = _raw_pairwise_transfers(
        {BalanceKey(a, "SGD"): -100, BalanceKey(b, "SGD"): 100}
    )
    assert len(transfers) == 1
    assert transfers[0].from_user_id == a
    assert transfers[0].to_user_id == b
    assert transfers[0].amount_cents == 100


def test_two_debtors_one_creditor() -> None:
    a, b, c = _uid(), _uid(), _uid()
    transfers = _raw_pairwise_transfers(
        {
            BalanceKey(a, "SGD"): -50,
            BalanceKey(b, "SGD"): -50,
            BalanceKey(c, "SGD"): 100,
        }
    )
    assert len(transfers) == 2
    assert all(t.to_user_id == c for t in transfers)


def test_empty_balances_no_transfers() -> None:
    assert _raw_pairwise_transfers({}) == []


def test_all_creditors_no_transfers() -> None:
    a, b = _uid(), _uid()
    assert (
        _raw_pairwise_transfers({BalanceKey(a, "SGD"): 50, BalanceKey(b, "SGD"): 50})
        == []
    )


def test_currency_isolation() -> None:
    a, b = _uid(), _uid()
    transfers = _raw_pairwise_transfers(
        {
            BalanceKey(a, "SGD"): -100,
            BalanceKey(b, "SGD"): 100,
            BalanceKey(a, "USD"): -50,
            BalanceKey(b, "USD"): 50,
        }
    )
    sgd = [t for t in transfers if t.currency == "SGD"]
    usd = [t for t in transfers if t.currency == "USD"]
    assert len(sgd) == 1
    assert len(usd) == 1


def test_multi_debtor_multi_creditor_settles_exactly() -> None:
    # Two debtors and two creditors: the transfers must draw both sides down to
    # zero (no cartesian over-payment). Regression for the earlier bug where
    # owed/credit were never decremented.
    a, b, c, d = _uid(), _uid(), _uid(), _uid()
    balances = {
        BalanceKey(a, "SGD"): -1,
        BalanceKey(b, "SGD"): -9,
        BalanceKey(c, "SGD"): 9,
        BalanceKey(d, "SGD"): 1,
    }

    net = dict(balances)
    for t in _raw_pairwise_transfers(balances):
        net[BalanceKey(t.from_user_id, t.currency)] += t.amount_cents
        net[BalanceKey(t.to_user_id, t.currency)] -= t.amount_cents
    assert all(v == 0 for v in net.values())
