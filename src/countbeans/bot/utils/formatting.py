"""Shared display-formatting helpers for the bot layer.

The @-prefix, first-name fallback, and generic placeholder are presentation
concerns and live here, never in DTOs or the service core (CLAUDE.md: display
strings are formatted in the bot layer).
"""

from collections.abc import Collection, Mapping
from uuid import UUID

from countbeans.dto.domain import MemberInfo


def payer_excluded_from_named_split(
    has_named: bool, participant_ids: Collection[UUID], payer_id: UUID
) -> bool:
    """True when a NAMED subset split silently leaves the payer out.

    A named split (the user mentioned participants) intentionally excludes the
    payer unless they mention themselves — "I paid, these people owe me" (see
    docs/spec.md "Participant selection"). The everyday case is "I paid for a
    dinner I also ate", where forgetting to self-mention yields a wrong split, so
    the handler appends a non-blocking nudge when this returns True. The empty
    "split everyone" case (``has_named`` False) always includes the payer, so it
    never nudges.
    """
    return has_named and payer_id not in participant_ids


def format_money(cents: int, currency: str) -> str:
    """Format an unsigned integer cent amount as a display string, e.g. ``USD 12.50``."""
    return f"{currency} {cents // 100}.{cents % 100:02d}"


def display_name(username: str | None, first_name: str | None) -> str:
    """Render a user for display: @handle, else first name, else a generic — but
    never a raw UUID.

    Unreachable-in-practice last branch: a claimed user always has a first name
    (Telegram requires it) and a placeholder always has a @handle, so one of the
    first two always fires; the fallback only guards the degenerate case.
    """
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return "someone"


_SPLIT_MODE_LABELS = {
    "exact": " (by exact amount)",
    "percent": " (by percentage)",
    "weighted": " (by weight)",
}

# Footer appended to a successful expense receipt (inline and wizard) pointing at
# the undo path. /void removes the most recent expense in the current scope, which
# — right after an add — is the one just recorded (its payer/recorder may always
# undo it). One definition so the two entry paths can't drift.
VOID_HINT = "↩️ Made a mistake? /void undoes the most recent expense."


def format_expense_receipt(
    *,
    scoped_event_name: str | None,
    description: str | None,
    amount_cents: int,
    currency: str,
    payer_username: str | None,
    payer_first_name: str | None,
    participants: Collection[MemberInfo],
    shares: Mapping[UUID, int],
    split_mode: str,
) -> list[str]:
    """The core expense-confirmation lines shared by the inline /addexpense handler
    and the interactive wizard: a scope-aware head, the payer, the participant
    list, and each participant's share (echoing how it was divided).

    Context-specific nudges — the coverage-gap warning, the payer-excluded hint,
    and the #general-override confirmation — depend on Telegram state the caller
    holds, so the caller appends those to the returned list.
    """
    if scoped_event_name is not None:
        head = (
            f'✅ Added to "{scoped_event_name}": {description} — {format_money(amount_cents, currency)}'
            if description
            else f'✅ Added to "{scoped_event_name}" — {format_money(amount_cents, currency)}'
        )
    else:
        head = (
            f"Added expense: {description} — {format_money(amount_cents, currency)}"
            if description
            else f"Added expense — {format_money(amount_cents, currency)}"
        )
    mode_label = _SPLIT_MODE_LABELS.get(split_mode, "")
    lines = [
        head,
        f"Paid by: {display_name(payer_username, payer_first_name)}",
        f"Split among: {', '.join(display_name(p.username, p.first_name) for p in participants)}",
        f"Shares{mode_label}:",
    ]
    for p in participants:
        lines.append(
            f"  {display_name(p.username, p.first_name)}: {format_money(shares.get(p.user_id, 0), currency)}"
        )
    return lines


def coverage_gap_warning(known_count: int, gap: int) -> str:
    """The non-blocking warning appended when a whole-group split can only cover
    the members the bot has met. ``known_count`` is who's in the split; ``gap`` is
    how many more the group has that the bot hasn't seen yet. One definition so the
    inline handler and the wizard can't drift."""
    return (
        f"\n⚠️ Split among the {known_count} member(s) I know — {gap} more "
        "haven't interacted yet. Ask them to /join to be included."
    )
