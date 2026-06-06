"""Telegram permission checks shared across handlers.

Admin gating (creator/administrator) backs the group-wide settings commands
(`/simplify on|off`, `/currency <CODE>`) and the admin-only `/start` setup. The
check is a single getChatMember call — available to any bot — so it lives here
rather than being copy-pasted per handler.
"""

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus

logger = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True if the user is the group's creator or an administrator."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        # Every admin-gated command (/simplify, /currency, /event, /start) hinges
        # on this call — surface the failure with context, then fail closed:
        # treat an API error as "not admin" so a transient error denies the
        # privileged action rather than risking it proceeding ungated.
        logger.warning(
            "getChatMember failed for admin check (chat=%s user=%s) — denying",
            chat_id,
            user_id,
            exc_info=True,
        )
        return False
    result = member.status in ADMIN_STATUSES
    logger.debug(
        "admin check: chat=%s user=%s status=%s is_admin=%s",
        chat_id,
        user_id,
        member.status,
        result,
    )
    return result
