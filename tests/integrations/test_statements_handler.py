"""Handler tests for /statements — the message view and inline paging callbacks.

Exercises the callback path the service tests can't: the owner-binding check
(only your own taps page your statement) and that a page tap repaints via
edit_text.
"""

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, Settlement
from countbeans.services.repositories import SettlementRepository

from ._bot_harness import MockedBot, feed, feed_callback, make_callback, make_message
from ._seed import (
    seed_event,
    seed_expense,
    seed_group,
    seed_member,
    seed_settlement,
)


async def test_statements_personal_view(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Your statement" in reply
    assert "No transactions yet" in reply


async def test_statements_group_view(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/statements all"), session=session)
    assert "Group statement" in (bot.last_reply or "")


async def test_statements_unrecognized_arg_notes_and_shows_personal(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements mine", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert 'didn\'t recognize "mine"' in reply
    assert "/statements all" in reply
    assert "Your statement" in reply  # personal view still rendered


async def test_statements_me_is_personal_without_note(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements me", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Your statement" in reply
    assert "didn't recognize" not in reply


async def test_statements_group_page_callback_repaints(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed_callback(
        dispatcher, bot, make_callback("stmt:g:0", from_id=1001), session=session
    )

    assert "Group statement" in (bot.last_edit or "")  # repainted via edit_text
    assert bot.last_answer is not None  # the tap was acknowledged


async def test_statements_tags_event_scope(dispatcher, session: AsyncSession) -> None:
    """Statements span all scopes; an event-tagged expense is labelled with the
    event name so it's distinguishable from a general one in the same list."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    event = await seed_event(session, group, creator=caller, name="Bali")
    # One general expense and one event-tagged expense.
    await seed_expense(
        session, group, payer=caller, participants=[caller, bob], amount_cents=1000
    )
    await seed_expense(
        session,
        group,
        payer=caller,
        participants=[caller, bob],
        amount_cents=2000,
        event_id=event.event_id,
        description="trip dinner",
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements all", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "🏷️ Bali" in reply  # the event-tagged entry carries its scope
    # The general expense line is present but untagged (one 🏷️ total).
    assert reply.count("🏷️") == 1


async def test_statements_flag_voided_settlement(
    dispatcher, session: AsyncSession
) -> None:
    """A voided settlement stays on the statement, struck out like a voided
    expense — the statement is the audit trail."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    settlement_id = await seed_settlement(
        session, group, from_user=caller, to_user=bob, amount_cents=500
    )
    repo = SettlementRepository(session)
    row = (
        await session.execute(select(Settlement).where(Settlement.id == settlement_id))
    ).scalar_one()
    await repo.mark_voided(row, voided_by=caller.id)

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements all", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "❌ 💸" in reply
    assert "(voided)" in reply


def _sent_buttons(bot: MockedBot) -> list[tuple[str, str]]:
    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "reply carries no keyboard"
    return [
        (b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row
    ]


def _edit_buttons(bot: MockedBot) -> list[tuple[str, str]]:
    markup = bot.edits[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "edit carries no keyboard"
    return [
        (b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row
    ]


async def test_void_from_statement_full_flow(dispatcher, session: AsyncSession) -> None:
    """Statement → 🗑️ Void an entry → pick the entry → confirm → it's voided
    (the confirm's Yes reuses void.py's vd: path)."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(
        session,
        group,
        payer=caller,
        participants=[caller],
        amount_cents=1000,
        description="oops",
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements all", from_id=1001, username="caller"),
        session=session,
    )
    void_entry_btn = next(d for _, d in _sent_buttons(bot) if d.startswith("sv:m:"))

    # Enter pick mode.
    await feed_callback(
        dispatcher, bot, make_callback(void_entry_btn, from_id=1001), session=session
    )
    pick = next(d for t, d in _edit_buttons(bot) if d.startswith("sv:k:"))
    assert "Tap an entry to void" in (bot.last_edit or "")

    # Pick the entry → confirm screen with a Yes (vd:ok) button.
    await feed_callback(
        dispatcher,
        bot,
        make_callback(pick, from_id=1001, username="caller"),
        session=session,
    )
    assert "Void this expense" in (bot.last_edit or "")
    yes = next(d for _, d in _edit_buttons(bot) if d.startswith("vd:ok:"))

    # Confirm → void.py records the void.
    await feed_callback(
        dispatcher,
        bot,
        make_callback(yes, from_id=1001, username="caller"),
        session=session,
    )
    assert "Voided" in (bot.last_edit or "")
    voided_at = (await session.execute(select(Expense.voided_at))).scalar_one()
    assert voided_at is not None


async def test_void_from_statement_cancel_returns_to_statement(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/statements all", from_id=1001, username="caller"),
        session=session,
    )
    void_btn = next(d for _, d in _sent_buttons(bot) if d.startswith("sv:m:"))
    await feed_callback(
        dispatcher, bot, make_callback(void_btn, from_id=1001), session=session
    )
    cancel = next(d for _, d in _edit_buttons(bot) if d.startswith("sv:c:"))
    await feed_callback(
        dispatcher, bot, make_callback(cancel, from_id=1001), session=session
    )

    assert "Group statement" in (bot.last_edit or "")  # back to the statement


async def test_void_from_statement_blocks_non_party(
    dispatcher, session: AsyncSession
) -> None:
    """On a group statement, picking an entry you can't void shows who can and
    offers no Yes button."""
    group = await seed_group(session)
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_expense(session, group, payer=bob, participants=[bob], amount_cents=1000)

    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/statements all", from_id=1001, username="caller"),
        session=session,
    )
    void_btn = next(d for _, d in _sent_buttons(bot) if d.startswith("sv:m:"))
    await feed_callback(
        dispatcher, bot, make_callback(void_btn, from_id=1001), session=session
    )
    pick = next(d for _, d in _edit_buttons(bot) if d.startswith("sv:k:"))
    await feed_callback(
        dispatcher,
        bot,
        make_callback(pick, from_id=1001, username="caller"),
        session=session,
    )

    assert "Only @bob" in (bot.last_edit or "")
    assert not any(d.startswith("vd:ok:") for _, d in _edit_buttons(bot))


async def test_void_pick_on_personal_statement_is_owner_bound(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=caller, participants=[caller], amount_cents=1000
    )

    bot = MockedBot()
    # caller opens their personal statement…
    await feed(
        dispatcher,
        bot,
        make_message("/statements", from_id=1001, username="caller"),
        session=session,
    )
    void_btn = next(d for _, d in _sent_buttons(bot) if d.startswith("sv:m:"))
    # …bob taps the void button on it → rejected (it's not bob's statement).
    await feed_callback(
        dispatcher,
        bot,
        make_callback(void_btn, from_id=2002, username="bob"),
        session=session,
    )
    answer = bot.last_answer
    assert answer is not None and answer.show_alert
    assert "not your statement" in (answer.text or "").lower()
    assert not bot.edits


async def test_statements_other_users_page_is_rejected(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    # Caller 1001 taps a page button bound to subject 9999 — not theirs.
    await feed_callback(
        dispatcher, bot, make_callback("stmt:u:9999:0", from_id=1001), session=session
    )

    assert bot.last_answer is not None
    assert "not your statement" in (bot.last_answer.text or "").lower()
    assert bot.edits == []  # nothing repainted
