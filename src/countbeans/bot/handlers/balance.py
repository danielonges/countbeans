"""Bot handler for /balance and /balance all."""
import logging
import uuid

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.services.balance import get_group_summary
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _fmt(cents: int, currency: str) -> str:
    sign = "+" if cents >= 0 else "-"
    abs_cents = abs(cents)
    return f"{sign}{currency} {abs_cents // 100}.{abs_cents % 100:02d}"


def _amount(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


def _name(username_by_id: dict[uuid.UUID, str | None], uid: uuid.UUID) -> str:
    username = username_by_id.get(uid)
    return f"@{username}" if username else str(uid)


@router.message(Command("balance"))
async def cmd_balance(message: Message, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    caller = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    text = (message.text or "").strip()
    parts = text.split()
    show_all = len(parts) > 1 and parts[1].lower() == "all"

    summary = await get_group_summary(uow, group.id, group.simplify_debts)
    username_by_id = {b.user_id: b.username for b in summary.balances}

    if show_all:
        if not summary.balances:
            await message.reply("No outstanding balances in this group.")
            return

        lines = ["Group balances:"]
        for b in sorted(summary.balances, key=lambda x: -x.balance_cents):
            lines.append(f"  {_name(username_by_id, b.user_id)}: {_fmt(b.balance_cents, b.currency)}")

        if summary.suggested_transfers:
            heading = "simplified" if group.simplify_debts else "raw"
            lines.append(f"\nSuggested transfers ({heading}):")
            for t in summary.suggested_transfers:
                lines.append(
                    f"  {_name(username_by_id, t.from_user_id)} → "
                    f"{_name(username_by_id, t.to_user_id)}: {_amount(t.amount_cents, t.currency)}"
                )

        await message.reply("\n".join(lines))
        return

    # Personal view: the caller's net position, then who to settle with — the
    # suggested transfers involving the caller (honoring the group's simplify
    # toggle, so it agrees with /balance all and /settleup).
    my_balances = [b for b in summary.balances if b.user_id == caller.id]
    if not my_balances:
        await message.reply("You have no outstanding balances in this group.")
        return

    lines = ["Your balance:"]
    for b in my_balances:
        direction = "you're owed" if b.balance_cents > 0 else "you owe"
        lines.append(f"  {_fmt(b.balance_cents, b.currency)} ({direction})")

    owed_to_me = [t for t in summary.suggested_transfers if t.to_user_id == caller.id]
    i_owe = [t for t in summary.suggested_transfers if t.from_user_id == caller.id]

    heading = "simplified" if group.simplify_debts else "raw"
    if owed_to_me or i_owe:
        lines.append(f"\nTo settle up ({heading}):")
    for t in owed_to_me:
        lines.append(f"  {_name(username_by_id, t.from_user_id)} pays you {_amount(t.amount_cents, t.currency)}")
    for t in i_owe:
        lines.append(f"  you pay {_name(username_by_id, t.to_user_id)} {_amount(t.amount_cents, t.currency)}")

    await message.reply("\n".join(lines))
