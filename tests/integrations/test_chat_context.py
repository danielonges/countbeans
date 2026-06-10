"""Integration tests for bot/utils/context.py — the shared per-command context.

resolve_chat_context owns the opening sequence every group command shares:
group upsert → caller upsert (group-scoped claim) → membership → active-event
fetch. Driven directly (no dispatcher) with a HarnessUoW over the rolled-back
session, like the seed helpers.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.bot.utils.context import resolve_chat_context

from ._bot_harness import DEFAULT_CHAT_ID, HarnessUoW, make_message
from ._seed import seed_event, seed_group, seed_member


async def test_creates_group_caller_and_membership(session: AsyncSession) -> None:
    uow = HarnessUoW(session)
    ctx = await resolve_chat_context(
        uow, make_message("/balance", from_id=1001, username="caller")
    )
    assert ctx.group.telegram_chat_id == DEFAULT_CHAT_ID
    assert ctx.caller.telegram_user_id == 1001
    members = await uow.group_members.list_members(ctx.group.id)
    assert [m.user_id for m in members] == [ctx.caller.id]
    # No active event → general scope throughout.
    assert ctx.active_event is None
    assert ctx.scoped_event is None
    assert ctx.event_id is None
    assert ctx.scope_note == ""
    assert ctx.currency == ctx.group.default_currency


async def test_resolving_twice_is_idempotent(session: AsyncSession) -> None:
    uow = HarnessUoW(session)
    msg = make_message("/balance", from_id=1001, username="caller")
    first = await resolve_chat_context(uow, msg)
    second = await resolve_chat_context(uow, msg)
    assert second.group.id == first.group.id
    assert second.caller.id == first.caller.id
    members = await uow.group_members.list_members(first.group.id)
    assert len(members) == 1


async def test_active_event_and_currency_fallback(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="caller"
    )
    event = await seed_event(
        session, group, creator=creator, name="Bali", default_currency="IDR"
    )

    ctx = await resolve_chat_context(
        HarnessUoW(session), make_message("/balance", from_id=1001, username="caller")
    )
    assert ctx.active_event is not None and ctx.active_event.id == event.event_id
    assert ctx.scoped_event is ctx.active_event
    assert ctx.event_id == event.event_id
    assert ctx.scope_note == ' in "Bali"'
    # Event default wins over the group default while the event is in scope.
    assert ctx.currency == "IDR"


async def test_scoped_force_general_overrides_event(session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="caller"
    )
    await seed_event(
        session, group, creator=creator, name="Bali", default_currency="IDR"
    )

    ctx = await resolve_chat_context(
        HarnessUoW(session), make_message("/addexpense 5", from_id=1001)
    )
    overridden = ctx.scoped(force_general=True)
    # The override empties the *effective* scope but keeps the active event
    # visible (the "Logged as general — not tagged to …" note needs its name).
    assert overridden.active_event is ctx.active_event
    assert overridden.scoped_event is None
    assert overridden.event_id is None
    assert overridden.scope_note == ""
    assert overridden.currency == group.default_currency
    # scoped() is a copy — the original context is unchanged (frozen dataclass).
    assert ctx.force_general is False and ctx.event_id is not None


async def test_event_without_currency_inherits_group_default(
    session: AsyncSession,
) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="caller"
    )
    await seed_event(session, group, creator=creator, name="Dinner")  # no currency

    ctx = await resolve_chat_context(
        HarnessUoW(session), make_message("/balance", from_id=1001)
    )
    assert ctx.scoped_event is not None
    assert ctx.currency == group.default_currency
