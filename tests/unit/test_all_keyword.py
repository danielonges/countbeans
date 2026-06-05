"""Unit tests for the reserved everyone-keyword helpers (CLAUDE.md "The @all /
all keyword"). One predicate backs both grammatical families, so the keyword
can never drift between commands."""

from countbeans.bot.utils.parsing import ALL_KEYWORD, is_all, is_all_selector


# Family 1 — the @all mention/target token (handle already stripped of its @).
def test_is_all_matches_keyword_case_insensitively() -> None:
    assert is_all("all")
    assert is_all("All")
    assert is_all("ALL")
    assert is_all(ALL_KEYWORD)


def test_is_all_rejects_real_handles() -> None:
    assert not is_all("alice")
    assert not is_all("allan")  # not a prefix match
    assert not is_all("")


# Family 2 — the bare `all` positional view selector (/balance all, /statements all).
def test_is_all_selector_true_on_second_arg() -> None:
    assert is_all_selector(["/balance", "all"])
    assert is_all_selector(["/statements", "ALL"])


def test_is_all_selector_false_without_selector() -> None:
    assert not is_all_selector(["/balance"])  # bare command → personal view
    assert not is_all_selector(["/statements", "me"])
    assert not is_all_selector([])
    # Only the first positional arg counts as the selector.
    assert not is_all_selector(["/balance", "general", "all"])
