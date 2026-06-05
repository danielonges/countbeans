"""Service for the Telegram membership event stream (my_chat_member /
chat_member).

These keep `group_members` accurate from Telegram's event stream — a member
joining/leaving — and track whether the bot itself is an administrator, rather
than relying solely on command-time self-onboarding. All SQL stays here in the
core; the bot handlers only translate aiogram updates into these calls. Joins
reuse `onboard_member` (it already upserts user + group + membership and claims
placeholders), so this module only adds the leave and bot-admin paths.
"""

import logging

from countbeans.db.models import Group

from .uow import UnitOfWork

logger = logging.getLogger(__name__)


async def record_member_leave(
    uow: UnitOfWork, telegram_chat_id: int, telegram_user_id: int
) -> bool:
    """Mark a departed member's membership as left. No-op (returns False) if the
    bot never knew the group or the user — there is nothing to close out."""
    group = await uow.groups.get_by_telegram_chat_id(telegram_chat_id)
    if group is None:
        return False
    user = await uow.users.get_by_telegram_id(telegram_user_id)
    if user is None:
        return False
    left = await uow.group_members.mark_left(group.id, user.id)
    logger.debug(
        "record_member_leave: chat=%s user=%s left=%s",
        telegram_chat_id,
        telegram_user_id,
        left,
    )
    return left


async def set_bot_admin(
    uow: UnitOfWork,
    telegram_chat_id: int,
    group_name: str | None,
    bot_is_admin: bool,
) -> Group:
    """Upsert the group and record whether the bot is an administrator of it.

    Driven by the my_chat_member stream (the bot being added/promoted/demoted)
    and by the admin gate's self-heal. Upserting here means the `groups` row
    exists the moment the bot is added, before anyone runs a command."""
    group = await uow.groups.upsert(
        telegram_chat_id=telegram_chat_id, group_name=group_name
    )
    await uow.groups.set_bot_admin(group.id, bot_is_admin)
    logger.debug(
        "set_bot_admin: chat=%s bot_is_admin=%s", telegram_chat_id, bot_is_admin
    )
    return group
