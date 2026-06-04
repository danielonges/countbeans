"""Integration tests for the event-scope service core — lifecycle + scope isolation.

Needs Postgres (the partial unique index and per-scope SQL aggregation can't be
exercised in memory). Each test rolls back via the conftest `session` fixture.
"""
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Event, Settlement
from countbeans.dto.commands import (
    CreateEventCommand,
    EditEventRosterCommand,
    SetActiveEventCommand,
    SetEventStatusCommand,
    SettleUpCommand,
)
from countbeans.dto.domain import BalanceKey
from countbeans.services.balance import compute_balances
from countbeans.services.events import (
    close_event,
    create_event,
    edit_event_roster,
    set_active_event,
)
from countbeans.services.repositories import EventRepository
from countbeans.services.settlement import settle_up

from ._bot_harness import HarnessUoW
from ._seed import read_group, seed_event, seed_expense, seed_group, seed_member


async def test_create_event_sets_active_pointer_and_roster(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")

    result = await create_event(
        HarnessUoW(session),
        CreateEventCommand(group_id=group.id, name="Bali", created_by=creator.id),
    )
    await session.flush()

    assert (await read_group(session)).active_event_id == result.event_id
    roster = await EventRepository(session).list_members(result.event_id)
    assert {m.user_id for m in roster} == {creator.id}


async def test_second_open_event_rejected(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    await seed_event(session, group, creator=creator, name="One")

    with pytest.raises(ValueError, match="already open"):
        await create_event(
            HarnessUoW(session),
            CreateEventCommand(group_id=group.id, name="Two", created_by=creator.id),
        )


async def test_close_frees_slot_and_clears_pointer(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    first = await seed_event(session, group, creator=creator, name="One")

    await close_event(
        HarnessUoW(session),
        group.id,
        SetEventStatusCommand(event_id=first.event_id, status="closed"),
    )
    await session.flush()

    assert (await read_group(session)).active_event_id is None
    closed = await EventRepository(session).get(first.event_id)
    assert closed is not None and closed.status == "closed" and closed.closed_at is not None
    # The slot is free: a new event can open.
    second = await seed_event(session, group, creator=creator, name="Two")
    assert (await read_group(session)).active_event_id == second.event_id


async def test_pause_resume_toggles_pointer(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    ev = await seed_event(session, group, creator=creator, name="Trip")

    await set_active_event(
        HarnessUoW(session), SetActiveEventCommand(group_id=group.id, event_id=None)
    )
    await session.flush()
    assert (await read_group(session)).active_event_id is None  # paused, still open

    await set_active_event(
        HarnessUoW(session),
        SetActiveEventCommand(group_id=group.id, event_id=ev.event_id),
    )
    await session.flush()
    assert (await read_group(session)).active_event_id == ev.event_id


async def test_roster_add_and_remove(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    ev = await seed_event(session, group, creator=creator, name="Trip")
    uow = HarnessUoW(session)

    assert await edit_event_roster(
        uow, EditEventRosterCommand(event_id=ev.event_id, user_id=bob.id, action="add")
    )
    await session.flush()
    assert {m.user_id for m in await uow.events.list_members(ev.event_id)} == {creator.id, bob.id}

    assert await edit_event_roster(
        uow, EditEventRosterCommand(event_id=ev.event_id, user_id=bob.id, action="remove")
    )
    await session.flush()
    assert {m.user_id for m in await uow.events.list_members(ev.event_id)} == {creator.id}


async def test_scopes_are_isolated(session: AsyncSession) -> None:
    group = await seed_group(session)
    a = await seed_member(session, group, telegram_user_id=1001, username="a")
    b = await seed_member(session, group, telegram_user_id=2002, username="b")

    # General: a fronts 10, split a+b → b owes a 5.
    await seed_expense(session, group, payer=a, participants=[a, b], amount_cents=1000)
    # Event: b fronts 20, split a+b → a owes b 10 (in the event scope only).
    ev = await seed_event(session, group, creator=a, name="Trip")
    await seed_expense(
        session, group, payer=b, participants=[a, b], amount_cents=2000, event_id=ev.event_id
    )

    uow = HarnessUoW(session)
    general = await compute_balances(uow, group.id)
    event = await compute_balances(uow, group.id, event_id=ev.event_id)

    # General excludes the event-tagged rows; the event derives only its own.
    assert general[BalanceKey(a.id, "SGD")] == 500
    assert general[BalanceKey(b.id, "SGD")] == -500
    assert event[BalanceKey(a.id, "SGD")] == -1000
    assert event[BalanceKey(b.id, "SGD")] == 1000
    # Per-(scope, currency) sum-to-zero.
    assert sum(general.values()) == 0
    assert sum(event.values()) == 0


async def test_settling_in_event_leaves_general_untouched(session: AsyncSession) -> None:
    group = await seed_group(session)
    a = await seed_member(session, group, telegram_user_id=1001, username="a")
    b = await seed_member(session, group, telegram_user_id=2002, username="b")

    await seed_expense(session, group, payer=a, participants=[a, b], amount_cents=1000)  # general
    ev = await seed_event(session, group, creator=a, name="Trip")
    await seed_expense(
        session, group, payer=b, participants=[a, b], amount_cents=2000, event_id=ev.event_id
    )  # event: a owes b 10

    uow = HarnessUoW(session)
    await settle_up(
        uow,
        SettleUpCommand(
            group_id=group.id,
            from_user_id=a.id,
            to_user_id=b.id,
            amount_cents=1000,
            currency="SGD",
            event_id=ev.event_id,
            created_by=a.id,
        ),
        simplify_debts=True,
    )
    await session.flush()

    # The settlement is tagged to the event and zeroes only the event scope.
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.event_id == ev.event_id
    assert await compute_balances(uow, group.id, event_id=ev.event_id) == {}
    general = await compute_balances(uow, group.id)
    assert general[BalanceKey(a.id, "SGD")] == 500
    assert general[BalanceKey(b.id, "SGD")] == -500


async def test_partial_index_rejects_two_open_events(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    session.add_all(
        [
            Event(id=uuid_utils.uuid7(), group_id=group.id, name="A", status="open", created_by=creator.id),
            Event(id=uuid_utils.uuid7(), group_id=group.id, name="B", status="open", created_by=creator.id),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()
