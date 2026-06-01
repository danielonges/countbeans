"""Unit tests for _raw_pairwise_transfers — no database needed."""
import uuid

from countbeans.services.balance import _raw_pairwise_transfers


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def test_one_debtor_one_creditor() -> None:
    a, b = _uid(), _uid()
    transfers = _raw_pairwise_transfers({(a, "SGD"): -100, (b, "SGD"): 100})
    assert len(transfers) == 1
    assert transfers[0].from_user_id == a
    assert transfers[0].to_user_id == b
    assert transfers[0].amount_cents == 100


def test_two_debtors_one_creditor() -> None:
    a, b, c = _uid(), _uid(), _uid()
    transfers = _raw_pairwise_transfers({(a, "SGD"): -50, (b, "SGD"): -50, (c, "SGD"): 100})
    assert len(transfers) == 2
    assert all(t.to_user_id == c for t in transfers)


def test_empty_balances_no_transfers() -> None:
    assert _raw_pairwise_transfers({}) == []


def test_all_creditors_no_transfers() -> None:
    a, b = _uid(), _uid()
    assert _raw_pairwise_transfers({(a, "SGD"): 50, (b, "SGD"): 50}) == []


def test_currency_isolation() -> None:
    a, b = _uid(), _uid()
    transfers = _raw_pairwise_transfers({
        (a, "SGD"): -100, (b, "SGD"): 100,
        (a, "USD"): -50, (b, "USD"): 50,
    })
    sgd = [t for t in transfers if t.currency == "SGD"]
    usd = [t for t in transfers if t.currency == "USD"]
    assert len(sgd) == 1
    assert len(usd) == 1
