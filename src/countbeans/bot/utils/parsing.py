"""Shared parsing helpers for the bot's command handlers."""

import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

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


# The bare positional selector ``me`` — the explicit "just mine" twin of ``all``.
# Both /balance and /statements accept it (and bare = personal too), so the two
# read commands take the *same* selector vocabulary.
_ME_SELECTOR = "me"


def parse_view_selector(args: Sequence[str]) -> tuple[bool, str | None]:
    """Classify a read command's view selector into ``(group_wide, unrecognized)``.

    ``group_wide`` is True only for ``all``; bare and ``me`` are the personal
    view (False). ``unrecognized`` is the first arg when it is *neither* a known
    selector nor absent — the command still answers with the personal view, but
    the handler can prepend a one-line "I didn't recognize 'x'" note rather than
    silently swallowing the typo (CLAUDE-grade forgiving-but-not-silent)."""
    if not args:
        return False, None
    if is_all_selector(args):
        return True, None
    if args[0].casefold() == _ME_SELECTOR:
        return False, None
    return False, args[0]


# The reserved write-scope override keyword, in its own ``#``-prefixed namespace
# (distinct from the ``@mention`` target family and the bare view-selector family
# above — see CLAUDE.md "The #general write-scope override"). On /addexpense and
# /settleup it forces THIS one write to the general (no-event) scope even while an
# event is active — a one-off escape hatch that needs no /event pause and so can't
# leave a forgotten-resume mis-filing later expenses. One spelling, one matcher,
# so the two commands can never drift.
GENERAL_KEYWORD = "general"

# Whole-token, case-insensitive: ``#general`` bounded by start/whitespace on both
# sides, so ``#generals`` or a ``#general`` glued to other text never matches.
_GENERAL_FLAG_RE = re.compile(rf"(?i)(?<!\S)#{GENERAL_KEYWORD}(?!\S)")


def extract_general_flag(text: str) -> tuple[str, bool]:
    """Strip the reserved ``#general`` override token, returning
    ``(remaining_text, present)``.

    Matches the whole ``#general`` token (case-insensitive) anywhere in ``text``,
    so it may sit before or after the @mentions. Callers run this on the region
    *after* a quoted description has been removed, so a literal ``#general`` inside
    quotes is preserved as description text rather than read as the override.
    """
    cleaned, n = _GENERAL_FLAG_RE.subn(" ", text)
    if not n:
        return text, False
    return re.sub(r"\s{2,}", " ", cleaned).strip(), True


# A participant token: ``@handle`` (equal) or ``@handle:<suffix>`` for an uneven
# split — ``@a:30`` (exact cents), ``@a:60%`` (percentage), ``@a:2x`` (weight).
# The suffix is any run of non-space, non-``@`` chars after the colon; its meaning
# is classified by ``_parse_suffix``.
_PARTICIPANT_RE = re.compile(r"@([\w.]+)(?::([^\s@]+))?")

# The split mode inferred from the suffix family. "equal" has no suffixes.
SplitMode = Literal["equal", "exact", "percent", "weighted"]


class ParsedSplit(NamedTuple):
    """The result of parsing a /addexpense mention region.

    * ``mode``    — inferred split mode (see ``SplitMode``).
    * ``handles`` — participant @handles (without the ``@``), ``@all`` removed,
      **exact-string-deduped** (first wins, order preserved) so the list stays
      1:1 with ``resolve_participants``' id-deduped output.
    * ``params``  — ``handle -> int`` in the mode's unit (exact: cents; percent:
      whole percent; weighted: integer weight), or ``None`` for an equal split.
    """

    mode: SplitMode
    handles: list[str]
    params: dict[str, int] | None


def _parse_suffix(
    handle: str, suffix: str | None
) -> tuple[Literal["exact", "percent", "weighted"] | None, int]:
    """Classify a single mention's split suffix into ``(family, value)``.

    ``None`` family means no suffix (an equal contributor). Raises ``ValueError``
    with a user-facing message on a malformed suffix. Percentages and weights are
    **whole numbers**; an exact amount is money (``parse_amount_cents`` — ≤2dp,
    positive). Sums are validated by the caller, not here."""
    if suffix is None:
        return None, 0
    if suffix.endswith("%"):
        body = suffix[:-1]
        if not body.isdigit():
            raise ValueError(
                f"Percentages must be whole numbers — @{handle}:{suffix} isn't "
                f"(try @{handle}:60%)."
            )
        return "percent", int(body)
    if suffix.endswith(("x", "X")):
        body = suffix[:-1]
        if not body.isdigit():
            raise ValueError(
                f"Weights must be whole numbers — @{handle}:{suffix} isn't "
                f"(try @{handle}:2x)."
            )
        return "weighted", int(body)
    try:
        return "exact", parse_amount_cents(suffix)
    except ValueError as exc:
        raise ValueError(
            f"@{handle}:{suffix} isn't a valid amount — use an amount like "
            f"@{handle}:30 or @{handle}:30.50."
        ) from exc


