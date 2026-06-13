"""Handlers for Telegram's membership event stream.

Two update types feed this router (both require the allowed-updates opt-in wired
in `bot/server.py`):

  * **my_chat_member** — the *bot's own* status changing (added / removed /
    promoted / demoted). The bot records whether it is an administrator
    (`groups.bot_is_admin`, consumed by the admin gate), creates the `groups` row
    on being added, and posts a welcome on promotion or a promote-me nudge when
    it lacks admin rights.
  * **chat_member** — *other members* joining or leaving. Only delivered while the
    bot is an admin (Telegram restriction). Joins onboard the member (claiming a
    placeholder if one waits); leaves set `left_at`, keeping `group_members`
    accurate from the event stream rather than drifting.

See CLAUDE.md "Onboarding & membership". All SQL lives in the service core
(`services/membership.py`, `services/onboard.py`); these handlers only translate
aiogram updates into service calls and send replies.
"""

import logging

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import (
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
    ChatMemberUpdatedFilter,
)
from aiogram.types import ChatMemberUpdated

from countbeans.bot.handlers._welcome import (
    GROUP_WELCOME,
    PROMOTE_REQUEST,
    WELCOME_KEYBOARD,
)
from countbeans.bot.utils.permissions import ADMIN_STATUSES, GROUP_TYPES
from countbeans.dto.commands import OnboardUserCommand
from countbeans.services.membership import record_member_leave, set_bot_admin
from countbeans.services.onboard import onboard_member
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# Statuses where the bot is still in the chat (vs. left/kicked). RESTRICTED is a
# present-but-limited member; treat it as a non-admin member.
_PRESENT_STATUSES = ADMIN_STATUSES | {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


@router.my_chat_member()
async def on_my_chat_member(
    event: ChatMemberUpdated, uow: UnitOfWork, bot: Bot
) -> None:
    """The bot's own membership changed. Maintain `groups.bot_is_admin` and guide
    the operator toward promoting the bot."""
    chat = event.chat
    if chat.type not in GROUP_TYPES:
        return

    is_admin_now = event.new_chat_member.status in ADMIN_STATUSES
    was_admin = event.old_chat_member.status in ADMIN_STATUSES
    present_now = event.new_chat_member.status in _PRESENT_STATUSES
    was_present = event.old_chat_member.status in _PRESENT_STATUSES

    if not present_now:
        # Removed (left/kicked). Best-effort flag clear; the row may not exist.
        group = await uow.groups.get_by_telegram_chat_id(chat.id)
        if group is not None:
            await uow.groups.set_bot_admin(group.id, False)
        logger.info("bot removed from chat=%s", chat.id)
        return

    await set_bot_admin(uow, chat.id, chat.title, is_admin_now)

    if is_admin_now and not was_admin:
        # Freshly added as admin, or just promoted — the moment to introduce itself.
        await bot.send_message(chat.id, GROUP_WELCOME, reply_markup=WELCOME_KEYBOARD)
        logger.info("bot is now admin in chat=%s — sent welcome", chat.id)
    elif not is_admin_now and (was_admin or not was_present):
        # The bot lacks admin and needs it — nudge for promotion. This fires both
        # on a demotion (`was_admin`: it just lost admin and can no longer work)
        # and on a fresh non-admin add (`not was_present`). The `was_present`
        # guard still suppresses no-op transitions (e.g. a group→supergroup
        # upgrade, old=member→new=member) so they don't spam the chat.
        await bot.send_message(chat.id, PROMOTE_REQUEST)
        logger.info("bot lacks admin in chat=%s — requested promotion", chat.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(event: ChatMemberUpdated, uow: UnitOfWork) -> None:
    """A member joined — onboard them (claiming a placeholder if one waits)."""
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await onboard_member(
        uow,
        OnboardUserCommand(
            telegram_user_id=user.id,
            telegram_chat_id=event.chat.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            group_name=event.chat.title,
        ),
    )
    logger.info("member joined chat=%s user=%s", event.chat.id, user.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_member_leave(event: ChatMemberUpdated, uow: UnitOfWork) -> None:
    """A member left or was removed — close out their active membership."""
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await record_member_leave(uow, event.chat.id, user.id)
    logger.info("member left chat=%s user=%s", event.chat.id, user.id)
