"""Bot handler for /balance and /balance all."""

import logging
import uuid

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import is_all_selector
from countbeans.services.balance import get_group_summary
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _fmt(cents: int, currency: str) -> str:
    sign = "+" if cents >= 0 else "-"
    abs_cents = abs(cents)
    return f"{sign}{currency} {abs_cents // 100}.{abs_cents % 100:02d}"


@router.message(Command("balance"))
async def cmd_balance(
    message: Message, command: CommandObject, uow: UnitOfWork
) -> None:
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
    await uow.group_members.ensure_member(group.id, caller.id)

    args = (command.args or "").split()
    show_all = is_all_selector(args)

    # /balance defaults to the active event's scope (general when none is active).
    # Named cross-scope reads (/balance general, /balance "<event>") are deferred.
    active = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    scope_in = f' in "{active.name}"' if active else ""
    summary = await get_group_summary(
        uow, group.id, group.simplify_debts, event_id=active.id if active else None
    )
    display_by_id: dict[uuid.UUID, str] = {
        b.user_id: display_name(b.username, b.first_name) for b in summary.balances
    }

    if show_all:
        if not summary.balances:
            await message.reply(f"No outstanding balances{scope_in}.")
            return

        lines = [f'Balances for "{active.name}":' if active else "Group balances:"]
        for b in sorted(summary.balances, key=lambda x: -x.balance_cents):
            lines.append(
                f"  {display_by_id[b.user_id]}: {_fmt(b.balance_cents, b.currency)}"
            )

        if summary.suggested_transfers:
            heading = "simplified" if group.simplify_debts else "raw"
            lines.append(f"\nSuggested transfers ({heading}):")
            for t in summary.suggested_transfers:
                lines.append(
                    f"  {display_by_id[t.from_user_id]} → "
                    f"{display_by_id[t.to_user_id]}: {format_money(t.amount_cents, t.currency)}"
                )

        await message.reply("\n".join(lines))
        return

    # Personal view: the caller's net position, then who to settle with — the
    # suggested transfers involving the caller (honoring the group's simplify
    # toggle, so it agrees with /balance all and /settleup).
    my_balances = [b for b in summary.balances if b.user_id == caller.id]
    if not my_balances:
        await message.reply(f"You have no outstanding balances{scope_in}.")
        return

    lines = [f'Your balance in "{active.name}":' if active else "Your balance:"]
    for b in my_balances:
        direction = "you're owed" if b.balance_cents > 0 else "you owe"
        lines.append(f"  {_fmt(b.balance_cents, b.currency)} ({direction})")

    owed_to_me = [t for t in summary.suggested_transfers if t.to_user_id == caller.id]
    i_owe = [t for t in summary.suggested_transfers if t.from_user_id == caller.id]

    heading = "simplified" if group.simplify_debts else "raw"
    if owed_to_me or i_owe:
        lines.append(f"\nTo settle up ({heading}):")
    for t in owed_to_me:
        lines.append(
            f"  {display_by_id[t.from_user_id]} pays you {format_money(t.amount_cents, t.currency)}"
        )
    for t in i_owe:
        lines.append(
            f"  you pay {display_by_id[t.to_user_id]} {format_money(t.amount_cents, t.currency)}"
        )

    await message.reply("\n".join(lines))
