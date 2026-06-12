"""Unit tests for parse_money — currency-aware amount parsing."""

import pytest

from countbeans.bot.utils.parsing import (
    MAX_AMOUNT_CENTS,
    explicit_currency,
    looks_like_money,
    parse_amount_cents,
    parse_money,
)


# explicit_currency — does the token pin a currency, or follow the scope?
# (Drives the wizard's #general toggle: pinned currencies survive a scope
# flip; bare/$ amounts are re-derived as "the scope's money".)
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("USD50", "USD"),
        ("usd50", "USD"),
        ("€50", "EUR"),
        ("50", None),  # bare → scope default
        ("$50", None),  # $ is "my scope's money", never a pin
        ("dinner", None),  # not money at all
    ],
)
def test_explicit_currency(token: str, expected: str | None) -> None:
    assert explicit_currency(token) == expected


# looks_like_money — the diagnostic predicate behind the "amount in the wrong
# place" nudges (/addexpense's ordering hint, the wizard's non-reply nudge).
@pytest.mark.parametrize("token", ["50", "25.50", "$50", "€50", "USD50", "usd50"])
def test_looks_like_money_accepts_amount_tokens(token: str) -> None:
    assert looks_like_money(token) is True


@pytest.mark.parametrize("token", ["dinner", "@alice", "50%", "", "USD", "0"])
def test_looks_like_money_rejects_non_amounts(token: str) -> None:
    assert looks_like_money(token) is False


def test_bare_number_uses_default_currency():
    assert parse_money("50", "SGD") == ("SGD", 5000)


def test_decimal_amount():
    assert parse_money("25.50", "SGD") == ("SGD", 2550)


def test_dollar_symbol_resolves_to_group_default():
    # $ is the "my group's money" symbol — never presumed USD.
    assert parse_money("$50", "SGD") == ("SGD", 5000)
    assert parse_money("$50", "AUD") == ("AUD", 5000)


def test_unambiguous_symbols():
    assert parse_money("€50", "SGD") == ("EUR", 5000)
    assert parse_money("£50", "SGD") == ("GBP", 5000)
    assert parse_money("¥500", "SGD") == ("JPY", 50000)
    assert parse_money("₹50", "SGD") == ("INR", 5000)
    assert parse_money("₩50", "SGD") == ("KRW", 5000)


def test_explicit_iso_code_overrides_default():
    assert parse_money("USD50", "SGD") == ("USD", 5000)


def test_iso_code_is_upper_cased():
    assert parse_money("usd50", "SGD") == ("USD", 5000)


def test_iso_code_with_decimal():
    assert parse_money("EUR25.50", "SGD") == ("EUR", 2550)


def test_unknown_3letter_code_trusted_by_shape():
    # No ISO-4217 registry — a 3-letter code is trusted by shape, matching the
    # DTO layer's rule.
    assert parse_money("ABC50", "SGD") == ("ABC", 5000)


def test_zero_is_rejected():
    with pytest.raises(ValueError):
        parse_money("0", "SGD")


def test_three_decimal_places_rejected():
    with pytest.raises(ValueError):
        parse_money("12.345", "SGD")


def test_garbage_token_rejected():
    with pytest.raises(ValueError):
        parse_money("dinner", "SGD")


def test_symbol_without_amount_rejected():
    with pytest.raises(ValueError):
        parse_money("$", "SGD")


def test_amount_at_limit_is_accepted():
    # The cap is inclusive — exactly MAX_AMOUNT_CENTS is fine.
    assert parse_amount_cents(str(MAX_AMOUNT_CENTS // 100)) == MAX_AMOUNT_CENTS


def test_amount_over_limit_rejected():
    # One major unit past the cap would overflow the BIGINT column on insert; it
    # must be rejected cleanly rather than reaching the DB.
    with pytest.raises(ValueError):
        parse_amount_cents(str(MAX_AMOUNT_CENTS // 100 + 1))


def test_oversized_amount_rejected_via_parse_money():
    with pytest.raises(ValueError):
        parse_money("9" * 20, "SGD")
