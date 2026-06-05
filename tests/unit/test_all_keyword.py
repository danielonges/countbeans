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
# args is command.args.split() — the command token is already stripped by CommandObject.
def test_is_all_selector_true_on_first_arg() -> None:
    assert is_all_selector(["all"])
    assert is_all_selector(["ALL"])


def test_is_all_selector_false_without_selector() -> None:
    assert not is_all_selector([])  # no args → personal view
    assert not is_all_selector(["me"])
    # Only the first arg counts; "all" buried later is not the selector.
    assert not is_all_selector(["general", "all"])
