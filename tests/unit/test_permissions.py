"""Unit tests for the shared is_admin permission check.

The real check is a single getChatMember call; here a fake bot returns a member
with each status so the creator/administrator → True, everyone-else → False
mapping is pinned without Telegram.
"""
from types import SimpleNamespace

from aiogram.enums import ChatMemberStatus

from countbeans.bot.permissions import is_admin


class _FakeBot:
    def __init__(self, status: ChatMemberStatus) -> None:
        self._status = status

    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        return SimpleNamespace(status=self._status)


async def test_creator_is_admin() -> None:
    bot = _FakeBot(ChatMemberStatus.CREATOR)
    assert await is_admin(bot, 1, 2) is True  # type: ignore[arg-type]


async def test_administrator_is_admin() -> None:
    bot = _FakeBot(ChatMemberStatus.ADMINISTRATOR)
    assert await is_admin(bot, 1, 2) is True  # type: ignore[arg-type]


async def test_member_is_not_admin() -> None:
    bot = _FakeBot(ChatMemberStatus.MEMBER)
    assert await is_admin(bot, 1, 2) is False  # type: ignore[arg-type]


async def test_left_is_not_admin() -> None:
    bot = _FakeBot(ChatMemberStatus.LEFT)
    assert await is_admin(bot, 1, 2) is False  # type: ignore[arg-type]
