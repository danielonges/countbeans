"""Unit tests for suggested_owed / suggested_owed_by_currency.

These are the single source of truth for "what you owe a specific person" — the
amount of the suggested you→them transfer. No DB: balances are constructed
directly. UUIDs are int-derived so the greedy tie-break (by id) is deterministic
and the simplify-on-vs-off routing is pinned exactly.
"""

import uuid

from countbeans.dto.domain import BalanceKey, BalanceMap
from countbeans.services.balance import suggested_owed, suggested_owed_by_currency

A, B, C, D = (uuid.UUID(int=n) for n in (1, 2, 3, 4))  # A < B < C < D


def _bal(entries: dict[tuple[uuid.UUID, str], int]) -> BalanceMap:
    return {BalanceKey(uid, cur): cents for (uid, cur), cents in entries.items()}


def test_direct_debt_amount() -> None:
    balances = _bal({(A, "SGD"): -30_00, (B, "SGD"): +30_00})
    assert suggested_owed(balances, A, B, "SGD", simplify_debts=True) == 30_00
    # The creditor owes the debtor nothing.
    assert suggested_owed(balances, B, A, "SGD", simplify_debts=True) == 0


def test_no_payment_between_pair_is_zero() -> None:
    # A owes 50 total, suggested to pay B 30 and C 20.
    balances = _bal({(A, "SGD"): -50_00, (B, "SGD"): +30_00, (C, "SGD"): +20_00})
    assert suggested_owed(balances, A, B, "SGD", simplify_debts=True) == 30_00
    assert suggested_owed(balances, A, C, "SGD", simplify_debts=True) == 20_00
    # B and C are creditors — no suggested payment between them or back to A.
    assert suggested_owed(balances, B, C, "SGD", simplify_debts=True) == 0
    assert suggested_owed(balances, B, A, "SGD", simplify_debts=True) == 0


def test_cap_is_below_net_debt() -> None:
    # The regression: A's net debt is 50, but the suggested payment to B is only
    # 30 — so /settleup-ing 50 to B must be rejected (handled by the > owed check
    # in settle_up, anchored on this value).
    balances = _bal({(A, "SGD"): -50_00, (B, "SGD"): +30_00, (C, "SGD"): +20_00})
    owed_to_b = suggested_owed(balances, A, B, "SGD", simplify_debts=True)
    assert owed_to_b == 30_00
    net_debt = 50_00
    assert owed_to_b < net_debt


def test_by_currency_isolates_currencies() -> None:
    # A owes B in SGD and C in EUR; each scope sums to zero independently.
    balances = _bal(
        {
            (A, "SGD"): -30_00,
            (B, "SGD"): +30_00,
            (A, "EUR"): -20_00,
            (C, "EUR"): +20_00,
        }
    )
    assert suggested_owed_by_currency(balances, A, B, simplify_debts=True) == {
        "SGD": 30_00
    }
    assert suggested_owed_by_currency(balances, A, C, simplify_debts=True) == {
        "EUR": 20_00
    }
    # Looking up the wrong currency for the pair yields zero.
    assert suggested_owed(balances, A, B, "EUR", simplify_debts=True) == 0


def test_simplify_flag_changes_routing() -> None:
    # Net: A -1, B -9, C +9, D +1. Simplified pairs largest-largest (B→C 9,
    # A→D 1); raw pairs in id order (A→C 1, B→C 8, B→D 1). So whether A is
    # suggested to pay C flips with the toggle — proving the flag is threaded.
    balances = _bal(
        {
            (A, "SGD"): -1_00,
            (B, "SGD"): -9_00,
            (C, "SGD"): +9_00,
            (D, "SGD"): +1_00,
        }
    )
    assert suggested_owed(balances, A, C, "SGD", simplify_debts=True) == 0
    assert suggested_owed(balances, A, D, "SGD", simplify_debts=True) == 1_00

    assert suggested_owed(balances, A, C, "SGD", simplify_debts=False) == 1_00
    assert suggested_owed(balances, A, D, "SGD", simplify_debts=False) == 0
