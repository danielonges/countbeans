"""Handler tests for /addexpense — parsing, recording, and the @all coverage warning."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense
from countbeans.services.repositories import UserRepository

from ._bot_harness import MockedBot, feed, make_message
from ._seed import seed_group


async def _expense_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Expense))).scalar_one()


async def test_addexpense_usage_on_no_args(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/addexpense"), session=session)
    assert "Usage" in (bot.last_reply or "")


async def test_addexpense_invalid_amount(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message('/addexpense notmoney "Lunch"'), session=session)
    assert "Invalid amount" in (bot.last_reply or "")
    assert await _expense_count(session) == 0


async def test_addexpense_records_named_split(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message('/addexpense 10 "Lunch" @bob', from_id=1001, username="caller"),
        session=session,
    )

    assert "Added expense" in (bot.last_reply or "")
    assert await _expense_count(session) == 1
    # The named-but-unseen @bob becomes a pending placeholder.
    bob = await UserRepository(session).find_by_mention("bob")
    assert bob is not None and bob.telegram_user_id is None


async def test_addexpense_whole_group_warns_about_unseen_members(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    # member_count=3 → actual 2, but the bot only knows the payer → gap of 1.
    bot = MockedBot(member_count=3)
    await feed(
        dispatcher,
        bot,
        make_message('/addexpense 10 "Snacks"', from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Added expense" in reply
    assert "haven't interacted" in reply
    assert "/join" in reply
