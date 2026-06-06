"""Bot handler for the /addexpense command.

Parses: /addexpense <amount> ["<description>"] [@user1 @user2 ...]

Participant rules (see CLAUDE.md "Splitting an expense"):
  * no @mentions (or @all)  → split among everyone the bot knows in the group;
  * one or more @mentions   → split among only those named (you are NOT
    included unless you @mention yourself).
"""

import logging
import re

from aiogram import Bot, Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.utils.formatting import (
    display_name,
    format_money,
    payer_excluded_from_named_split,
)
from countbeans.bot.utils.parsing import (
    extract_quoted_description,
    has_split_suffix,
    is_all,
    parse_money,
    unquoted_description,
)
from countbeans.dto.commands import AddExpenseCommand, MentionedUser
from countbeans.services.add_expense import add_expense, resolve_participants
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

_MENTION_RE = re.compile(r"@([\w.]+)")
_USAGE = (
    'Usage: /addexpense <amount> ["description"] [@user ...]\n'
    'Example: /addexpense 25.50 "Dinner" @alice @bob\n'
    "• No @mentions (or @all) → split among everyone in the group.\n"
    "• Name people → split among only them; you're not included unless you "
    "@mention yourself.\n"
    "• Quotes are optional — an unquoted description is the words before the "
    "first @mention; quote it only if it contains an @.\n"
    "• Quote the description with any matching pair — \"...\", '...', "
    "“...”/‘...’ (curly, handy on phones), "
    "«...», or `...`. Escape a quote inside with a backslash: "
    '"she said \\"hi\\"".\n'
    "• Prefix the amount with a currency to override the default: $50, €50, USD50."
)


