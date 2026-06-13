"""Handler tests for /void — preview-then-confirm undo of the most recent expense.

/void never writes on its own: it replies with a preview of the expense it would
void plus confirm/keep buttons, and only the caller's confirm tap voids. Covers:
the preview is a pure read; confirm clears the expense from derived balances;
keep leaves it; the buttons are bound to the caller; a stale confirm (already
voided) is a no-op; permission refusal for a non-admin non-owner happens at
preview (no buttons); and scope isolation (an active event's void never touches
a general expense, and vice versa).
"""

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense
from countbeans.services.repositories import BalanceRepository, GroupRepository

from ._bot_harness import MockedBot, feed, feed_callback, make_callback, make_message
from ._seed import (
    read_group,
    seed_event,
    seed_expense,
    seed_group,
    seed_member,
)


async def _active_expense_count(
    session: AsyncSession, *, event_scoped: bool = False
) -> int:
    """Number of non-voided expenses, optionally restricted to event-tagged rows."""
    q = select(func.count()).select_from(Expense).where(Expense.voided_at.is_(None))
    if event_scoped:
        q = q.where(Expense.event_id.is_not(None))
    else:
        q = q.where(Expense.event_id.is_(None))
    return (await session.execute(q)).scalar_one()


def _buttons(bot: MockedBot) -> tuple[str, str]:
    """The (confirm, keep) callback data on the most recent preview reply."""
    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "preview reply carries no keyboard"
    row = markup.inline_keyboard[0]
    confirm, keep = row[0].callback_data, row[1].callback_data
    assert confirm is not None and confirm.startswith("vd:ok:")
    assert keep is not None and keep.startswith("vd:x:")
    return confirm, keep


async def test_void_previews_then_confirm_clears_balances(
    dispatcher, session: AsyncSession
) -> None:
    """/void shows the expense first (no write); the confirm tap voids it and it
    drops out of derived balances."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # caller fronts SGD 10 split evenly → bob owes caller SGD 5.
    await seed_expense(
        session, group, payer=caller, participants=[caller, bob], amount_cents=1000
    )
    assert await BalanceRepository(session).compute_for_group(group.id)  # non-empty

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )

    preview = bot.last_reply or ""
    assert "Void this expense" in preview
    assert "10.00" in preview
    assert "@caller" in preview  # names the payer
    # Preview is a pure read — nothing voided yet.
    assert await _active_expense_count(session) == 1

    confirm, _ = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )

    assert "Voided" in (bot.last_edit or "")
    assert "10.00" in (bot.last_edit or "")
    # Voided rows are excluded from the derivation → no outstanding balances.
    assert await BalanceRepository(session).compute_for_group(group.id) == {}
    assert await _active_expense_count(session) == 0


async def test_void_keep_leaves_expense(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    _, keep = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(keep, from_id=1001, username="caller"),
        session=session,
    )

    assert "Kept" in (bot.last_edit or "")
    assert await _active_expense_count(session) == 1


async def test_void_buttons_bound_to_caller(dispatcher, session: AsyncSession) -> None:
    """Someone else tapping the caller's confirm gets an alert; nothing voided."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    confirm, _ = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=2002, username="bob"),
        session=session,
    )

    answer = bot.last_answer
    assert answer is not None and answer.show_alert
    assert "person who ran /void" in (answer.text or "")
    assert not bot.edits  # the preview wasn't repainted
    assert await _active_expense_count(session) == 1


async def test_void_stale_confirm_is_a_noop(dispatcher, session: AsyncSession) -> None:
    """Two previews of the same expense: confirming the first voids it; the
    second (now stale) confirm reports it's already gone instead of re-voiding."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    confirm, _ = _buttons(bot)

    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )
    assert "Voided" in (bot.last_edit or "")

    # Same callback data again — a double-tap or a second stale preview.
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )
    assert "already voided or gone" in (bot.last_edit or "")
    assert await _active_expense_count(session) == 0


async def test_void_crafted_callback_is_ignored(
    dispatcher, session: AsyncSession
) -> None:
    """A confirm whose expense id isn't a UUID answers silently — no edit, no write."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    await feed_callback(
        dispatcher,
        bot,
        make_callback("vd:ok:not-a-uuid:1001", from_id=1001, username="caller"),
        session=session,
    )

    assert not bot.edits
    assert await _active_expense_count(session) == 1


