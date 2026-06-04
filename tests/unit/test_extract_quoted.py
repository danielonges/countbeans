"""Unit tests for extract_quoted_description — flexible quoted descriptions."""

from countbeans.bot.parsing import extract_quoted_description


def test_straight_double_quotes() -> None:
    assert extract_quoted_description('"dinner"') == ("dinner", "")


def test_straight_single_quotes() -> None:
    assert extract_quoted_description("'lunch'") == ("lunch", "")


def test_curly_double_quotes() -> None:
    # The mobile-keyboard case: “…” must work just like "…".
    assert extract_quoted_description("“dinner”") == ("dinner", "")


def test_curly_single_quotes() -> None:
    assert extract_quoted_description("‘lunch’") == ("lunch", "")


def test_backticks_and_guillemets() -> None:
    assert extract_quoted_description("`code`") == ("code", "")
    assert extract_quoted_description("«bonjour»") == ("bonjour", "")


def test_description_then_mentions_left_in_remainder() -> None:
    desc, rest = extract_quoted_description('"dinner" @alice @bob')
    assert desc == "dinner"
    assert "@alice" in rest and "@bob" in rest


def test_mention_before_quote() -> None:
    desc, rest = extract_quoted_description('@alice "dinner"')
    assert desc == "dinner"
    assert "@alice" in rest


def test_at_sign_inside_quotes_is_not_a_mention() -> None:
    # The whole quoted run (with its @) is removed from the remainder.
    desc, rest = extract_quoted_description('"dinner with @alice"')
    assert desc == "dinner with @alice"
    assert "@alice" not in rest


def test_escaped_closing_quote() -> None:
    assert extract_quoted_description('"she said \\"hi\\""') == ('she said "hi"', "")


def test_escaped_backslash() -> None:
    assert extract_quoted_description('"a\\\\b"') == ("a\\b", "")


def test_mismatched_pair_is_not_a_description() -> None:
    # A curly opener closed by a straight quote is not a matching pair.
    assert extract_quoted_description('“dinner"') == (None, '“dinner"')


def test_no_quotes_returns_text_unchanged() -> None:
    assert extract_quoted_description("dinner @alice") == (None, "dinner @alice")


def test_empty_quotes_yield_none() -> None:
    assert extract_quoted_description('""') == (None, "")


def test_unmatched_apostrophe_skipped_so_real_quote_wins() -> None:
    # The ' in "it's" has no closing ' — it must not swallow the real "dinner".
    desc, rest = extract_quoted_description('it\'s "dinner"')
    assert desc == "dinner"
    assert "it's" in rest


def test_first_complete_pair_wins() -> None:
    assert extract_quoted_description('"a" "b"')[0] == "a"
