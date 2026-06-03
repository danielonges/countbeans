"""Bot handler for the /addexpense command.

Parses: /addexpense <amount> ["<description>"] [@user1 @user2 ...]
"""
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.parsing import parse_money
from countbeans.dto.commands import AddExpenseCommand
from countbeans.services.add_expense import add_expense
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

_MENTION_RE = re.compile(r"@([\w.]+)")
_USAGE = (
    'Usage: /addexpense <amount> ["description"] [@user1 @user2 ...]\n'
    'Example: /addexpense 25.50 "Dinner" @alice @bob\n'
    "Prefix the amount with a currency to override the group default: "
    "$50, €50, USD50."
)


@router.message(Command("addexpense"))
async def cmd_addexpense(message: Message, uow: UnitOfWork) -> None:
    if message.text is None or message.from_user is None:
        return

    text = re.sub(r"^/addexpense\s*", "", message.text.strip(), flags=re.IGNORECASE)
    tokens = text.split()

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

    # Extract optional quoted description
    description: str | None = None
    quoted = re.search(r'"([^"]*)"', rest)
    if quoted:
        description = quoted.group(1) or None
        rest = rest[: quoted.start()] + rest[quoted.end() :]

    mentions = _MENTION_RE.findall(rest)

    participant_users = [payer]
    seen_ids = {payer.id}
    for handle in mentions:
        if handle == (message.from_user.username or ""):
            continue
        u = await uow.users.resolve_mention(handle)
        if u.id not in seen_ids:
            participant_users.append(u)
            seen_ids.add(u.id)

    for u in participant_users:
        await uow.group_members.ensure_member(group.id, u.id)

    try:
        cmd = AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[u.id for u in participant_users],
            created_by=payer.id,
        )
    except Exception as exc:
        logger.warning("Invalid /addexpense command: %s", exc)
        await message.reply(f"Invalid command: {exc}")
        return

    result = await add_expense(uow, cmd)

    major, minor = result.amount_cents // 100, result.amount_cents % 100
    payer_name = f"@{payer.username}" if payer.username else "you"
    participant_names = [
        f"@{u.username}" if u.username else str(u.id) for u in participant_users
    ]
    share_lines = [
        f"  {name}: {result.currency} {result.shares.get(u.id, 0) // 100}.{result.shares.get(u.id, 0) % 100:02d}"
        for u, name in zip(participant_users, participant_names)
    ]

    await message.reply(
        f"Added expense: {description or 'expense'} — {result.currency} {major}.{minor:02d}\n"
        f"Paid by: {payer_name}\n"
        f"Split among: {', '.join(participant_names)}\n"
        f"Shares:\n" + "\n".join(share_lines)
    )
    logger.info(
        "Expense recorded: expense_id=%s amount_cents=%d",
        result.expense_id,
        result.amount_cents,
    )
