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
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.formatting import display_name
from countbeans.bot.parsing import extract_quoted_description, parse_money
from countbeans.dto.commands import AddExpenseCommand
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
    "• Quote the description with any matching pair — \"…\", '…', or curly "
    "“…”/‘…’ (handy on phones). Escape a quote inside with a backslash: "
    '"she said \\"hi\\"".\n'
    "Prefix the amount with a currency to override the default: $50, €50, USD50."
)


def _money(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


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

    payer = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    # The amount token may carry a currency marker ($50, €50, USD50); resolve it
    # against the group default. Mixed currencies are fine — balances derive
    # per-currency (see CLAUDE.md "Deriving balances").
    try:
        currency, amount_cents = parse_money(tokens[0], group.default_currency)
    except ValueError:
        await message.reply("Invalid amount. Use a positive number like 25.50")
        return

    rest = " ".join(tokens[1:])

    # Pull out an optional quoted description (any matching quote pair, escapes
    # honored), then scan whatever's left for @mentions — so an @ or quote inside
    # the description is never mistaken for a participant.
    description, rest = extract_quoted_description(rest)
    mentions = _MENTION_RE.findall(rest)
    participants = await resolve_participants(uow, group.id, payer.id, mentions)

    try:
        cmd = AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[p.user_id for p in participants],
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

    lines = [
        f"Added expense: {description or 'expense'} — {_money(result.amount_cents, result.currency)}",
        f"Paid by: {display_name(payer.username, payer.first_name)}",
        f"Split among: {', '.join(display_name(p.username, p.first_name) for p in participants)}",
        "Shares:",
    ]
    for p in participants:
        lines.append(f"  {display_name(p.username, p.first_name)}: {_money(result.shares.get(p.user_id, 0), result.currency)}")

    # When splitting the whole group, warn if the bot can't see everyone — it
    # can only split among members who've interacted (CLAUDE.md "Onboarding").
    if not mentions or all(h.lower() == "all" for h in mentions):
        try:
            actual = await bot.get_chat_member_count(message.chat.id) - 1  # minus the bot
            if len(participants) < actual:
                gap = actual - len(participants)
                lines.append(
                    f"\n⚠️ Split among the {len(participants)} member(s) I know — "
                    f"{gap} more haven't interacted yet. Ask them to /start to be included."
                )
        except Exception:
            logger.warning("could not fetch chat member count for %s", message.chat.id)

    await message.reply("\n".join(lines))
    logger.info(
        "Expense recorded: expense_id=%s amount_cents=%d participants=%d",
        result.expense_id,
        result.amount_cents,
        len(participants),
    )
