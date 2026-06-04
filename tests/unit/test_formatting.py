"""Unit tests for the shared display_name helper: @handle → first name → generic."""

from countbeans.bot.utils.formatting import display_name


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
