"""Seeding helpers for handler tests that need pre-existing ledger state.

These write directly through the repositories / service core (not the bot) so a
test can set up "two members with a debt between them" before driving a command.
Everything uses DEFAULT_CHAT_ID so the group a handler upserts is the same one
seeded here.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Group, User
from countbeans.dto.commands import AddExpenseCommand
from countbeans.services.add_expense import add_expense
from countbeans.services.repositories import (
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)

from ._bot_harness import DEFAULT_CHAT_ID, HarnessUoW


async def seed_group(session: AsyncSession) -> Group:
    return await GroupRepository(session).upsert(
        telegram_chat_id=DEFAULT_CHAT_ID, group_name="Test Group"
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


async def seed_expense(
    session: AsyncSession,
    group: Group,
    *,
    payer: User,
    participants: list[User],
    amount_cents: int,
    currency: str = "SGD",
    description: str = "seed",
) -> None:
    """Record an even-split expense via the real service (so balances derive)."""
    await add_expense(
        HarnessUoW(session),
        AddExpenseCommand(
            group_id=group.id,
            payer_id=payer.id,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            participants=[p.id for p in participants],
            created_by=payer.id,
        ),
    )
    await session.flush()
