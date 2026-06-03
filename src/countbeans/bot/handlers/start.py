"""Bot handler for /start.

In a group, /start is a normal command and therefore an onboarding point: it
upserts the caller into `users`/`group_members` and the chat into `groups` (see
CLAUDE.md "Onboarding & membership"), then posts a welcome listing the commands.
In a private chat there is nothing to track, so it just explains that the bot is
group-only.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

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
    "• /settleup @user <amount> — record a payment (full or partial)\n"
    "• /simplify [on|off] — view or (admins) toggle simplified settle-up suggestions\n"
    "• /currency [CODE] — view or (admins) set the group's default currency\n"
    "• /group — group info, members, and activity\n"
    "\n"
    "You're all set — I've added you to this group's ledger."
)

_PRIVATE_WELCOME = (
    "👋 I'm countbeans, a shared-expense tracker for Telegram groups.\n"
    "\n"
    "Add me to a group chat to start splitting expenses — I don't track "
    "anything in private chats."
)


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def start_group(message: Message, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    user = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )
    await uow.group_members.ensure_member(group.id, user.id)

    await message.answer(_GROUP_WELCOME)
    logger.info(
        "Onboarded user=%s into group=%s via /start",
        message.from_user.id,
        message.chat.id,
    )


@router.message(Command("start"))
async def start_private(message: Message) -> None:
    await message.answer(_PRIVATE_WELCOME)
