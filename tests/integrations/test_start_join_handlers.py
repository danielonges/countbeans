"""End-to-end handler tests for /start and /join via the aiogram harness.

These drive real `Update`s through a `Dispatcher` (filters + routing run) with a
MockedBot, covering the paths the service-core tests can't: the /start admin
gate, chat-type routing, and which reply text is sent. Needs Postgres (handlers
onboard) — see conftest.py.
"""

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import GroupMember, User

from ._bot_harness import (
    MockedBot,
    feed,
    feed_callback,
    make_callback,
    make_message,
)
from ._seed import seed_group, seed_placeholder


def _welcome_has_join_button(bot: MockedBot) -> bool:
    markup = bot.sent[-1].reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    return any(
        b.callback_data == "join:me" for row in markup.inline_keyboard for b in row
    )


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


async def test_start_admin_onboards_and_welcomes(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/start", from_id=2002), session=session)

    assert "countbeans" in (bot.last_reply or "").lower()
    assert await _is_member(session, 2002)  # admin is onboarded, no /join needed
    assert _welcome_has_join_button(bot)  # ✋ Count me in for the rest of the group


async def test_join_button_onboards_tapper_with_social_proof(
    dispatcher, session: AsyncSession
) -> None:
    """Tapping ✋ Count me in onboards the tapper and posts a public 'joined'
    line; a second tap just toasts (no duplicate public line)."""
    await seed_group(session)
    bot = MockedBot()
    await feed_callback(
        dispatcher, bot, make_callback("join:me", from_id=7007), session=session
    )

    assert await _is_member(session, 7007)
    assert "joined the ledger" in (bot.sent[-1].text or "")  # public social proof
    assert "in" in ((bot.last_answer.text or "").lower() if bot.last_answer else "")

    # A second tap is idempotent — a private toast, no second public message.
    public_before = sum("joined the ledger" in (m.text or "") for m in bot.sent)
    await feed_callback(
        dispatcher, bot, make_callback("join:me", from_id=7007), session=session
    )
    answer = bot.last_answer
    assert answer is not None and "already in" in (answer.text or "").lower()
    public_after = sum("joined the ledger" in (m.text or "") for m in bot.sent)
    assert public_after == public_before  # no extra public line


async def test_join_button_claims_placeholder(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_placeholder(session, group, username="ghost")
    bot = MockedBot()
    await feed_callback(
        dispatcher,
        bot,
        make_callback("join:me", from_id=8008, username="ghost"),
        session=session,
    )

    assert await _is_member(session, 8008)
    assert "linked" in (bot.sent[-1].text or "").lower()
    assert await _count_users(session) == 1  # claimed in place, not duplicated


async def test_start_in_private_chat_explains_group_only(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/start", chat_type="private", chat_id=3003),
        session=session,
    )

    assert "private" in (bot.last_reply or "").lower()
    assert await _count_users(session) == 0  # nothing tracked in private chats


async def test_join_onboards_any_member(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=False)  # no admin gate on /join
    await feed(dispatcher, bot, make_message("/join", from_id=4004), session=session)

    assert "you're in" in (bot.last_reply or "").lower()
    assert await _is_member(session, 4004)


async def test_join_again_acknowledges_auto_add(
    dispatcher, session: AsyncSession
) -> None:
    """A second /join (or one from an auto-onboarded member) explains why they're
    already in, rather than a bare 'nothing to do'."""
    bot = MockedBot(caller_is_admin=False)
    await feed(dispatcher, bot, make_message("/join", from_id=4004), session=session)
    await feed(dispatcher, bot, make_message("/join", from_id=4004), session=session)

    reply = (bot.last_reply or "").lower()
    assert "already in this group's ledger" in reply
    assert "automatically" in reply


async def test_join_claims_pending_placeholder(
    dispatcher, session: AsyncSession
) -> None:
    # "ghost" was @mentioned before ever interacting — the placeholder lives in
    # this group (a mention ensures membership), so the group-scoped claim gate
    # (security review #1) lets /join claim it.
    group = await seed_group(session)
    placeholder = await seed_placeholder(session, group, username="ghost")
    assert placeholder.telegram_user_id is None

    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/join", from_id=5005, username="ghost"),
        session=session,
    )

    assert "linked" in (bot.last_reply or "").lower()
    assert await _is_member(session, 5005)
    assert await _count_users(session) == 1  # claimed in place, not duplicated
