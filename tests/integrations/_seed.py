"""Seeding helpers for handler tests that need pre-existing ledger state.

These write directly through the repositories / service core (not the bot) so a
test can set up "two members with a debt between them" before driving a command.
Everything uses DEFAULT_CHAT_ID so the group a handler upserts is the same one
seeded here.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Group, User
from countbeans.dto.commands import AddExpenseCommand, CreateEventCommand
from countbeans.dto.results import EventCreatedResult
from countbeans.services.add_expense import add_expense
from countbeans.services.events import create_event
from countbeans.services.repositories import (
    GroupMemberRepository,
    GroupRepository,
    SettlementRepository,
    UserRepository,
)

from ._bot_harness import DEFAULT_CHAT_ID, HarnessUoW


async def seed_group(session: AsyncSession, *, chat_id: int = DEFAULT_CHAT_ID) -> Group:
    return await GroupRepository(session).upsert(
        telegram_chat_id=chat_id, group_name="Test Group"
    )


async def read_group(session: AsyncSession) -> Group:
    """Re-read the default group's current row (e.g. to assert a setting changed)."""
    return (
        await session.execute(
            select(Group).where(Group.telegram_chat_id == DEFAULT_CHAT_ID)
        )
    ).scalar_one()


async def seed_member(
    session: AsyncSession,
    group: Group,
    *,
    telegram_user_id: int,
    username: str,
    first_name: str | None = None,
) -> User:
    """Onboard a claimed user and add them to the group (an active member)."""
    user = await UserRepository(session).upsert(
        telegram_user_id=telegram_user_id,
        username=username,
        first_name=first_name or username.capitalize(),
        last_name=None,
    )
    await GroupMemberRepository(session).ensure_member(group.id, user.id)
    return user


async def seed_placeholder(
    session: AsyncSession, group: Group, *, username: str
) -> User:
    """Create a pending placeholder (telegram_user_id IS NULL) and add it to the
    group — mirroring how a real mention (`/addexpense @x`) tracks an unseen user,
    so the group-scoped claim gate (security review #1) sees it as referenced here."""
    user = await UserRepository(session).resolve_mention(username)
    await GroupMemberRepository(session).ensure_member(group.id, user.id)
    return user


async def seed_event(
    session: AsyncSession,
    group: Group,
    *,
    creator: User,
    name: str = "Trip",
    default_currency: str | None = None,
) -> EventCreatedResult:
    """Open an event via the real service (sets the active pointer, adds the
    creator to the roster). Returns the EventCreatedResult (has .event_id)."""
    result = await create_event(
        HarnessUoW(session),
        CreateEventCommand(
            group_id=group.id,
            name=name,
            created_by=creator.id,
            default_currency=default_currency,
        ),
    )
    await session.flush()
    return result


async def seed_settlement(
    session: AsyncSession,
    group: Group,
    *,
    from_user: User,
    to_user: User,
    amount_cents: int,
    currency: str = "SGD",
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record a settlement directly through the repository (no suggested-transfer
    validation — tests pair it with a seeded debt). Returns the settlement id."""
    result = await SettlementRepository(session).add(
        group_id=group.id,
        event_id=event_id,
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        amount_cents=amount_cents,
        currency=currency,
    )
    await session.flush()
    return result.settlement_id


async def seed_expense(
    session: AsyncSession,
    group: Group,
    *,
    payer: User,
    participants: list[User],
    amount_cents: int,
    currency: str = "SGD",
    description: str = "seed",
    event_id: uuid.UUID | None = None,
) -> None:
    """Record an even-split expense via the real service (so balances derive). Pass
    event_id to tag it to an event scope."""
    await add_expense(
        HarnessUoW(session),
        AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[p.id for p in participants],
            event_id=event_id,
            created_by=payer.id,
        ),
    )
    await session.flush()
