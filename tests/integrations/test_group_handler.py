"""Handler tests for /group — the info snapshot and its coverage-gap warning."""

from sqlalchemy.ext.asyncio import AsyncSession

from ._bot_harness import MockedBot, feed, make_message
from ._seed import seed_event, seed_group, seed_member


async def test_group_snapshot_basics(dispatcher, session: AsyncSession) -> None:
    # member_count=2 → actual (minus bot) = 1 == known 1, so no coverage warning.
    bot = MockedBot(member_count=2)
    await feed(
        dispatcher,
        bot,
        make_message("/group", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Test Group" in reply
    assert "Currency: SGD" in reply
    assert "Debt simplification: on" in reply
    assert "@caller" in reply  # the caller is onboarded and listed
    # With no event open, /group says where new expenses land.
    assert "Active event: none" in reply


async def test_group_shows_active_event(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="caller"
    )
    await seed_event(session, group, creator=creator, name="Bali")
    bot = MockedBot(member_count=2)
    await feed(
        dispatcher,
        bot,
        make_message("/group", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert 'Active event: "Bali"' in reply
    assert "new expenses tag to it" in reply


async def test_group_flags_coverage_gap_pointing_to_join(
    dispatcher, session: AsyncSession
) -> None:
    # member_count=3 → actual 2 > known 1: the bot can't see everyone yet.
    bot = MockedBot(member_count=3)
    await feed(dispatcher, bot, make_message("/group"), session=session)

    reply = bot.last_reply or ""
    assert "haven't interacted" in reply
    assert "/join" in reply  # not /start — that's admin-only now
