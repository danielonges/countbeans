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

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import parse_view_selector
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


def _transfer_heading(simplify_debts: bool) -> str:
    """Plain words for the suggested-transfer set, instead of the system
    vocabulary "simplified"/"raw" — "raw" means nothing to a non-technical user.
    The distinction that matters to them is fewest payments vs. every debt."""
    return "fewest payments" if simplify_debts else "exact pairwise debts"


def _pivot_row(to: str) -> list[InlineKeyboardButton]:
    """The single button that flips between the personal and group views in
    place — the most common follow-up to one view is the other. Not owner-bound:
    `bal:all` shows public data, and `bal:me` shows the *tapper's* own balance
    (a subset of what `/balance all` already reveals), so anyone may flip it."""
    if to == "all":
        return [
            InlineKeyboardButton(text="👥 Everyone's balances", callback_data="bal:all")
        ]
    return [InlineKeyboardButton(text="🙋 Just mine", callback_data="bal:me")]


def _keyboard(
    pay_rows: list[list[InlineKeyboardButton]], pivot_to: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*pay_rows, _pivot_row(pivot_to)])


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
    # The group view's pivot flips to the tapper's own balance.
    if not summary.balances:
        return f"No outstanding balances{scope_in}.", _keyboard([], "me")

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

    pay_rows: list[list[InlineKeyboardButton]] = []
    if summary.suggested_transfers:
        lines.append(
            f"\nSuggested transfers ({_transfer_heading(group.simplify_debts)}):"
        )
        for t in summary.suggested_transfers:
            lines.append(
                f"  {display_by_id[t.from_user_id]} → "
                f"{display_by_id[t.to_user_id]}: {format_money(t.amount_cents, t.currency)}"
            )
        lines.append(
            "\nTap a transfer to record it as paid in full — only its payer can."
        )
        names = {b.user_id: (b.username, b.first_name) for b in summary.balances}
        pay_rows = payment_buttons(summary.suggested_transfers, names, origin="a")
    return "\n".join(lines), _keyboard(pay_rows, "me")


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
    # The personal view's pivot flips to everyone's balances.
    if not my_balances:
        return f"You have no outstanding balances{scope_in}.", _keyboard([], "all")

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

    if owed_to_me or i_owe:
        lines.append(f"\nTo settle up ({_transfer_heading(group.simplify_debts)}):")
    for t in owed_to_me:
        lines.append(
            f"  {display_by_id[t.from_user_id]} pays you {format_money(t.amount_cents, t.currency)}"
        )
    for t in i_owe:
        lines.append(
            f"  you pay {display_by_id[t.to_user_id]} {format_money(t.amount_cents, t.currency)}"
        )

    pay_rows: list[list[InlineKeyboardButton]] = []
    if i_owe:
        lines.append("\nTap to record a payment as paid in full.")
        names = {b.user_id: (b.username, b.first_name) for b in summary.balances}
        pay_rows = payment_buttons(i_owe, names, origin="m", viewer_is_payer=True)
    return "\n".join(lines), _keyboard(pay_rows, "all")


@router.message(Command("balance"))
async def cmd_balance(
    message: Message, command: CommandObject, uow: UnitOfWork
) -> None:
    if message.from_user is None:
        return

    ctx = await resolve_chat_context(uow, message)
    group, caller = ctx.group, ctx.caller

    args = (command.args or "").split()
    show_all, unrecognized = parse_view_selector(args)

    # /balance defaults to the active event's scope (general when none is active).
    # Named cross-scope reads (/balance general, /balance "<event>") are deferred.
    if show_all:
        text, keyboard = await render_group_balances(uow, group, ctx.active_event)
    else:
        text, keyboard = await render_personal_balance(
            uow, group, caller.id, ctx.active_event
        )
    # Forgiving but not silent: an unrecognized arg still shows the personal view,
    # with a note so the caller doesn't mistake it for the group view (theme T4).
    if unrecognized is not None:
        text = (
            f'ℹ️ I didn\'t recognize "{unrecognized}" — showing your own balance. '
            "Use /balance all for everyone's.\n\n" + text
        )
    await message.reply(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("bal:"))
async def on_balance_pivot(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """The me⇄all pivot: `bal:all` repaints as the group view, `bal:me` as the
    *tapper's* own. Edits in place (like /statements paging). Not owner-bound —
    `bal:me` reveals only what `/balance all` already does, so any member may
    flip the message they're looking at."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    view = (callback.data or "").split(":")[1] if ":" in (callback.data or "") else ""
    chat = callback.message.chat
    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )
    active_event = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )

    if view == "all":
        text, keyboard = await render_group_balances(uow, group, active_event)
    elif view == "me":
        viewer = await uow.users.upsert(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            claim_in_group=group.id,
        )
        text, keyboard = await render_personal_balance(
            uow, group, viewer.id, active_event
        )
    else:
        await callback.answer()
        return

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        # "message is not modified" — e.g. tapping "Just mine" on your own view.
        logger.debug("balance pivot edit skipped (not modified)")
    await callback.answer()
