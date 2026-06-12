"""Handler tests for the interactive /addexpense wizard.

Drives the multi-step flow through the real Dispatcher (FSM state lives in the
harness's MemoryStorage, reset between tests by the conftest autouse fixture).
Free-text steps are fed as replies to the bot's ForceReply prompt — in the
harness every bot send is message_id 999, so a reply carries
``reply_to_message_id=999``; button taps are fed as callback queries.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, User

from aiogram.types import InlineKeyboardMarkup

from ._bot_harness import (
    MockedBot,
    feed,
    feed_callback,
    make_callback,
    make_message,
)
from ._seed import seed_event, seed_group, seed_member, seed_placeholder

# Every bot send in the harness is message_id 999, so the wizard anchor is 999;
# the ownership gate binds taps to that anchor, so taps must carry the same id.
_ANCHOR_ID = 999


def make_tap(data: str, *, from_id: int = 1001):
    """A tap on the wizard anchor (message 999) from the initiator (default)."""
    return make_callback(data, from_id=from_id, message_id=_ANCHOR_ID)


async def _expense_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(Expense))
    ).scalar_one()


async def _shares_by_username(session: AsyncSession) -> dict[str | None, int]:
    rows = (
        await session.execute(
            select(User.username, ExpenseShare.share_cents).join(
                ExpenseShare, ExpenseShare.user_id == User.id
            )
        )
    ).all()
    return {username: cents for username, cents in rows}


async def _start_to_roster(
    dispatcher, bot: MockedBot, session: AsyncSession, *, amount: str
) -> None:
    """Drive the wizard from a bare /addexpense through the amount step, leaving
    the anchor on the participant roster. Free-text steps are reply-only, so the
    amount is sent as a reply to the prompt (message_id 999 in the harness)."""
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(
        dispatcher,
        bot,
        make_message(amount, from_id=1001, reply_to_message_id=999),
        session=session,
    )


# --- routing & entry --------------------------------------------------------


async def test_bare_command_starts_wizard(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/addexpense"), session=session)
    assert "how much" in (bot.last_reply or "").lower()
    # The first prompt has no inline keyboard (it's a ForceReply), so it must
    # surface the /cancel escape hatch in text.
    assert "/cancel" in (bot.last_reply or "")
    assert await _expense_count(session) == 0


# --- amount-line description ------------------------------------------------


async def test_amount_line_accepts_inline_description(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    # Amount + free-text description on one line (no quotes; the apostrophe stays).
    await feed(
        dispatcher,
        bot,
        make_message(
            "50.25 1 night at domino's pizza", from_id=1001, reply_to_message_id=999
        ),
        session=session,
    )
    assert "For: 1 night at domino's pizza" in (bot.last_reply or "")
    assert "SGD 50.25" in (bot.last_reply or "")

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    assert "1 night at domino's pizza" in (bot.last_edit or "")
    assert await _expense_count(session) == 1


async def test_amount_line_strips_wrapping_quotes(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(
        dispatcher,
        bot,
        make_message('12 "team dinner"', from_id=1001, reply_to_message_id=999),
        session=session,
    )
    # The wrapping quotes are stripped — "For: team dinner", not 'For: "team dinner"'.
    assert "For: team dinner" in (bot.last_reply or "")


async def test_amount_step_keeps_prompt_and_reply(
    dispatcher, session: AsyncSession
) -> None:
    # The opening prompt is kept, so the reply to it is kept too (symmetry) — the
    # amount step deletes nothing; the anchor posts below.
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(
        dispatcher,
        bot,
        make_message("25", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    assert bot.deleted == []  # nothing deleted at the opening step
    assert "Who's in?" in (bot.last_reply or "")


async def test_non_reply_chatter_does_not_advance(
    dispatcher, session: AsyncSession
) -> None:
    # A message that isn't a reply to the prompt is ignored — ordinary group
    # chatter can't hijack the wizard's free-text step.
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    sent_before = len(bot.sent)
    await feed(dispatcher, bot, make_message("haha ok", from_id=1001), session=session)
    assert len(bot.sent) == sent_before  # not a reply → nothing happened
    assert await _expense_count(session) == 0
    # A proper reply to the prompt advances the step.
    await feed(
        dispatcher,
        bot,
        make_message("25", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    assert "Who's in?" in (bot.last_reply or "")


async def test_subsequent_reply_step_deletes_prompt_and_resends_anchor(
    dispatcher, session: AsyncSession
) -> None:
    # A *subsequent* reply step (here the 📝 Description prompt) deletes the bot's
    # prompt and re-sends the anchor as a fresh message, rather than editing in
    # place — clear feedback that the input was received.
    await seed_group(session)
    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="25")
    await feed_callback(dispatcher, bot, make_tap("ax:desc"), session=session)

    deletes_before = len(bot.deleted)
    sends_before = len(bot.sent)
    await feed(
        dispatcher,
        bot,
        make_message("team lunch", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    assert len(bot.deleted) > deletes_before  # the description prompt was removed
    assert 1 in {d.message_id for d in bot.deleted}  # and the user's reply (id 1)
    assert len(bot.sent) > sends_before  # the anchor came back as a new message
    assert "For: team lunch" in (bot.last_reply or "")
    assert "Who's in?" in (bot.last_reply or "")


# --- happy path: everyone, equal -------------------------------------------


async def test_everyone_equal_records_expense(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)  # alice + bob + carol + the bot
    await _start_to_roster(dispatcher, bot, session, amount="25.50")

    # The roster opens with everyone selected (matches inline "no mentions = all").
    assert "3 selected" in (bot.last_reply or "")

    # The equal split commits in ONE tap from the roster — no mode screen, no
    # separate confirm (the anchor previews the draft; /void is the undo).
    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)

    assert "Added expense" in (bot.last_edit or "")
    assert "SGD 25.50" in (bot.last_edit or "")
    # Identical equal shares collapse to one line instead of N.
    assert "SGD 8.50 each" in (bot.last_edit or "")
    # The receipt teaches the one-liner (everyone selected → no mentions).
    assert "Faster next time: /addexpense 25.50" in (bot.last_edit or "")
    assert "/void" in (bot.last_edit or "")  # undo hint on the receipt
    assert await _expense_count(session) == 1
    assert await _shares_by_username(session) == {
        "alice": 850,
        "bob": 850,
        "carol": 850,
    }


async def test_everyone_split_warns_on_coverage_gap(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    # 5 real members in the chat (+ bot) but only alice & bob have interacted.
    bot = MockedBot(member_count=6)
    await _start_to_roster(dispatcher, bot, session, amount="10")

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)

    assert "haven't interacted" in (bot.last_edit or "")
    assert await _expense_count(session) == 1


# --- participant toggle -----------------------------------------------------


async def test_toggling_a_member_out_excludes_them(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)
    await _start_to_roster(dispatcher, bot, session, amount="30")

    # Roster is ordered by username: alice(0), bob(1), carol(2). Drop carol.
    await feed_callback(dispatcher, bot, make_tap("ax:p:2"), session=session)
    assert "2 selected" in (bot.last_edit or "")

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)

    shares = await _shares_by_username(session)
    assert shares == {"alice": 1500, "bob": 1500}
    assert "carol" not in shares
    # A subset split's teaching tip spells out the @handles.
    assert "Faster next time: /addexpense 30 @alice @bob" in (bot.last_edit or "")


# --- exact split: reconcile gating -----------------------------------------


async def test_exact_split_blocks_until_reconciled(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)
    await _start_to_roster(dispatcher, bot, session, amount="30")  # 3000 cents

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:exact"), session=session)

    # Enter 10 + 10 + 5 = 2500 ≠ 3000 → Confirm must be refused.
    for idx, amount in (("ax:s:0", "10"), ("ax:s:1", "10"), ("ax:s:2", "5")):
        await feed_callback(dispatcher, bot, make_tap(idx), session=session)
        await feed(
            dispatcher,
            bot,
            make_message(amount, from_id=1001, reply_to_message_id=999),
            session=session,
        )

    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert "total" in ((bot.last_answer and bot.last_answer.text) or "").lower()
    assert await _expense_count(session) == 0

    # Fix carol's share to 10 → total 3000 → Confirm now writes.
    await feed_callback(dispatcher, bot, make_tap("ax:s:2"), session=session)
    await feed(
        dispatcher,
        bot,
        make_message("10", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    assert await _expense_count(session) == 1
    assert await _shares_by_username(session) == {
        "alice": 1000,
        "bob": 1000,
        "carol": 1000,
    }
    # The teaching tip is equal-split-only — an uneven receipt carries none.
    assert "Faster next time" not in (bot.last_edit or "")


async def test_bad_format_share_reasks_without_crashing(
    dispatcher, session: AsyncSession
) -> None:
    # Regression: a wrongly-formatted share used to crash — the re-ask replied to
    # the bad message that had just been deleted ("message to be replied not
    # found"). It must re-ask cleanly instead.
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="30")
    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:percent"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:s:0"), session=session)

    sent_before = len(bot.sent)
    # "abc" isn't a valid percent → re-ask (must not raise on a deleted reply).
    await feed(
        dispatcher,
        bot,
        make_message("abc", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    assert len(bot.sent) > sent_before  # a fresh re-ask was sent
    assert "percentage" in (bot.last_reply or "").lower()
    assert await _expense_count(session) == 0


# --- ✏️ amount edit & fast-path hardening ------------------------------------


async def test_amount_edit_keeps_selection_and_description(
    dispatcher, session: AsyncSession
) -> None:
    # ✏️ Amount re-asks mid-flow: the new amount lands without resetting the
    # toggled selection or the description (a mistyped amount used to mean
    # cancel-and-restart).
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")
    bot = MockedBot(member_count=4)
    await _start_to_roster(dispatcher, bot, session, amount="30 team lunch")

    # Drop carol, then re-enter the amount.
    await feed_callback(dispatcher, bot, make_tap("ax:p:2"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:amt"), session=session)
    assert "new amount" in (bot.last_reply or "")

    await feed(
        dispatcher,
        bot,
        make_message("40", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    anchor = bot.last_reply or ""
    assert "SGD 40" in anchor  # the amount changed…
    assert "For: team lunch" in anchor  # …the description survived…
    assert "2 selected" in anchor  # …and so did the selection.

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    assert await _shares_by_username(session) == {"alice": 2000, "bob": 2000}


async def test_crafted_equal_mode_callback_is_ignored(
    dispatcher, session: AsyncSession
) -> None:
    # The mode screen no longer offers Equal — a crafted ax:m:equal must not
    # move the flow (equal commits only via the roster's ax:eq).
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="20")
    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)

    edits_before = len(bot.edits)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    assert len(bot.edits) == edits_before  # no repaint — the tap was a no-op
    assert await _expense_count(session) == 0


async def test_non_reply_amount_gets_nudge(dispatcher, session: AsyncSession) -> None:
    # The reply box was dismissed and the amount arrives as a plain message: the
    # wizard can't read it (free-text steps are reply-only), but silence reads
    # as a dead bot — nudge exactly when the text parses as an amount (ordinary
    # chatter stays ignored, see test_non_reply_chatter_does_not_advance).
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(dispatcher, bot, make_message("25.50", from_id=1001), session=session)
    assert "repl" in (bot.last_reply or "").lower()  # "I only catch replies…"
    assert await _expense_count(session) == 0


async def test_roster_marks_pending_placeholder(
    dispatcher, session: AsyncSession
) -> None:
    # A placeholder (mentioned but never seen) is flagged ⏳ where people are
    # chosen, so a typo'd @handle is caught at the decision point.
    group = await seed_group(session)
    await seed_placeholder(session, group, username="ghost")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="10")

    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("@ghost ⏳" in label for label in labels)
    assert not any("⏳" in label for label in labels if "@alice" in label)


# --- cancellation -----------------------------------------------------------


async def test_cancel_button_aborts(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="12")
    await feed_callback(dispatcher, bot, make_tap("ax:x"), session=session)

    assert "cancelled" in (bot.last_edit or "").lower()
    assert await _expense_count(session) == 0

    # State is cleared: a further tap matches no handler and records nothing.
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert await _expense_count(session) == 0


async def test_command_at_amount_step_is_not_eaten(
    dispatcher, session: AsyncSession
) -> None:
    # A command typed mid-wizard must route to its own handler, not be parsed as
    # the amount — the free-text step filter excludes anything starting with "/".
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(dispatcher, bot, make_message("/cancel", from_id=1001), session=session)
    assert "ancel" in (bot.last_reply or "")
    assert "Invalid amount" not in (bot.last_reply or "")


# --- ownership --------------------------------------------------------------


async def test_other_user_cannot_drive_draft(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="40")

    # Bob (2002) taps the initiator's (alice, 1001) anchor — rejected.
    await feed_callback(
        dispatcher, bot, make_tap("ax:clear", from_id=2002), session=session
    )
    assert "isn't your" in ((bot.last_answer and bot.last_answer.text) or "").lower()

    # The draft is untouched: alice can still commit everyone.
    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    assert await _expense_count(session) == 1


# --- crafted-callback hardening --------------------------------------------


async def test_malformed_participant_index_is_ignored(
    dispatcher, session: AsyncSession
) -> None:
    # callback_data normally comes from bot-rendered buttons, but a crafted tap
    # with a non-numeric or out-of-range index must not raise (ValueError on int())
    # or seed `selected` with an index that later IndexErrors at submit.
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="20")

    await feed_callback(dispatcher, bot, make_tap("ax:p:abc"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:p:99"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:pg:xyz"), session=session)

    # The draft is intact: both real members are still selected and it commits.
    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    assert await _expense_count(session) == 1
    assert set(await _shares_by_username(session)) == {"alice", "bob"}


async def test_malformed_share_index_is_ignored(
    dispatcher, session: AsyncSession
) -> None:
    # The s: branch looks up data["roster"][idx]; a crafted non-numeric index must
    # be parsed defensively rather than crashing on int().
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="30")
    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:percent"), session=session)

    sent_before = len(bot.sent)
    await feed_callback(dispatcher, bot, make_tap("ax:s:abc"), session=session)
    assert len(bot.sent) == sent_before  # no share prompt sent, no crash
    assert await _expense_count(session) == 0


async def test_submit_is_guarded_against_double_tap(
    dispatcher, session: AsyncSession
) -> None:
    # A double-tapped commit (the roster's equal fast path here) must not record
    # the expense twice in the append-only ledger. The second callback sees the
    # `submitting` flag the first set and bails. The harness is sequential, so
    # simulate an in-flight first submit by pre-setting the flag, then feed the
    # second tap.
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await _start_to_roster(dispatcher, bot, session, amount="20")

    storage = dispatcher.storage
    key = next(iter(storage.storage))  # the single live draft (alice)
    data = await storage.get_data(key)
    data["submitting"] = True
    await storage.set_data(key, data)

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    assert await _expense_count(session) == 0  # guarded — no duplicate write


# --- events / #general parity ----------------------------------------------


async def test_event_scope_defaults_and_general_toggle(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_event(session, group, creator=alice, name="Trip")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="20")

    # An active event scopes the draft to the event roster (just alice so far).
    assert "Scope: Trip" in (bot.last_reply or "")
    assert "1 selected" in (bot.last_reply or "")

    # #general flips to the whole-group, no-event scope (alice + bob).
    await feed_callback(dispatcher, bot, make_tap("ax:gen"), session=session)
    assert "Scope: General" in (bot.last_edit or "")
    assert "2 selected" in (bot.last_edit or "")


async def test_general_toggle_rederives_default_currency(
    dispatcher, session: AsyncSession
) -> None:
    # A bare amount means "the scope's money". Under a JPY event, "500" is
    # JPY 500 — flipping to #general must re-label it as the group's SGD (and
    # back), exactly as the inline path resolves scope before parsing.
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    await seed_event(session, group, creator=alice, name="Trip", default_currency="JPY")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="500")
    assert "JPY 500.00" in (bot.last_reply or "")

    await feed_callback(dispatcher, bot, make_tap("ax:gen"), session=session)
    assert "SGD 500.00" in (bot.last_edit or "")  # general scope → group default

    await feed_callback(dispatcher, bot, make_tap("ax:gen"), session=session)
    assert "JPY 500.00" in (bot.last_edit or "")  # back to the event's currency

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    row = (await session.execute(select(Expense))).scalar_one()
    assert row.currency == "JPY"


async def test_pinned_currency_survives_general_toggle(
    dispatcher, session: AsyncSession
) -> None:
    # An explicitly typed currency (EUR50) is the user's choice — the scope
    # flip must not relabel it.
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    await seed_event(session, group, creator=alice, name="Trip", default_currency="JPY")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="EUR50")
    assert "EUR 50.00" in (bot.last_reply or "")

    await feed_callback(dispatcher, bot, make_tap("ax:gen"), session=session)
    assert "EUR 50.00" in (bot.last_edit or "")

    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)
    row = (await session.execute(select(Expense))).scalar_one()
    assert row.currency == "EUR"
    assert row.event_id is None  # forced general
    # The tip must spell the pinned currency out to round-trip under #general.
    assert "Faster next time: /addexpense EUR50 #general" in (bot.last_edit or "")


async def test_event_scope_tags_expense_to_event(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    event = await seed_event(session, group, creator=alice, name="Trip")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="20")
    await feed_callback(dispatcher, bot, make_tap("ax:eq"), session=session)

    assert 'Added to "Trip"' in (bot.last_edit or "")
    row = (await session.execute(select(Expense))).scalar_one()
    assert row.event_id == event.event_id
