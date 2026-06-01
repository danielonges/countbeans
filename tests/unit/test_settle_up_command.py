"""Unit tests for SettleUpCommand DTO validation — no database needed."""
import uuid

import pytest
from pydantic import ValidationError

from countbeans.dto.commands import SettleUpCommand


def _valid_cmd(**overrides: object) -> SettleUpCommand:
    defaults: dict[str, object] = {
        "group_id": uuid.uuid4(),
        "from_user_id": uuid.uuid4(),
        "to_user_id": uuid.uuid4(),
        "amount_cents": 500,
        "currency": "SGD",
        "created_by": uuid.uuid4(),
    }
    defaults.update(overrides)
    return SettleUpCommand(**defaults)  # type: ignore[arg-type]


def test_settleup_command_valid() -> None:
    cmd = _valid_cmd()
    assert cmd.amount_cents == 500
    assert cmd.currency == "SGD"
    assert cmd.event_id is None


def test_settleup_command_valid_with_event_id() -> None:
    event_id = uuid.uuid4()
    cmd = _valid_cmd(event_id=event_id)
    assert cmd.event_id == event_id


def test_settleup_command_rejects_zero_amount() -> None:
    with pytest.raises(ValidationError, match="amount_cents"):
        _valid_cmd(amount_cents=0)


def test_settleup_command_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError, match="amount_cents"):
        _valid_cmd(amount_cents=-100)


def test_settleup_command_rejects_same_user() -> None:
    same_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="from_user_id and to_user_id must be different"):
        _valid_cmd(from_user_id=same_id, to_user_id=same_id)


def test_settleup_command_rejects_short_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        _valid_cmd(currency="US")


def test_settleup_command_rejects_long_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        _valid_cmd(currency="USDX")


def test_settleup_command_is_frozen() -> None:
    cmd = _valid_cmd()
    with pytest.raises(Exception):  # noqa: B017
        cmd.amount_cents = 999  # type: ignore[misc]
