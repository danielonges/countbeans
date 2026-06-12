"""Unit tests for the wizard's one-liner teaching tip (_one_liner_tip).

The tip must round-trip: every command it emits has to parse back into the
same expense — so it bails (returns None) whenever the draft can't be spelled
faithfully in the one-liner grammar.
"""

from typing import cast

from countbeans.bot.handlers.addexpense_wizard.render import _one_liner_tip
from countbeans.bot.handlers.addexpense_wizard.states import RosterMember, WizardDraft


def _rm(username: str | None, *, pending: bool = False) -> RosterMember:
    return {
        "user_id": "u-" + (username or "nohandle"),
        "username": username,
        "first_name": username.capitalize() if username else "NoHandle",
        "is_pending": pending,
    }


def _draft(**overrides: object) -> WizardDraft:
    base: dict[str, object] = {
        "split_mode": "equal",
        "amount_cents": 5025,
        "currency": "SGD",
        "currency_general": "SGD",
        "currency_event": None,
        "description": "dinner",
        "roster": [_rm("alice"), _rm("bob")],
        "selected": [0, 1],
        "force_general": False,
        "active_event_id": None,
    }
    base.update(overrides)
    return cast(WizardDraft, base)


def test_everyone_selected_omits_mentions() -> None:
    # Everyone ⇄ no mentions — the inline "split everyone" default.
    assert _one_liner_tip(_draft()) == "/addexpense 50.25 dinner"


def test_subset_lists_handles() -> None:
    assert _one_liner_tip(_draft(selected=[1])) == "/addexpense 50.25 dinner @bob"


def test_no_description_is_just_amount() -> None:
    assert _one_liner_tip(_draft(description=None)) == "/addexpense 50.25"


def test_whole_amount_drops_cents() -> None:
    assert (
        _one_liner_tip(_draft(amount_cents=5000, description=None)) == "/addexpense 50"
    )


def test_overridden_currency_is_prefixed() -> None:
    assert (
        _one_liner_tip(_draft(currency="EUR", description=None))
        == "/addexpense EUR50.25"
    )


def test_currency_compares_against_effective_scope_default() -> None:
    # Event scope: a bare amount parsed as the event's JPY needs no prefix —
    # the one-liner under the same active event resolves bare → JPY too.
    tip = _one_liner_tip(
        _draft(
            currency="JPY",
            currency_event="JPY",
            active_event_id="e1",
            description=None,
        )
    )
    assert tip == "/addexpense 50.25"


def test_general_override_prefixes_event_currency() -> None:
    # Pinned event-currency + forced general: the one-liner's #general scope
    # resolves bare → SGD, so JPY must be spelled out to round-trip.
    tip = _one_liner_tip(
        _draft(
            currency="JPY",
            currency_event="JPY",
            active_event_id="e1",
            force_general=True,
            description=None,
        )
    )
    assert tip == "/addexpense JPY50.25 #general"


def test_general_override_appends_flag() -> None:
    tip = _one_liner_tip(_draft(force_general=True, active_event_id="e1"))
    assert tip == "/addexpense 50.25 dinner #general"


def test_uneven_split_has_no_tip() -> None:
    assert _one_liner_tip(_draft(split_mode="percent")) is None


def test_subset_member_without_handle_has_no_tip() -> None:
    # A subset needs @handles; a member without a public username can't be
    # mentioned by text, so the tip would lie — skip it.
    assert _one_liner_tip(_draft(roster=[_rm(None), _rm("bob")], selected=[0])) is None


def test_everyone_without_handles_still_tips() -> None:
    # Everyone selected needs no mentions, so missing handles don't matter.
    tip = _one_liner_tip(_draft(roster=[_rm(None)], selected=[0]))
    assert tip == "/addexpense 50.25 dinner"


def test_unsafe_description_has_no_tip() -> None:
    # An @ or quote inside the description would mis-parse in the one-liner.
    assert _one_liner_tip(_draft(description="lunch @ joe's")) is None
