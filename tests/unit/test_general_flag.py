"""Unit tests for the reserved #general write-scope override (CLAUDE.md "The
#general write-scope override"). One matcher backs both /addexpense and /settleup,
so the keyword can never drift between commands."""

from countbeans.bot.utils.parsing import extract_general_flag


def test_absent_returns_text_unchanged() -> None:
    text, present = extract_general_flag("Lunch @bob")
    assert not present
    assert text == "Lunch @bob"


def test_strips_the_token_and_flags_present() -> None:
    text, present = extract_general_flag("Lunch @bob #general")
    assert present
    assert text == "Lunch @bob"


def test_case_insensitive() -> None:
    for token in ("#general", "#General", "#GENERAL"):
        text, present = extract_general_flag(f"@bob {token}")
        assert present
        assert text == "@bob"


def test_position_does_not_matter() -> None:
    # Before, between, or after the @mentions — all strip to the same remainder.
    for raw in ("#general @bob @carol", "@bob #general @carol", "@bob @carol #general"):
        text, present = extract_general_flag(raw)
        assert present
        assert text == "@bob @carol"


def test_only_whole_token_matches() -> None:
    # A glued or longer word is not the override (so "#generals" stays text).
    for raw in ("#generals", "x#general", "#general2"):
        text, present = extract_general_flag(raw)
        assert not present
        assert text == raw


def test_token_only_collapses_to_empty() -> None:
    text, present = extract_general_flag("#general")
    assert present
    assert text == ""
