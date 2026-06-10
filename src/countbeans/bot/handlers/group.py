"""Bot handler for /group — group info snapshot."""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name
from countbeans.services.group_info import get_group_info
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("group"))
async def cmd_group(message: Message, uow: UnitOfWork, bot: Bot) -> None:
    if message.from_user is None:
        return

    group = (await resolve_chat_context(uow, message)).group

    try:
        chat_count = await bot.get_chat_member_count(message.chat.id)
        actual_count = chat_count - 1  # subtract the bot itself
    except Exception:
        logger.warning(
            "Could not fetch chat member count for %s",
            message.chat.id,
            exc_info=True,
        )
        actual_count = None

    info = await get_group_info(
        uow,
        group.id,
        group_name=group.group_name,
        default_currency=group.default_currency,
        simplify_debts=group.simplify_debts,
        actual_count=actual_count,
    )
    logger.debug(
        "group info: group=%s known=%s actual=%s",
        group.id,
        info.known_count,
        info.actual_count,
    )

    lines: list[str] = []

    # Header
    name = info.group_name or "This group"
    lines.append(name)
    lines.append(f"Currency: {info.default_currency}")
    simplify_label = "on" if info.simplify_debts else "off"
    lines.append(f"Debt simplification: {simplify_label}")

    # Members
    lines.append("")
    claimed = [m for m in info.members if not m.is_pending]
    pending = [m for m in info.members if m.is_pending]

    lines.append(f"Members ({info.known_count}):")
    if claimed:
        for m in claimed:
            lines.append(f"  {display_name(m.username, m.first_name)}")
    else:
        lines.append("  (none yet)")

    if pending:
        lines.append("  Pending (not yet interacted):")
        for m in pending:
            lines.append(f"    @{m.username}")

    # Coverage gap
    if info.actual_count is not None and info.known_count < info.actual_count:
        gap = info.actual_count - info.known_count
        lines.append(
            f"\n⚠️ {gap} member(s) in this chat haven't interacted with the bot yet "
            f"({info.known_count}/{info.actual_count} known). "
            "Ask them to send /join."
        )

    # Activity
    if info.activity:
        lines.append("")
        lines.append("Activity:")
        for a in sorted(info.activity, key=lambda x: x.currency):
            total = f"{a.total_cents // 100}.{a.total_cents % 100:02d}"
            lines.append(f"  {a.expense_count} expense(s) · {a.currency} {total} total")
    else:
        lines.append("")
        lines.append("No expenses recorded yet.")

    # Plain text on purpose: @usernames, group names, and first names are
    # free text that would mangle (or 400) under Markdown parsing — same reason
    # /statements stays plain.
    await message.reply("\n".join(lines))
