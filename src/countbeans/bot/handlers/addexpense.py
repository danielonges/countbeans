"""Bot handler for the /addexpense command.

Parses: /addexpense <amount> ["<description>"] [@user1 @user2 ...]

Participant rules (see CLAUDE.md "Splitting an expense"):
  * no @mentions (or @all)  → split among everyone the bot knows in the group;
  * one or more @mentions   → split among only those named (you are NOT
    included unless you @mention yourself).
"""

import logging

from aiogram import Bot, Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from countbeans.bot.handlers.addexpense_wizard import start_wizard
from countbeans.bot.utils.formatting import (
    VOID_HINT,
    coverage_gap_warning,
    format_expense_receipt,
    format_money,
    payer_excluded_from_named_split,
)
from countbeans.bot.utils.parsing import (
    extract_general_flag,
    extract_quoted_description,
    parse_money,
    parse_participants,
    unquoted_description,
)
from countbeans.dto.commands import AddExpenseCommand, MentionedUser
from countbeans.services.add_expense import add_expense, resolve_participants
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("addexpense"))
async def cmd_addexpense(
    message: Message,
    command: CommandObject,
    uow: UnitOfWork,
    bot: Bot,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return

    # command.args is everything after the command — already stripped of both
    # "/addexpense" and any "@botname" suffix Telegram appends in groups, so a
    # bare "/addexpense" (or "/addexpense@bot") yields no args. That starts the
    # interactive, button-driven wizard (addexpense_wizard.py); the inline
    # one-liner below still serves a command with args.
    tokens = (command.args or "").split()

    if not tokens:
        await start_wizard(message, state, uow)
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

    rest = " ".join(tokens[1:])

    # Pull out an optional quoted description (any matching quote pair, escapes
    # honored), then the reserved #general override, then scan whatever's left for
    # @mentions — so neither an @/quote inside the description nor the flag is
    # mistaken for a participant.
    description, rest = extract_quoted_description(rest)
    # #general forces THIS one expense to general (no-event) scope even while an
    # event is active — a one-off escape hatch that needs no /event pause (CLAUDE.md
    # "The #general write-scope override"). Stripped from the unquoted region only,
    # so a literal #general inside a quoted description stays text.
    rest, force_general = extract_general_flag(rest)
    # No quoted description → fall back to the unquoted run of words between the
    # amount and the first @mention (spec /addexpense rule 2), now also free of the
    # #general flag. A quoted description always wins; `rest` is otherwise left
    # untouched so @mentions still parse from it exactly as before (a standalone
    # "USD" word becomes description, not a currency — the fused-marker rule on the
    # amount token is unaffected).
    if description is None:
        description = unquoted_description(rest)

    # The effective scope: the active event, unless #general overrode it for this
    # command. event_id, the currency fallback, the reply head, and the coverage
    # check all key off `scoped_event` (not `active`), so an override behaves
    # exactly like "no active event".
    scoped_event = None if force_general else active
    event_id = scoped_event.id if scoped_event else None

    # The amount token may carry a currency marker ($50, €50, USD50); fall back to
    # the scoped event's currency, then the group default. Mixed currencies are
    # fine — balances derive per-currency (see CLAUDE.md "Deriving balances").
    scope_currency = (
        scoped_event.default_currency if scoped_event else None
    ) or group.default_currency
    try:
        currency, amount_cents = parse_money(tokens[0], scope_currency)
    except ValueError:
        await message.reply("Invalid amount. Use a positive number like 25.50")
        return
    # Parse the @mention region into participants + split mode. @all is bot-grammar
    # for "split everyone" (parse_participants drops it → an empty handle list
    # *means* everyone). Uneven-split suffixes (@a:30, @a:60%, @a:2x) set the mode;
    # a malformed split (mixed families, a missing share, a bad number) raises
    # ValueError HERE — before resolve_participants — so a rejected command never
    # creates placeholder users / roster rows as a side effect. Scanned on `rest`
    # (the region after the quoted description was removed), so a ':' or @ inside
    # the description doesn't trip it.
    try:
        split = parse_participants(rest)
    except ValueError as exc:
        await message.reply(str(exc))
        return
    named = split.handles
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
    # An uneven split is expressed via typed @handle:share tokens; a tapped mention
    # (text_mention) can't carry a suffix, so it has no share in a non-equal split.
    if split.mode != "equal" and mentioned:
        await message.reply(
            "Uneven splits don't support tapped mentions yet — name everyone with a "
            "typed @handle and a share (e.g. @alice:30)."
        )
        return
    # Validate the split totals before resolve_participants (friendly messages, and
    # no placeholder side effects on a bad command); compute_shares re-checks these
    # inside the transaction as a backstop.
    if split.params is not None:
        total = sum(split.params.values())
        if split.mode == "percent" and total != 100:
            await message.reply(
                f"Percentages must add up to 100% — yours total {total}%."
            )
            return
        if split.mode == "exact" and total != amount_cents:
            await message.reply(
                "Exact amounts must add up to "
                f"{format_money(amount_cents, currency)} — yours total "
                f"{format_money(total, currency)}."
            )
            return

    participants = await resolve_participants(
        uow, group.id, payer.id, named, mentioned_users=mentioned, event_id=event_id
    )

    # In a non-equal split, map each resolved participant to its parsed share.
    # split.handles is exact-string-deduped and order-preserved, and (no tapped
    # mentions in a non-equal split) resolve_participants returns exactly those
    # users 1:1 in the same order — so zip is sound (see its docstring).
    split_params = (
        {p.user_id: split.params[h] for h, p in zip(named, participants)}
        if split.params is not None
        else None
    )

    try:
        cmd = AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[p.user_id for p in participants],
            split_mode=split.mode,
            split_params=split_params,
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

    # The shared receipt body (scope-aware head + paid-by + split-among + shares).
    # Every scoped reply echoes the scope so a "sticky" active event can't quietly
    # mis-file an expense (CLAUDE.md "Events"). General tracking keeps its wording.
    lines = format_expense_receipt(
        scoped_event_name=scoped_event.name if scoped_event is not None else None,
        description=description,
        amount_cents=result.amount_cents,
        currency=result.currency,
        payer_username=payer.username,
        payer_first_name=payer.first_name,
        participants=participants,
        shares=result.shares,
        split_mode=split.mode,
    )

    # When splitting the whole group, warn if the bot can't see everyone — it can
    # only split among members who've interacted (CLAUDE.md "Onboarding"). Inside
    # an event, @all means the roster (an intentional subset), so this gate is
    # skipped — but a #general override is a whole-group general split, so it warns.
    if scoped_event is None and not named and not mentioned:
        try:
            actual = (
                await bot.get_chat_member_count(message.chat.id) - 1
            )  # minus the bot
            if len(participants) < actual:
                lines.append(
                    coverage_gap_warning(len(participants), actual - len(participants))
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

    # A #general override while an event is active: confirm it stayed general so a
    # deliberate escape-hatch use is visible (the head already omits the event).
    # No per-reply nudge on ordinary event expenses — the scope echo above is the
    # signal, and #general is the one-step way to opt out (CLAUDE.md "Events").
    if force_general and active is not None:
        lines.append(f'\nℹ️ Logged as general — not tagged to "{active.name}".')

    lines.append(f"\n{VOID_HINT}")
    await message.reply("\n".join(lines))
    logger.info(
        "Expense recorded: expense_id=%s amount_cents=%d participants=%d",
        result.expense_id,
        result.amount_cents,
        len(participants),
    )