@router.message(Command("addexpense"))
async def cmd_addexpense(
    message: Message, command: CommandObject, uow: UnitOfWork, bot: Bot
) -> None:
    if message.from_user is None:
        return

    # command.args is everything after the command — already stripped of both
    # "/addexpense" and any "@botname" suffix Telegram appends in groups, so a
    # bare "/addexpense" (or "/addexpense@bot") yields no args and shows usage.
    tokens = (command.args or "").split()

    if not tokens:
        await message.reply(_USAGE)
        return

    # Group first: the placeholder-claim in upsert is group-scoped (claim_in_group).
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )
    payer = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        claim_in_group=group.id,
    )

    # Active-event mode: when the group has an active event, auto-tag this expense
    # to it and split the event roster (not the whole group) — CLAUDE.md "Events".
    active = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    event_id = active.id if active else None

    # The amount token may carry a currency marker ($50, €50, USD50); fall back to
    # the active event's currency, then the group default. Mixed currencies are fine
    # — balances derive per-currency (see CLAUDE.md "Deriving balances").
    scope_currency = (
        active.default_currency if active else None
    ) or group.default_currency
    try:
        currency, amount_cents = parse_money(tokens[0], scope_currency)
    except ValueError:
        await message.reply("Invalid amount. Use a positive number like 25.50")
        return

    rest = " ".join(tokens[1:])

    # Pull out an optional quoted description (any matching quote pair, escapes
    # honored), then scan whatever's left for @mentions — so an @ or quote inside
    # the description is never mistaken for a participant.
    description, rest = extract_quoted_description(rest)
    # No quoted description → fall back to the unquoted run of words between the
    # amount and the first @mention (spec /addexpense rule 2). A quoted
    # description always wins; `rest` is left untouched so @mentions still parse
    # from it exactly as before (a standalone "USD" word becomes description, not
    # a currency — the fused-marker rule on the amount token is unaffected).
    if description is None:
        description = unquoted_description(rest)
    # @all is bot-grammar for "split everyone" — strip it here so the service sees
    # only real handles (an empty list then *means* everyone). Mixing @all with
    # named handles drops the @all and splits among the named (CLAUDE.md).
    named = [h for h in _MENTION_RE.findall(rest) if not is_all(h)]
    # A text_mention entity carries a real telegram_user_id (a user without a public
    # @handle, or a tap-selected one) → resolve to a claimed user, never a username
    # placeholder (security review #1). NOTE: entities are read from the whole
    # message, so a tap-mention placed inside the quoted description would also be
    # counted as a participant (typed @handles inside it are already stripped). Rare,
    # and the echoed "Split among" line surfaces it.
    mentioned = [
        MentionedUser(
            telegram_user_id=e.user.id,
            username=e.user.username,
            first_name=e.user.first_name,
            last_name=e.user.last_name,
        )
        for e in (message.entities or [])
        if e.type == MessageEntityType.TEXT_MENTION and e.user is not None
    ]
    # Safety net: uneven-split suffixes (@alice:40, @bob:60%, @alice:2x) aren't
    # implemented yet — the mention regex stops at the ':' and would silently drop
    # the suffix, recording an EQUAL split with a ✅ (a silent money error). Until
    # uneven splits land (deferred Should-have), reject instead. Scanned on `rest`
    # (the mention region after the quoted description was removed), so a ':' inside
    # the description doesn't trip it. Checked *before* resolve_participants so a
    # rejected command never creates placeholder users / roster rows as a side effect.
    if has_split_suffix(rest):
        await message.reply(
            "Uneven splits (e.g. @alice:40, @bob:60%, @alice:2x) aren't supported "
            "yet — everyone named is split equally. Remove the ':' parts to record "
            "an equal split."
        )
        return

    participants = await resolve_participants(
        uow, group.id, payer.id, named, mentioned_users=mentioned, event_id=event_id
    )

    try:
        cmd = AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[p.user_id for p in participants],
            event_id=event_id,
            created_by=payer.id,
        )
    except Exception as exc:
        logger.warning("Invalid /addexpense command: %s", exc)
        await message.reply(f"Invalid command: {exc}")
        return

    # add_expense raises ValueError with a user-facing message when a split
    # doesn't reconcile (percentages ≠ 100, exact shares ≠ amount, etc.).
    try:
        result = await add_expense(uow, cmd)
    except ValueError as exc:
        await message.reply(str(exc))
        return

    # Every scoped reply echoes the scope so a "sticky" active event can't quietly
    # mis-file an expense (CLAUDE.md "Events"). General tracking keeps its wording.
    if active is not None:
        head = (
            f'✅ Added to "{active.name}": {description} — {format_money(result.amount_cents, result.currency)}'
            if description
            else f'✅ Added to "{active.name}" — {format_money(result.amount_cents, result.currency)}'
        )
    else:
        head = (
            f"Added expense: {description} — {format_money(result.amount_cents, result.currency)}"
            if description
            else f"Added expense — {format_money(result.amount_cents, result.currency)}"
        )
    lines = [
        head,
        f"Paid by: {display_name(payer.username, payer.first_name)}",
        f"Split among: {', '.join(display_name(p.username, p.first_name) for p in participants)}",
        "Shares:",
    ]
    for p in participants:
        lines.append(
            f"  {display_name(p.username, p.first_name)}: {format_money(result.shares.get(p.user_id, 0), result.currency)}"
        )

    # When splitting the whole group, warn if the bot can't see everyone — it can
    # only split among members who've interacted (CLAUDE.md "Onboarding"). Inside
    # an event, @all means the roster (an intentional subset), so this gate is skipped.
    if active is None and not named and not mentioned:
        try:
            actual = (
                await bot.get_chat_member_count(message.chat.id) - 1
            )  # minus the bot
            if len(participants) < actual:
                gap = actual - len(participants)
                lines.append(
                    f"\n⚠️ Split among the {len(participants)} member(s) I know — "
                    f"{gap} more haven't interacted yet. Ask them to /join to be included."
                )
        except Exception:
            logger.warning(
                "could not fetch chat member count for %s",
                message.chat.id,
                exc_info=True,
            )

    # A named subset split intentionally excludes the payer ("I paid, these owe
    # me") — but the everyday case is a shared expense the payer also took part
    # in. The split is still recorded as named; this is a non-blocking nudge to
    # self-mention if they should have been included (the expense is unchanged).
    if payer_excluded_from_named_split(
        bool(named or mentioned), cmd.participants, payer.id
    ):
        lines.append(
            "\nℹ️ You're not included in this split. If you shared this expense "
            "too, @mention your own handle to be added."
        )

    # Active events are "sticky": this expense auto-tagged to the event, and
    # logging a *general* expense needs /event pause (admin-only). Say so, so a
    # non-admin isn't left wondering why their expense landed on the event tab.
    if active is not None:
        lines.append(
            "\nℹ️ To record a general (non-event) expense instead, an admin can "
            "/event pause first."
        )

    await message.reply("\n".join(lines))
    logger.info(
        "Expense recorded: expense_id=%s amount_cents=%d participants=%d",
        result.expense_id,
        result.amount_cents,
        len(participants),
    )
