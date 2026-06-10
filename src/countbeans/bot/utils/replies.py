"""Reply fragments that need Telegram calls — the aiogram-free
``formatting.py`` can't host them."""

import logging

from aiogram import Bot

from countbeans.bot.utils.formatting import coverage_gap_warning

logger = logging.getLogger(__name__)


async def whole_group_coverage_warning(
    bot: Bot, chat_id: int, known_count: int
) -> str | None:
    """The coverage-gap warning when the chat holds more humans than the split
    covers, or ``None`` on full coverage / a failed count lookup (logged).

    Owns the ``getChatMemberCount`` call and the minus-the-bot adjustment; the
    *trigger* conditions (when a split counts as whole-group) stay with each
    entry path, since the one-liner and the wizard deliberately differ."""
    try:
        actual = await bot.get_chat_member_count(chat_id) - 1  # minus the bot
    except Exception:
        logger.warning(
            "could not fetch chat member count for %s", chat_id, exc_info=True
        )
        return None
    if known_count < actual:
        return coverage_gap_warning(known_count, actual - known_count)
    return None
