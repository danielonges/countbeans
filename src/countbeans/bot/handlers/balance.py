"""Bot handler for /balance and /balance all."""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.services.balance import compute_balances, get_group_summary
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _fmt(cents: int, currency: str) -> str:
    sign = "+" if cents >= 0 else "-"
    abs_cents = abs(cents)
    return f"{sign}{currency} {abs_cents // 100}.{abs_cents % 100:02d}"


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

    if show_all:
        summary = await get_group_summary(uow, group.id, group.simplify_debts)
        if not summary.balances:
            await message.reply("No outstanding balances in this group.")
            return

        username_by_id = {b.user_id: b.username for b in summary.balances}
        lines = ["Group balances:"]
        for b in sorted(summary.balances, key=lambda x: -x.balance_cents):
            name = f"@{b.username}" if b.username else str(b.user_id)
            lines.append(f"  {name}: {_fmt(b.balance_cents, b.currency)}")

        if summary.suggested_transfers:
            heading = "simplified" if group.simplify_debts else "raw"
            lines.append(f"\nSuggested transfers ({heading}):")
            for t in summary.suggested_transfers:
                from_name = f"@{username_by_id.get(t.from_user_id) or t.from_user_id}"
                to_name = f"@{username_by_id.get(t.to_user_id) or t.to_user_id}"
                lines.append(
                    f"  {from_name} → {to_name}: "
                    f"{t.currency} {t.amount_cents // 100}.{t.amount_cents % 100:02d}"
                )

        await message.reply("\n".join(lines))
    else:
        raw = await compute_balances(uow, group.id)
        my_balances = {
            key.currency: cents for key, cents in raw.items() if key.user_id == caller.id
        }

        if not my_balances:
            await message.reply("You have no outstanding balances in this group.")
            return

        lines = ["Your balance:"]
        for cur, cents in my_balances.items():
            direction = "group owes you" if cents > 0 else "you owe the group"
            lines.append(f"  {_fmt(cents, cur)} ({direction})")
        await message.reply("\n".join(lines))
