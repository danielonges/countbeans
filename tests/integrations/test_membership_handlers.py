"""Handler tests for the membership event stream (my_chat_member / chat_member)
and the AdminGateMiddleware refuse-until-admin gate.

These exercise the real routers/middleware through the harness dispatcher, so
filters, routing, and the service core all run against the real schema (rolled
back per test). See CLAUDE.md "Onboarding & membership".
"""

from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, Update
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.bot.middleware import AdminGateMiddleware
from countbeans.services.repositories import (
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)

from ._bot_harness import (
    DEFAULT_CHAT_ID,
    HarnessUoW,
    MockedBot,
    feed_chat_member,
    feed_my_chat_member,
    make_chat_member_updated,
    make_message,
)
from ._seed import read_group, seed_group, seed_member, seed_placeholder

# --- my_chat_member: the bot's own status -----------------------------------


async def test_promote_creates_group_and_welcomes(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot()
    cmu = make_chat_member_updated(
        old="left", new="administrator"
    )  # bot added as admin
    await feed_my_chat_member(dispatcher, bot, cmu, session=session)

    group = await read_group(session)
    assert group.bot_is_admin is True
    assert "countbeans" in (bot.last_reply or "").lower()


async def test_added_as_member_requests_promotion(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot()
    cmu = make_chat_member_updated(old="left", new="member")  # added, not admin
    await feed_my_chat_member(dispatcher, bot, cmu, session=session)

    group = await read_group(session)
    assert group.bot_is_admin is False
    assert "administrator" in (bot.last_reply or "").lower()


async def test_demotion_requests_promotion(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await GroupRepository(session).set_bot_admin(group.id, True)
    bot = MockedBot()
    cmu = make_chat_member_updated(old="administrator", new="member")
    await feed_my_chat_member(dispatcher, bot, cmu, session=session)

    assert (await read_group(session)).bot_is_admin is False
    assert "administrator" in (bot.last_reply or "").lower()


async def test_removal_clears_flag_without_message(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await GroupRepository(session).set_bot_admin(group.id, True)
    bot = MockedBot()
    cmu = make_chat_member_updated(old="administrator", new="left")
    await feed_my_chat_member(dispatcher, bot, cmu, session=session)

    assert (await read_group(session)).bot_is_admin is False
    assert bot.last_reply is None  # nothing to say when removed


async def test_noop_transition_sends_no_message(
    dispatcher, session: AsyncSession
) -> None:
    """group→supergroup migration fires my_chat_member with old=member, new=member.
    The bot was already present as a non-admin; no PROMOTE_REQUEST should be sent."""
    await seed_group(session)
    bot = MockedBot()
    cmu = make_chat_member_updated(old="member", new="member")
    await feed_my_chat_member(dispatcher, bot, cmu, session=session)

    assert bot.last_reply is None


# --- chat_member: other members join / leave --------------------------------


async def test_join_onboards_member(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    bot = MockedBot()
    cmu = make_chat_member_updated(
        old="left", new="member", target_id=2002, username="newbie"
    )
    await feed_chat_member(dispatcher, bot, cmu, session=session)

    members = await GroupMemberRepository(session).list_members(group.id)
    assert any(m.username == "newbie" and not m.is_pending for m in members)


async def test_join_claims_pending_placeholder(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    # The placeholder lives in this group (a mention ensures membership), so the
    # group-scoped claim gate (security review #1) lets the join claim it.
    placeholder = await seed_placeholder(session, group, username="newbie")
    assert placeholder.telegram_user_id is None

    bot = MockedBot()
    cmu = make_chat_member_updated(
        old="left", new="member", target_id=2002, username="newbie"
    )
    await feed_chat_member(dispatcher, bot, cmu, session=session)

    claimed = await UserRepository(session).get_by_telegram_id(2002)
    assert claimed is not None and claimed.id == placeholder.id  # same row, now claimed


async def test_join_ignores_bots(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    bot = MockedBot()
    cmu = make_chat_member_updated(
        old="left", new="member", target_id=777, username="another_bot", is_bot=True
    )
    await feed_chat_member(dispatcher, bot, cmu, session=session)

    members = await GroupMemberRepository(session).list_members(group.id)
    assert all(m.username != "another_bot" for m in members)


async def test_leave_marks_left(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="leaver")
    bot = MockedBot()
    cmu = make_chat_member_updated(
        old="member", new="left", target_id=2002, username="leaver"
    )
    await feed_chat_member(dispatcher, bot, cmu, session=session)

    members = await GroupMemberRepository(session).list_members(group.id)
    assert all(m.username != "leaver" for m in members)  # no longer active


async def test_mark_left_noop_when_no_active_membership(session: AsyncSession) -> None:
    group = await seed_group(session)
    user = await UserRepository(session).upsert(
        telegram_user_id=3003, username="ghost", first_name="G", last_name=None
    )
    assert await GroupMemberRepository(session).mark_left(group.id, user.id) is False


# --- AdminGateMiddleware: refuse-until-admin --------------------------------

# A dedicated dispatcher: the gate runs on dp.message, in front of a trivial
# /ping handler that records whether it executed. Its own router (so it never
# collides with the shared session dispatcher's routers).
_ran: list[int] = []
_gate_router = Router()


@_gate_router.message(Command("ping"))
async def _ping(message: Message, uow) -> None:
    _ran.append(message.chat.id)


def _build_gate_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AdminGateMiddleware())
    dp.include_router(_gate_router)
    return dp


_GATE_DP = _build_gate_dispatcher()


async def test_gate_blocks_when_bot_not_admin(session: AsyncSession) -> None:
    await seed_group(session)  # bot_is_admin defaults False
    _ran.clear()
    bot = MockedBot(bot_is_admin=False)
    await _GATE_DP.feed_update(
        bot,
        Update(update_id=9, message=make_message("/ping")),
        uow=HarnessUoW(session),
    )

    assert _ran == []  # handler was blocked
    assert "administrator" in (bot.last_reply or "").lower()
    assert (await read_group(session)).bot_is_admin is False


async def test_gate_selfheals_and_runs_when_admin(session: AsyncSession) -> None:
    await seed_group(session)  # stored flag False, but the bot is actually admin
    _ran.clear()
    bot = MockedBot(bot_is_admin=True)
    await _GATE_DP.feed_update(
        bot,
        Update(update_id=10, message=make_message("/ping")),
        uow=HarnessUoW(session),
    )

    assert _ran == [DEFAULT_CHAT_ID]  # handler ran
    assert (await read_group(session)).bot_is_admin is True  # flag self-healed to True


async def test_gate_skips_when_group_not_set_up(session: AsyncSession) -> None:
    # No group row yet — the gate must let the message through (so /start can run).
    _ran.clear()
    bot = MockedBot(bot_is_admin=False)
    await _GATE_DP.feed_update(
        bot,
        Update(update_id=11, message=make_message("/ping")),
        uow=HarnessUoW(session),
    )

    assert _ran == [DEFAULT_CHAT_ID]  # passed through, no gate row to consult
