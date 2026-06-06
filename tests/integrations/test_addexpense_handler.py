"""Handler tests for /addexpense — parsing, recording, and the @all coverage warning."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, User
from countbeans.services.repositories import UserRepository

from ._bot_harness import MockedBot, feed, make_message, text_mention_entity
from ._seed import seed_group, seed_member


async def _expense_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(Expense))
    ).scalar_one()


async def _share_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(ExpenseShare))
    ).scalar_one()


async def _shares_by_username(session: AsyncSession) -> dict[str | None, int]:
    """Map each recorded share to its participant's @username — for asserting how
    an uneven split apportioned the amount."""
    rows = (
        await session.execute(
            select(User.username, ExpenseShare.share_cents).join(
                ExpenseShare, ExpenseShare.user_id == User.id
            )
        )
    ).all()
    return {username: cents for username, cents in rows}


async def test_addexpense_usage_on_no_args(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/addexpense"), session=session)
    assert "Usage" in (bot.last_reply or "")


async def test_addexpense_invalid_amount(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(
        dispatcher, bot, make_message('/addexpense notmoney "Lunch"'), session=session
    )
    assert "Invalid amount" in (bot.last_reply or "")
    assert await _expense_count(session) == 0


async def test_addexpense_records_named_split(
    dispatcher, session: AsyncSession
) -> None:
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


async def test_addexpense_text_mention_resolves_to_claimed_user(
    dispatcher, session: AsyncSession
) -> None:
    """A text_mention carries a real telegram id → the participant is a CLAIMED
    user, never a username placeholder (security review #1). A text_mention with no
    typed @handle splits among the mention, not the whole group."""
    group = await seed_group(session)
    # Another known member, so a "split everyone" would include >1 person.
    await seed_member(session, group, telegram_user_id=3003, username="carol")
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            "/addexpense 30 lunch",
            from_id=1001,
            username="payer",
            entities=[text_mention_entity(2002, first_name="Bob")],
        ),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Added expense" in reply
    assert "Bob" in reply  # the mention is in the split
    assert "carol" not in reply  # NOT split-everyone — only the named mention
    # Bob is a claimed user (real telegram id), not a pending placeholder.
    bob = await UserRepository(session).get_by_telegram_id(2002)
    assert bob is not None and bob.telegram_user_id == 2002
    assert await _share_count(session) == 1  # payer paid; only Bob owes a share


async def test_addexpense_text_mention_plus_typed_handle(
    dispatcher, session: AsyncSession
) -> None:
    """A text_mention (claimed) and a typed @handle (placeholder) both join the split."""
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            "/addexpense 30 @alice",
            from_id=1001,
            username="payer",
            entities=[text_mention_entity(2002, first_name="Bob")],
        ),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Bob" in reply and "@alice" in reply
    bob = await UserRepository(session).get_by_telegram_id(2002)
    assert bob is not None  # claimed via the real id
    alice = await UserRepository(session).find_by_mention("alice")
    assert alice is not None and alice.telegram_user_id is None  # still a placeholder
    assert await _share_count(session) == 2


async def test_addexpense_whole_group_warns_about_unseen_members(
    dispatcher, session: AsyncSession
) -> None:
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


async def test_addexpense_exact_split(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 50 "Dinner" @alice:30 @bob:20',
            from_id=1001,
            username="caller",
        ),
        session=session,
    )

    assert "by exact amount" in (bot.last_reply or "")
    assert await _expense_count(session) == 1
    assert await _shares_by_username(session) == {"alice": 3000, "bob": 2000}


async def test_addexpense_percent_split(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 100 "Trip" @alice:60% @bob:40%',
            from_id=1001,
            username="caller",
        ),
        session=session,
    )

    assert "by percentage" in (bot.last_reply or "")
    assert await _shares_by_username(session) == {"alice": 6000, "bob": 4000}


async def test_addexpense_weighted_split(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 90 "Cab" @alice:2x @bob:1x', from_id=1001, username="caller"
        ),
        session=session,
    )

    assert "by weight" in (bot.last_reply or "")
    # apportion 9000 by {alice:2, bob:1} → 6000 / 3000, summing to the amount.
    shares = await _shares_by_username(session)
    assert shares == {"alice": 6000, "bob": 3000}
    assert sum(shares.values()) == 9000


async def test_addexpense_mixed_families_rejected_no_side_effects(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 50 "x" @alice:30 @bob:40%', from_id=1001, username="caller"
        ),
        session=session,
    )

    assert "mix" in (bot.last_reply or "")
    # Rejected *before* resolve_participants: no expense and no placeholder users.
    assert await _expense_count(session) == 0
    users = UserRepository(session)
    assert await users.find_by_mention("alice") is None
    assert await users.find_by_mention("bob") is None


async def test_addexpense_exact_wrong_sum_rejected_no_side_effects(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 50 "x" @alice:30 @bob:30', from_id=1001, username="caller"
        ),
        session=session,
    )

    assert "add up to" in (bot.last_reply or "")
    assert await _expense_count(session) == 0
    users = UserRepository(session)
    assert await users.find_by_mention("alice") is None
    assert await users.find_by_mention("bob") is None
