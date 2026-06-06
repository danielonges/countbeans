"""Unit tests for unquoted_description and the quoted→unquoted resolution.

`unquoted_description` is the fallback the /addexpense handler uses when there is
no *quoted* description (spec /addexpense grammar rule 2): the run of words
between the amount and the first @mention. The amount token is already consumed
by the caller, so the input here is everything after it.
"""

from countbeans.bot.utils.parsing import (
    extract_quoted_description,
    unquoted_description,
)


def _resolve(rest: str) -> str | None:
    """Mirror the handler: a quoted description wins, else the unquoted run."""
    description, rest = extract_quoted_description(rest)
    if description is None:
        description = unquoted_description(rest)
    return description


# --- unquoted_description (the pure helper) ---------------------------------


def test_words_before_mention() -> None:
    assert unquoted_description("lunch @bob") == "lunch"


def test_multiple_words_before_mention() -> None:
    assert (
        unquoted_description("lunch with the team @bob @sue") == "lunch with the team"
    )


def test_no_mention_keeps_whole_run() -> None:
    assert unquoted_description("movie tickets") == "movie tickets"


def test_only_mention_yields_none() -> None:
    assert unquoted_description("@bob") is None


def test_empty_yields_none() -> None:
    assert unquoted_description("") is None


def test_whitespace_only_yields_none() -> None:
    assert unquoted_description("   ") is None


def test_run_is_trimmed() -> None:
    # Leading/trailing whitespace around the run (and before the @) is stripped.
    assert unquoted_description("  lunch   @bob") == "lunch"


def test_standalone_currency_word_becomes_description() -> None:
    # "USD" here is NOT a fused amount marker (that rides the amount token, which
    # the caller already consumed) — it is just the first word of the description.
    assert unquoted_description("USD @alice") == "USD"


# --- end-to-end resolution (quoted wins, else unquoted) ---------------------


def test_resolve_unquoted_run() -> None:
    assert _resolve("lunch @bob") == "lunch"


def test_resolve_quoted_still_wins() -> None:
    assert _resolve('"Dinner" @alice') == "Dinner"


def test_resolve_mention_only_is_none() -> None:
    assert _resolve("@bob") is None


def test_resolve_multiword_unquoted() -> None:
    assert _resolve("lunch with the team @bob @sue") == "lunch with the team"


def test_resolve_no_mention() -> None:
    assert _resolve("movie tickets") == "movie tickets"
