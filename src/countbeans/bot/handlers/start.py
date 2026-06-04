"""Bot handler for /start.

In a group, /start is the **admin setup** command (creator/administrator only,
like /simplify and /currency): it onboards the admin who runs it — upserting them
into `users`/`group_members` and the chat into `groups` (see CLAUDE.md
"Onboarding & membership") — and posts a welcome listing the commands. A
non-admin is refused and pointed at /join, which is how everyone else opts in.
In a private chat there is nothing to track, so it just explains that the bot is
group-only.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.utils.permissions import is_admin
from countbeans.dto.commands import OnboardUserCommand
from countbeans.services.onboard import onboard_member
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

_GROUP_WELCOME = (
    "👋 Hi! I'm countbeans — I track shared expenses for this group so nobody "
    "has to do the mental math.\n"
    "\n"
    "Commands:\n"
    '• /addexpense <amount> "<desc>" [@user …] — record an expense\n'
    "• /balance [all] — your net position, or every member's with /balance all\n"
    "• /settleup @user [amount] — record a payment; omit amount to settle in full\n"
    "• /statements [all] — your transactions, or the whole group's with /statements all\n"
    "• /simplify [on|off] — view or (admins) toggle simplified settle-up suggestions\n"
    "• /currency [CODE] — view or (admins) set the group's default currency\n"
    "• /group — group info, members, and activity\n"
    "\n"
    "You're all set — I've added you to this group's ledger.\n"
    "Everyone else: run /join to be added too."
)

_NOT_ADMIN = (
    "Only group admins can run /start. "
    "To add yourself to expense tracking, use /join."
)

_PRIVATE_WELCOME = (
    "👋 I'm countbeans, a shared-expense tracker for Telegram groups.\n"
    "\n"
    "Add me to a group chat to start splitting expenses — I don't track "
    "anything in private chats."
)


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def start_group(message: Message, uow: UnitOfWork, bot: Bot) -> None:
    if message.from_user is None:
        return

    # /start is admin setup — refuse non-admins and point them at /join.
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply(_NOT_ADMIN)
        return

    # The admin who runs /start is onboarded here, so they need not also /join.
    await onboard_member(
        uow,
        OnboardUserCommand(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            group_name=getattr(message.chat, "title", None),
        ),
    )

    await message.answer(_GROUP_WELCOME)
    logger.info(
        "Onboarded admin user=%s into group=%s via /start",
        message.from_user.id,
        message.chat.id,
    )


@router.message(Command("start"))
async def start_private(message: Message) -> None:
    await message.answer(_PRIVATE_WELCOME)
