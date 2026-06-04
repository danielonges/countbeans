"""Telegram permission checks shared across handlers.

Admin gating (creator/administrator) backs the group-wide settings commands
(`/simplify on|off`, `/currency <CODE>`) and the admin-only `/start` setup. The
check is a single getChatMember call — available to any bot — so it lives here
rather than being copy-pasted per handler.
"""

from aiogram import Bot
from aiogram.enums import ChatMemberStatus

_ADMIN_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True if the user is the group's creator or an administrator."""
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in _ADMIN_STATUSES
