"""Bot handler for /join — the explicit, anyone-can-run opt-in to expense tracking.

Where /start is admin-only setup, /join is how an ordinary member adds
themselves: it onboards the caller (upserting them into `users`/`group_members`
and the chat into `groups`) and, crucially, **claims a pending placeholder** if
they were @mentioned in an expense before they ever interacted — see CLAUDE.md
"Onboarding & membership". The reply is status-aware so the caller knows whether
they were newly added, already in, or just linked to earlier mentions. In a
private chat there is nothing to join.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from countbeans.bot.utils.formatting import display_name
from countbeans.dto.commands import OnboardUserCommand
from countbeans.dto.results import OnboardResult
from countbeans.services.onboard import onboard_member
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

_PRIVATE_JOIN = (
    "There's nothing to join in a private chat — add me to a group and run "
    "/join there to start tracking shared expenses."
)


def _compose_join_reply(result: OnboardResult) -> str:
    """Status-aware confirmation. Claiming takes precedence: it's the surprising,
    most-informative outcome (their earlier mentions now count toward them)."""
    if result.claimed_placeholder:
        return (
            "🔗 Welcome! You'd already been mentioned in expenses here — "
            "I've linked those to you. You're now in the ledger."
        )
    if result.newly_added:
        return "✅ You're in! I'll track shared expenses for you in this group."
    return (
        "👍 You're already in this group's ledger (I add everyone automatically "
        "when they join the chat) — nothing to do."
    )


@router.message(Command("join"), F.chat.type.in_({"group", "supergroup"}))
async def join_group(message: Message, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    result = await onboard_member(
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

    await message.reply(_compose_join_reply(result))
    logger.info(
        "Onboarded user=%s into group=%s via /join (claimed=%s, new=%s)",
        message.from_user.id,
        message.chat.id,
        result.claimed_placeholder,
        result.newly_added,
    )


@router.message(Command("join"))
async def join_private(message: Message) -> None:
    await message.answer(_PRIVATE_JOIN)


@router.callback_query(F.data == "join:me")
async def join_button(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """The welcome's ✋ Count me in button — onboards whoever taps it, so the
    highest-volume onboarding action is one tap, not a typed /join. A real join
    posts a short public line (social proof in the chat); an already-member tap
    just gets a private toast, so repeat taps don't spam."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    chat = callback.message.chat
    result = await onboard_member(
        uow,
        OnboardUserCommand(
            telegram_user_id=callback.from_user.id,
            telegram_chat_id=chat.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            group_name=getattr(chat, "title", None),
        ),
    )
    label = display_name(result.username, result.first_name)
    if result.claimed_placeholder:
        await callback.answer("🔗 Linked to your earlier mentions — you're in!")
        await callback.message.answer(
            f"🔗 {label} joined — linked to earlier mentions."
        )
    elif result.newly_added:
        await callback.answer("✅ You're in!")
        await callback.message.answer(f"✅ {label} joined the ledger.")
    else:
        await callback.answer("👍 You're already in — nothing to do.")
    logger.info(
        "Onboarded user=%s into group=%s via ✋ button (claimed=%s, new=%s)",
        callback.from_user.id,
        chat.id,
        result.claimed_placeholder,
        result.newly_added,
    )
