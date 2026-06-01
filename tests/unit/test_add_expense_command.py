"""Unit tests for AddExpenseCommand DTO validation — no database needed."""
import uuid

import pytest
from pydantic import ValidationError

from countbeans.dto.commands import AddExpenseCommand


def _cmd(**overrides: object) -> AddExpenseCommand:
    defaults: dict[str, object] = {
        "group_id": uuid.uuid4(),
        "payer_id": uuid.uuid4(),
        "amount_cents": 1000,
        "currency": "SGD",
        "participants": [uuid.uuid4()],
        "created_by": uuid.uuid4(),
    }
    defaults.update(overrides)
    return AddExpenseCommand(**defaults)  # type: ignore[arg-type]


def test_valid() -> None:
    cmd = _cmd()
    assert cmd.amount_cents == 1000
    assert cmd.split_mode == "equal"
    assert cmd.event_id is None


def test_rejects_zero_amount() -> None:
    with pytest.raises(ValidationError, match="amount_cents"):
        _cmd(amount_cents=0)


def test_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError, match="amount_cents"):
        _cmd(amount_cents=-1)


def test_rejects_empty_participants() -> None:
    with pytest.raises(ValidationError, match="participants"):
        _cmd(participants=[])


def test_rejects_short_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        _cmd(currency="SG")


def test_rejects_long_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        _cmd(currency="SGDD")


def test_frozen() -> None:
    cmd = _cmd()
    with pytest.raises(Exception):  # noqa: B017
        cmd.amount_cents = 999  # type: ignore[misc]
