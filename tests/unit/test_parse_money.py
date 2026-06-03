"""Unit tests for parse_money — currency-aware amount parsing."""
import pytest

from countbeans.bot.parsing import parse_money


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