async def test_void_refused_for_non_owner_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    """A member who neither paid nor recorded the expense, and isn't an admin,
    gets the refusal at preview time — no buttons, nothing written."""
    group = await seed_group(session)
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=1001, username="caller")
    # bob both pays and records the expense.
    await seed_expense(session, group, payer=bob, participants=[bob], amount_cents=1000)

    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "@bob" in reply  # names who is allowed to void it
    assert "admin" in reply.lower()
    assert bot.sent[-1].reply_markup is None  # a refusal, not a confirm screen
    # Nothing voided.
    assert await _active_expense_count(session) == 1
    assert (
        await session.execute(
            select(Expense.voided_at).where(Expense.payer_id == bob.id)
        )
    ).scalar_one() is None


async def test_void_allowed_for_admin_non_owner(
    dispatcher, session: AsyncSession
) -> None:
    """A group admin may void an expense someone else recorded (preview + confirm)."""
    group = await seed_group(session)
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(session, group, payer=bob, participants=[bob], amount_cents=1000)

    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    assert "Void this expense" in (bot.last_reply or "")
    confirm, _ = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )

    assert "Voided" in (bot.last_edit or "")
    assert await _active_expense_count(session) == 0


async def test_void_nothing_to_void(dispatcher, session: AsyncSession) -> None:
    """No active expense in scope → a clear 'nothing to void' reply, no error."""
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=1001, username="caller")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )

    assert "Nothing to void" in (bot.last_reply or "")


async def test_void_in_event_leaves_general_expense(
    dispatcher, session: AsyncSession
) -> None:
    """With an event active, /void targets the event scope and never touches a
    general expense — scope isolation, through preview and confirm."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # A general expense first…
    await seed_expense(
        session, group, payer=caller, participants=[caller, bob], amount_cents=1000
    )
    # …then open an event (sets the active pointer) and add an event-scoped expense.
    event = await seed_event(session, group, creator=caller, name="Bali")
    await seed_expense(
        session,
        group,
        payer=caller,
        participants=[caller, bob],
        amount_cents=2000,
        event_id=event.event_id,
    )
    assert (await read_group(session)).active_event_id == event.event_id

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    preview = bot.last_reply or ""
    assert 'in "Bali"' in preview  # scope note proves it previews the event scope
    assert "20.00" in preview

    confirm, _ = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )

    edited = bot.last_edit or ""
    assert 'in "Bali"' in edited
    assert "20.00" in edited
    # The event expense is gone; the general one survives.
    assert await _active_expense_count(session, event_scoped=True) == 0
    assert await _active_expense_count(session, event_scoped=False) == 1
    # General balance untouched.
    assert await BalanceRepository(session).compute_for_group(group.id) != {}


async def test_void_general_leaves_event_expense(
    dispatcher, session: AsyncSession
) -> None:
    """The mirror: with no event active, /void targets the general scope and
    never touches an event-tagged expense (even a more-recent one)."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=caller, participants=[caller, bob], amount_cents=1000
    )
    event = await seed_event(session, group, creator=caller, name="Bali")
    await seed_expense(
        session,
        group,
        payer=caller,
        participants=[caller, bob],
        amount_cents=2000,
        event_id=event.event_id,
    )
    # Pause the event so the group is back in general-tracking mode.
    await GroupRepository(session).set_active_event(group.id, None)
    await session.flush()

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/void", from_id=1001, username="caller"),
        session=session,
    )
    preview = bot.last_reply or ""
    assert '"Bali"' not in preview  # general scope has no event note
    assert "10.00" in preview  # the general expense, not the more-recent event one

    confirm, _ = _buttons(bot)
    await feed_callback(
        dispatcher,
        bot,
        make_callback(confirm, from_id=1001, username="caller"),
        session=session,
    )

    edited = bot.last_edit or ""
    assert "Voided" in edited
    assert '"Bali"' not in edited
    assert "10.00" in edited
    # The general expense is gone; the event one survives.
    assert await _active_expense_count(session, event_scoped=False) == 0
    assert await _active_expense_count(session, event_scoped=True) == 1
