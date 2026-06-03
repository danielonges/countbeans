"""Shared parsing helpers for the bot's command handlers."""
import re

# Currency *symbols* that map to exactly one ISO-4217 code in this app's context.
# Deliberately omits `$`, which is ambiguous across USD/SGD/AUD/CAD/HKD/…: rather
# than guess (and risk silently mis-currencying a money ledger), `$` is resolved
# to the *group's own default currency* below — the "use my group's money"
# symbol — never presumed to be USD. `¥` resolves to JPY (the dominant reading);
# CNY users type the explicit code.
_SYMBOL_TO_CODE = {
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
}

_AMOUNT_BODY = r"\d+(?:\.\d{1,2})?"

# An amount token with an OPTIONAL leading currency marker — either a symbol or a
# 3-letter ISO code — fused to the digits (`$50`, `€50`, `USD50`, `50`). The
# marker only ever rides the amount token; a code floating in the description is
# not a currency (keeps the grammar single-regex and unambiguous).
_MONEY_RE = re.compile(
    rf"^(?:(?P<sym>[$€£¥₹₩])|(?P<code>[A-Za-z]{{3}}))?(?P<amt>{_AMOUNT_BODY})$"
)


def parse_money(token: str, default_currency: str) -> tuple[str, int]:
    """Parse a money token into ``(currency_code, cents)``.

    Resolution order (see ``_SYMBOL_TO_CODE`` for the symbol-ambiguity rationale):

    * explicit 3-letter code fused to the amount (``USD50``) → that code, upper-cased;
    * a known symbol (``€50``) → its mapped code;
    * ``$50`` → the group's ``default_currency`` (the "my group's money" symbol);
    * a bare number (``50``) → ``default_currency``.

    No ISO-4217 registry check — a 3-letter code is trusted by shape only, exactly
    as the DTO layer does (CLAUDE.md: "no currency enum/registry for now"). Raises
    ``ValueError`` on a malformed token or a non-positive amount.
    """
    match = _MONEY_RE.match(token)
    if match is None:
        raise ValueError(f"could not parse amount from {token!r}")
    cents = parse_amount_cents(match.group("amt"))
    code = match.group("code")
    sym = match.group("sym")
    if code is not None:
        currency = code.upper()
    elif sym is not None:
        currency = default_currency if sym == "$" else _SYMBOL_TO_CODE[sym]
    else:
        currency = default_currency
    return currency, cents


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
