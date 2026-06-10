"""Unit tests for the shared display_name helper: @handle → first name → generic."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from countbeans.bot.utils.formatting import (
    display_name,
    general_override_note,
    humanize_validation_error,
    payer_excluded_from_named_split,
)
from countbeans.dto.commands import AddExpenseCommand, SettleUpCommand


def test_prefers_username() -> None:
    assert display_name("alice", "Alice") == "@alice"


def test_falls_back_to_first_name() -> None:
    assert display_name(None, "Bob") == "Bob"


def test_falls_back_to_generic_when_nameless() -> None:
    # Near-unreachable in practice (claimed users always have a first name,
    # placeholders always have a @handle) — but never a raw UUID.
    assert display_name(None, None) == "someone"


def test_empty_strings_treated_as_missing() -> None:
    # Empty username falls through to first name; both empty → generic.
    assert display_name("", "Carol") == "Carol"
    assert display_name("", "") == "someone"


# payer_excluded_from_named_split — the non-blocking self-mention nudge (Finding 4).
# A named subset split excludes the payer unless they mention themselves, so the
# nudge fires exactly when the user named participants and the payer is absent.
def test_nudge_when_named_split_excludes_payer() -> None:
    payer = uuid4()
    others = [uuid4(), uuid4()]
    assert payer_excluded_from_named_split(True, others, payer) is True


def test_no_nudge_when_payer_in_named_split() -> None:
    payer = uuid4()
    participants = [payer, uuid4()]
    assert payer_excluded_from_named_split(True, participants, payer) is False


def test_no_nudge_for_split_everyone() -> None:
    # No named participants → "split everyone" always includes the payer, so the
    # payer's absence from the list is irrelevant and the nudge never fires.
    payer = uuid4()
    assert payer_excluded_from_named_split(False, [uuid4(), uuid4()], payer) is False


def test_nudge_for_event_scoped_named_split_when_payer_absent() -> None:
    # Event-scope is irrelevant to the helper — what matters is that the user
    # named participants and the payer isn't among the resolved ids.
    payer = uuid4()
    roster = [uuid4(), uuid4()]
    assert payer_excluded_from_named_split(True, roster, payer) is True


# general_override_note — the #general confirmation shared by the inline handler
# and the wizard (one definition so the two entry paths can't drift).
def test_general_override_note_names_the_event() -> None:
    assert general_override_note("Bali") == (
        '\nℹ️ Logged as general — not tagged to "Bali".'
    )


# humanize_validation_error — the chat reply must carry the validator's own rule
# message, never pydantic's developer report (model name, input reprs, docs URL).
def test_humanized_field_validator_message() -> None:
    payer = uuid4()
    with pytest.raises(ValidationError) as exc_info:
        AddExpenseCommand(
            group_id=uuid4(),
            payer_id=payer,
            amount_cents=1000,
            currency="USD",
            participants=[],
            created_by=payer,
        )
    msg = humanize_validation_error(exc_info.value)
    assert msg == "participants must not be empty"
    assert "errors.pydantic.dev" not in msg
    assert "validation error for" not in msg


def test_humanized_model_validator_message() -> None:
    same = uuid4()
    with pytest.raises(ValidationError) as exc_info:
        SettleUpCommand(
            group_id=uuid4(),
            from_user_id=same,
            to_user_id=same,
            amount_cents=1000,
            currency="USD",
            created_by=same,
        )
    msg = humanize_validation_error(exc_info.value)
    assert msg == "from_user_id and to_user_id must be different users"
    assert "errors.pydantic.dev" not in msg
    assert "validation error for" not in msg
