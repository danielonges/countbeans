"""End-to-end handler tests for /start and /join via the aiogram harness.

These drive real `Update`s through a `Dispatcher` (filters + routing run) with a
MockedBot, covering the paths the service-core tests can't: the /start admin
gate, chat-type routing, and which reply text is sent. Needs Postgres (handlers
onboard) — see conftest.py.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import GroupMember, User
from countbeans.services.repositories import UserRepository

from ._bot_harness import MockedBot, feed, make_message


async def _count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def _is_member(session: AsyncSession, telegram_user_id: int) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(GroupMember)
            .join(User, User.id == GroupMember.user_id)
            .where(User.telegram_user_id == telegram_user_id)
        )
    ).scalar_one()
    return count == 1


async def test_start_non_admin_is_refused(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=False)
    await feed(dispatcher, bot, make_message("/start", from_id=1001), session=session)

    assert "/join" in (bot.last_reply or "")
    # The gate returns before any write — nothing onboarded.
    assert await _count_users(session) == 0


async def test_start_admin_onboards_and_welcomes(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/start", from_id=2002), session=session)

    assert "countbeans" in (bot.last_reply or "").lower()
    assert await _is_member(session, 2002)  # admin is onboarded, no /join needed


async def test_start_in_private_chat_explains_group_only(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher, bot, make_message("/start", chat_type="private", chat_id=3003), session=session
    )

    assert "private" in (bot.last_reply or "").lower()
    assert await _count_users(session) == 0  # nothing tracked in private chats


async def test_join_onboards_any_member(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=False)  # no admin gate on /join
    await feed(dispatcher, bot, make_message("/join", from_id=4004), session=session)

    assert "you're in" in (bot.last_reply or "").lower()
    assert await _is_member(session, 4004)


async def test_join_claims_pending_placeholder(dispatcher, session: AsyncSession) -> None:
    # "ghost" was @mentioned in an expense before ever interacting.
    placeholder = await UserRepository(session).resolve_mention("ghost")
    assert placeholder.telegram_user_id is None

    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher, bot, make_message("/join", from_id=5005, username="ghost"), session=session
    )

    assert "linked" in (bot.last_reply or "").lower()
    assert await _is_member(session, 5005)
    assert await _count_users(session) == 1  # claimed in place, not duplicated
