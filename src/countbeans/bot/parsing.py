"""Shared parsing helpers for the bot's command handlers."""


def parse_amount_cents(s: str) -> int:
    """Parse a decimal string (max 2 dp) to integer cents without using float.

    Float arithmetic accumulates rounding error across a ledger, so amounts are
    parsed straight from the string into integer minor units (see CLAUDE.md
    "Design principles"). Callers gate input with their own amount regex first;
    this raises ``ValueError`` on a non-positive result.
    """
    if "." in s:
        integer_part, decimal_part = s.split(".", 1)
        decimal_part = decimal_part.ljust(2, "0")[:2]
    else:
        integer_part, decimal_part = s, "00"
    cents = int(integer_part) * 100 + int(decimal_part)
    if cents <= 0:
        raise ValueError("amount must be positive")
    return cents