def parse_participants(mention_region: str) -> ParsedSplit:
    """Parse the @mention region of /addexpense into participants and a split mode.

    Each token is ``@handle`` (equal) or ``@handle:<suffix>`` for an uneven split
    (``@a:30`` exact / ``@a:60%`` percentage / ``@a:2x`` weight). The mode is the
    suffix family and must be uniform across the named participants. ``@all`` is
    the everyone-keyword (``is_all``): it is dropped from ``handles`` (an empty
    list then *means* everyone) and may never carry a suffix.

    Raises ``ValueError`` (with a user-facing message) on a malformed split —
    mixed families, a participant missing its share, a bad number, or a suffix on
    ``@all``. The handler calls this **before** ``resolve_participants`` so a
    rejected command never creates placeholder users. Sums (percent → 100, exact
    → amount) are checked by the handler, where the amount/currency are known.
    """
    handles: list[str] = []
    seen: set[str] = set()
    # handle -> (family, value); family None means an equal contributor (no suffix).
    parsed: dict[str, tuple[Literal["exact", "percent", "weighted"] | None, int]] = {}

    for match in _PARTICIPANT_RE.finditer(mention_region):
        handle, suffix = match.group(1), match.group(2)
        if is_all(handle):
            if suffix is not None:
                raise ValueError("@all can't carry a split amount — drop the ':' part.")
            continue  # the everyone-keyword, never a share target
        if handle in seen:
            continue  # exact-string dedup; first occurrence (and its share) wins
        seen.add(handle)
        handles.append(handle)
        parsed[handle] = _parse_suffix(handle, suffix)

    families: set[SplitMode] = {fam for fam, _ in parsed.values() if fam is not None}
    if not families:
        return ParsedSplit("equal", handles, None)
    if len(families) > 1:
        raise ValueError(
            "Don't mix split types in one command — use all exact amounts, all "
            "percentages (@a:60%), or all weights (@a:2x)."
        )
    (family,) = families
    missing = [h for h, (fam, _) in parsed.items() if fam is None]
    if missing:
        raise ValueError(
            f"In an uneven split everyone needs a share — @{missing[0]} is missing "
            f"one (e.g. @{missing[0]}:30, @{missing[0]}:60%, or @{missing[0]}:2x)."
        )
    return ParsedSplit(family, handles, {h: val for h, (_, val) in parsed.items()})


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


def unquoted_description(text: str) -> str | None:
    """The unquoted description: the run of words before the first ``@`` mention.

    Per the spec (``/addexpense`` grammar rule 2), when there is no *quoted*
    description the description is "the run of words between the amount and the
    first @mention". The amount token is already consumed by the caller, so
    ``text`` is everything after it; we take the substring up to the first literal
    ``@`` and trim surrounding whitespace. Returns ``None`` when that run is empty
    (e.g. ``"@bob"`` or ``""``), so the caller can distinguish "no description".

    The quoted path (``extract_quoted_description``) always wins — call this only
    as the fallback when it returns ``None``.

    NOTE: a ``text_mention`` entity (a tap-selected user with no public ``@``
    handle) carries no literal ``@`` in the message text, so its display name
    could land inside this unquoted run. This is rare and acceptable — the reply
    echoes the resulting split.
    """
    at = text.find("@")
    run = (text if at == -1 else text[:at]).strip()
    return run or None


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


def explicit_currency(token: str) -> str | None:
    """The currency a money token pins independently of any default — a fused
    ISO code (``USD50``) or an unambiguous symbol (``€50``). ``None`` for a
    bare amount or ``$``: both mean "the scope's own currency" (see
    ``_SYMBOL_TO_CODE`` for why ``$`` floats), so they should follow the scope
    if it later changes — the wizard's #general toggle re-derives them."""
    match = _MONEY_RE.match(token)
    if match is None:
        return None
    code = match.group("code")
    if code is not None:
        return code.upper()
    sym = match.group("sym")
    if sym is not None and sym != "$":
        return _SYMBOL_TO_CODE[sym]
    return None


def looks_like_money(token: str) -> bool:
    """Whether ``token`` would parse as an amount (with or without a currency
    marker). A diagnostic predicate for "an amount, but in the wrong place"
    nudges — /addexpense's wrong-order hint and the wizard's non-reply nudge —
    never used to record money, so the default currency passed is irrelevant."""
    try:
        parse_money(token, "USD")
    except ValueError:
        return False
    return True


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
