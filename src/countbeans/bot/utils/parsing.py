"""Shared parsing helpers for the bot's command handlers."""

import re
from collections.abc import Sequence

# The reserved "everyone" keyword. It denotes the same word in two grammatical
# families (see CLAUDE.md "The @all / all keyword") — only the spelling differs:
#   * Family 1 — `@all`, a token in the @mention/target namespace, sitting among
#     @username args (/addexpense participants, /settleup target, /event roster).
#     Test an already-extracted @handle (without the @) with `is_all`.
#   * Family 2 — bare `all`, a positional view selector that pairs with `me`
#     (/balance all, /statements all). Test the split args with `is_all_selector`.
# Both go through the same single comparison below, so the keyword lives in one
# place and the two families can never drift apart.
ALL_KEYWORD = "all"


def is_all(token: str) -> bool:
    """True if ``token`` is the reserved everyone-keyword (case-insensitive).

    Family 1 — call on an @handle already stripped of its leading ``@`` (as
    /addexpense, /settleup, and /event do)."""
    return token.casefold() == ALL_KEYWORD


def is_all_selector(args: Sequence[str]) -> bool:
    """True if the first positional arg is the bare ``all`` selector.

    Family 2 — /balance all, /statements all. ``args`` is ``command.args.split()``
    (CommandObject already strips the command token, so the selector is at index 0,
    not index 1)."""
    return bool(args) and is_all(args[0])


# Opening → closing quote characters accepted around a description. Covers the
# straight ASCII quotes and the "smart"/curly quotes mobile keyboards substitute
# automatically (the common reason a typed "…" fails to match) — plus backticks.
# A description must be wrapped in a *matching pair*: a curly opener needs a curly
# closer, etc. (see extract_quoted_description).
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",  # “ ”  curly double
    "‘": "’",  # ‘ ’  curly single
    "«": "»",  # « »  guillemets
    "`": "`",
}


def extract_quoted_description(text: str) -> tuple[str | None, str]:
    """Pull a quoted description out of ``text``, returning
    ``(description, remaining_text)``.

    * Accepts any **matching** quote pair from ``_QUOTE_PAIRS`` (so a curly
      opener is only closed by its curly partner, never a straight quote).
    * A backslash **escapes the next character**, so the closing quote can appear
      inside the description (``"she said \\"hi\\""`` → ``she said "hi"``) and a
      literal backslash is written ``\\\\``.
    * Scans left to right for the first opener that has a valid matching close;
      an unmatched opener (e.g. an apostrophe in ``it's``) is skipped, not
      treated as the start of a quote, so a later real quote still wins.
    * Returns ``(None, text)`` unchanged when there is no quoted run; an empty
      quote (``""``) yields ``None`` too.

    The matched run (quotes included) is removed from ``remaining_text`` so the
    caller can then scan it for @mentions without seeing anything inside the
    description.
    """
    n = len(text)
    i = 0
    while i < n:
        opener = text[i]
        closer = _QUOTE_PAIRS.get(opener)
        if closer is not None:
            chars: list[str] = []
            j = i + 1
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    chars.append(text[j + 1])  # escape: take the next char literally
                    j += 2
                    continue
                if c == closer:
                    description = "".join(chars) or None
                    return description, text[:i] + text[j + 1 :]
                chars.append(c)
                j += 1
            # Opener had no matching closer — not a quote; keep scanning past it.
        i += 1
    return None, text


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

# Upper bound on a single parsed amount, in cents. Amounts are stored as BIGINT
# (max ~9.2e18), and `_AMOUNT_BODY` permits an unbounded integer part, so without
# a cap a 20-digit token would overflow the column on INSERT and surface as an
# unhandled DB error instead of a clean rejection. 10**15 cents (ten trillion
# major units) dwarfs any real expense yet leaves ample BIGINT headroom, so no
# row can overflow.
MAX_AMOUNT_CENTS = 10**15

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
    this raises ``ValueError`` on a non-positive result or one exceeding
    ``MAX_AMOUNT_CENTS`` (which would otherwise overflow the BIGINT column).
    """
    if "." in s:
        integer_part, decimal_part = s.split(".", 1)
        decimal_part = decimal_part.ljust(2, "0")[:2]
    else:
        integer_part, decimal_part = s, "00"
    cents = int(integer_part) * 100 + int(decimal_part)
    if cents <= 0:
        raise ValueError("amount must be positive")
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("amount is too large")
    return cents
