"""Bot handler for /balance and /balance all — with tap-to-settle buttons.

The two views are module-level render functions (not inlined in the command)
because the tap-to-settle callback in handlers/settleup.py repaints these same
views after recording a payment — the view must come from one place or the
repaint would drift from the command's reply. Each suggested transfer involving
money the *viewer* could move is also a button, tappable only by its debtor
(handled in settleup.py's `st:` callback), so settling never means re-typing
what this reply just said.
"""

import logging
import uuid

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, Message

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import is_all_selector
from countbeans.bot.utils.settle_buttons import payment_buttons
from countbeans.db.models import Event, Group
from countbeans.services.balance import get_group_summary
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _fmt(cents: int, currency: str) -> str:
    sign = "+" if cents >= 0 else "-"
    abs_cents = abs(cents)
    return f"{sign}{currency} {abs_cents // 100}.{abs_cents % 100:02d}"


async def render_group_balances(
    uow: UnitOfWork, group: Group, active_event: Event | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The /balance all view: every member's net position, the suggested
    transfers, and a tap-to-settle button per transfer (origin 'a')."""
    scope_in = f' in "{active_event.name}"' if active_event else ""
    summary = await get_group_summary(
        uow,
        group.id,
        group.simplify_debts,
        event_id=active_event.id if active_event else None,
    )
    logger.debug(
        "balance view: scope=all group=%s balances=%d transfers=%d",
        group.id,
        len(summary.balances),
        len(summary.suggested_transfers),
    )
    if not summary.balances:
        return f"No outstanding balances{scope_in}.", None

    display_by_id: dict[uuid.UUID, str] = {
        b.user_id: display_name(b.username, b.first_name) for b in summary.balances
    }
    lines = [
        f'Balances for "{active_event.name}":' if active_event else "Group balances:"
    ]
    for b in sorted(summary.balances, key=lambda x: -x.balance_cents):
        lines.append(
            f"  {display_by_id[b.user_id]}: {_fmt(b.balance_cents, b.currency)}"
        )

    keyboard = None
    if summary.suggested_transfers:
        heading = "simplified" if group.simplify_debts else "raw"
        lines.append(f"\nSuggested transfers ({heading}):")
        for t in summary.suggested_transfers:
            lines.append(
                f"  {display_by_id[t.from_user_id]} → "
                f"{display_by_id[t.to_user_id]}: {format_money(t.amount_cents, t.currency)}"
            )
        lines.append(
            "\nTap a transfer to record it as paid in full — only its payer can."
        )
        names = {b.user_id: (b.username, b.first_name) for b in summary.balances}
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=payment_buttons(
                summary.suggested_transfers, names, origin="a"
            )
        )
    return "\n".join(lines), keyboard


async def render_personal_balance(
    uow: UnitOfWork, group: Group, viewer_id: uuid.UUID, active_event: Event | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The personal /balance view: the viewer's net position, then who to settle
    with — their own payments as tap-to-settle buttons (origin 'm')."""
    scope_in = f' in "{active_event.name}"' if active_event else ""
    summary = await get_group_summary(
        uow,
        group.id,
        group.simplify_debts,
        event_id=active_event.id if active_event else None,
    )
    my_balances = [b for b in summary.balances if b.user_id == viewer_id]
    logger.debug(
        "balance view: scope=me group=%s my_balances=%d",
        group.id,
        len(my_balances),
    )
    if not my_balances:
        return f"You have no outstanding balances{scope_in}.", None

    display_by_id: dict[uuid.UUID, str] = {
        b.user_id: display_name(b.username, b.first_name) for b in summary.balances
    }
    lines = [
        f'Your balance in "{active_event.name}":' if active_event else "Your balance:"
    ]
    for b in my_balances:
        direction = "you're owed" if b.balance_cents > 0 else "you owe"
        lines.append(f"  {_fmt(b.balance_cents, b.currency)} ({direction})")

    owed_to_me = [t for t in summary.suggested_transfers if t.to_user_id == viewer_id]
    i_owe = [t for t in summary.suggested_transfers if t.from_user_id == viewer_id]

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

    keyboard = None
    if i_owe:
        lines.append("\nTap to record a payment as paid in full.")
        names = {b.user_id: (b.username, b.first_name) for b in summary.balances}
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=payment_buttons(
                i_owe, names, origin="m", viewer_is_payer=True
            )
        )
    return "\n".join(lines), keyboard


@router.message(Command("balance"))
async def cmd_balance(
    message: Message, command: CommandObject, uow: UnitOfWork
) -> None:
    if message.from_user is None:
        return

    ctx = await resolve_chat_context(uow, message)
    group, caller = ctx.group, ctx.caller

    args = (command.args or "").split()
    show_all = is_all_selector(args)

    # /balance defaults to the active event's scope (general when none is active).
    # Named cross-scope reads (/balance general, /balance "<event>") are deferred.
    if show_all:
        text, keyboard = await render_group_balances(uow, group, ctx.active_event)
    else:
        text, keyboard = await render_personal_balance(
            uow, group, caller.id, ctx.active_event
        )
    await message.reply(text, reply_markup=keyboard)
