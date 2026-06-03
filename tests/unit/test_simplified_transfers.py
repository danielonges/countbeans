"""Unit tests for _simplified_transfers — the greedy debt-simplification heuristic.

No database needed. Covers the per-currency split, the reduced transfer count vs.
raw pairwise, deterministic tie-breaking, and the load-bearing invariant that the
output is always a *valid* settlement (every balance zeroes out).
"""
import uuid

from countbeans.dto.domain import BalanceKey, BalanceMap, Transfer
from countbeans.services.balance import _raw_pairwise_transfers, _simplified_transfers


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def _settles(balances: BalanceMap, transfers: list[Transfer]) -> bool:
    """Apply transfers to balances; return True iff every balance ends at zero.
    A debtor paying raises their (negative) balance; a creditor receiving lowers
    their (positive) balance."""
    net = dict(balances)
    for t in transfers:
        from_key = BalanceKey(t.from_user_id, t.currency)
        to_key = BalanceKey(t.to_user_id, t.currency)
        net[from_key] = net.get(from_key, 0) + t.amount_cents
        net[to_key] = net.get(to_key, 0) - t.amount_cents
    return all(v == 0 for v in net.values())


def test_one_debtor_one_creditor() -> None:
    a, b = _uid(), _uid()
    transfers = _simplified_transfers({BalanceKey(a, "SGD"): -100, BalanceKey(b, "SGD"): 100})
    assert len(transfers) == 1
    assert transfers[0].from_user_id == a
    assert transfers[0].to_user_id == b
    assert transfers[0].amount_cents == 100


def test_empty_balances_no_transfers() -> None:
    assert _simplified_transfers({}) == []


def test_all_creditors_no_transfers() -> None:
    a, b = _uid(), _uid()
    assert _simplified_transfers({BalanceKey(a, "SGD"): 50, BalanceKey(b, "SGD"): 50}) == []


def test_currency_isolation() -> None:
    a, b = _uid(), _uid()
    transfers = _simplified_transfers({
        BalanceKey(a, "SGD"): -100, BalanceKey(b, "SGD"): 100,
        BalanceKey(a, "USD"): -50, BalanceKey(b, "USD"): 50,
    })
    assert sum(t.amount_cents for t in transfers if t.currency == "SGD") == 100
    assert sum(t.amount_cents for t in transfers if t.currency == "USD") == 50
    assert {t.currency for t in transfers} == {"SGD", "USD"}


def test_reduces_transfer_count_vs_raw_pairwise() -> None:
    # Assign ids in ascending order so the *raw* (id-sorted) greedy hits its
    # worst case: small debtor & big creditor come first, forcing the big debt to
    # split across both creditors → 3 transfers. Amount-sorted simplify pairs the
    # two big sides first and settles in 2.
    a, b, c, d = sorted(_uid() for _ in range(4))  # a < b < c < d
    balances = {
        BalanceKey(a, "SGD"): -1, BalanceKey(b, "SGD"): -9,
        BalanceKey(c, "SGD"): 9, BalanceKey(d, "SGD"): 1,
    }

    simplified = _simplified_transfers(balances)
    assert {(t.from_user_id, t.to_user_id, t.amount_cents) for t in simplified} == {
        (b, c, 9),
        (a, d, 1),
    }
    assert len(simplified) == 2

    raw = _raw_pairwise_transfers(balances)
    assert len(raw) == 3

    # Both must be valid settlements — the toggle only trades fewness for the
    # raw view, never correctness.
    assert _settles(balances, simplified)
    assert _settles(balances, raw)


def test_deterministic_tie_break_by_id() -> None:
    # Equal debts: the matching order is broken by id (ascending) so the output
    # is stable across runs.
    a, b, c = _uid(), _uid(), _uid()
    balances = {
        BalanceKey(a, "SGD"): -5, BalanceKey(b, "SGD"): -5, BalanceKey(c, "SGD"): 10,
    }

    first = _simplified_transfers(balances)
    second = _simplified_transfers(balances)
    assert first == second
    assert [t.from_user_id for t in first] == sorted([a, b])
    assert all(t.to_user_id == c for t in first)


def test_always_a_valid_settlement() -> None:
    # A messier mix of debtors and creditors still settles exactly.
    a, b, c, d, e = _uid(), _uid(), _uid(), _uid(), _uid()
    balances = {
        BalanceKey(a, "SGD"): -30, BalanceKey(b, "SGD"): -45, BalanceKey(c, "SGD"): -25,
        BalanceKey(d, "SGD"): 70, BalanceKey(e, "SGD"): 30,
    }
    transfers = _simplified_transfers(balances)
    assert _settles(balances, transfers)
    assert all(t.amount_cents > 0 for t in transfers)
