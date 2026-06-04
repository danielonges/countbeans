"""Integration tests for the add_expense service function.

Uses an ephemeral Postgres container (Testcontainers via conftest.py).
Each test rolls back via the session fixture — no permanent state.
"""

import uuid

import pytest
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, User
from countbeans.dto.commands import AddExpenseCommand
from countbeans.services.add_expense import add_expense, resolve_participants
from countbeans.services.repositories import (
    ExpenseRepository,
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)


class _SessionUoW:
    def __init__(self, session: AsyncSession) -> None:
        self.expenses = ExpenseRepository(session)
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)
        self.group_members = GroupMemberRepository(session)


def _make_user(**kw: object) -> User:
    return User(id=uuid_utils.uuid7(), **kw)


def _make_group(telegram_chat_id: int = 1) -> Group:
    return Group(
        id=uuid_utils.uuid7(), telegram_chat_id=telegram_chat_id, default_currency="SGD"
    )


async def _seed(session: AsyncSession, n: int = 2) -> tuple[Group, list[User]]:
    group = _make_group()
    users = [_make_user() for _ in range(n)]
    session.add(group)
    session.add_all(users)
    await session.flush()
    session.add_all([GroupMember(group_id=group.id, user_id=u.id) for u in users])
    await session.flush()
    return group, users


def _cmd(
    group: Group,
    payer: User,
    participants: list[User],
    amount_cents: int = 1000,
) -> AddExpenseCommand:
    return AddExpenseCommand(
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=amount_cents,
        currency="SGD",
        participants=[u.id for u in participants],
        created_by=payer.id,
    )


async def test_add_expense_records_rows(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session)
    uow = _SessionUoW(session)

    result = await add_expense(uow, _cmd(group, alice, [alice, bob]))  # type: ignore[arg-type]

    expense = (
        await session.execute(select(Expense).where(Expense.id == result.expense_id))
    ).scalar_one()
    shares = (
        (
            await session.execute(
                select(ExpenseShare).where(ExpenseShare.expense_id == result.expense_id)
            )
        )
        .scalars()
        .all()
    )

    assert expense.amount_cents == 1000
    assert expense.payer_id == alice.id
    assert len(shares) == 2


async def test_shares_sum_to_amount(session: AsyncSession) -> None:
    group, users = await _seed(session, n=3)
    uow = _SessionUoW(session)

    result = await add_expense(uow, _cmd(group, users[0], users, amount_cents=100))  # type: ignore[arg-type]

    assert sum(result.shares.values()) == 100


async def test_equal_split_three_differ_by_at_most_one(session: AsyncSession) -> None:
    group, users = await _seed(session, n=3)
    uow = _SessionUoW(session)

    result = await add_expense(uow, _cmd(group, users[0], users, amount_cents=10))  # type: ignore[arg-type]

    values = sorted(result.shares.values())
    assert values[-1] - values[0] <= 1


async def test_expense_id_is_uuid(session: AsyncSession) -> None:
    group, users = await _seed(session)
    uow = _SessionUoW(session)

    result = await add_expense(uow, _cmd(group, users[0], users))  # type: ignore[arg-type]

    assert isinstance(result.expense_id, uuid.UUID)


async def test_zero_amount_rejected(session: AsyncSession) -> None:
    group, users = await _seed(session)
    with pytest.raises(ValidationError, match="amount_cents"):
        _cmd(group, users[0], users, amount_cents=0)


async def test_balance_sum_to_zero(session: AsyncSession) -> None:
    """After an equal-split expense, net balances (derived manually) sum to zero."""
    group, (alice, bob) = await _seed(session)
    uow = _SessionUoW(session)

    result = await add_expense(uow, _cmd(group, alice, [alice, bob], amount_cents=100))  # type: ignore[arg-type]

    alice_balance = 100 - result.shares[alice.id]
    bob_balance = -result.shares[bob.id]
    assert alice_balance + bob_balance == 0


async def _seed_named(
    session: AsyncSession, usernames: list[str]
) -> tuple[Group, list[User]]:
    group = _make_group()
    users = [
        _make_user(username=name, telegram_user_id=1000 + i)
        for i, name in enumerate(usernames)
    ]
    session.add(group)
    session.add_all(users)
    await session.flush()
    session.add_all([GroupMember(group_id=group.id, user_id=u.id) for u in users])
    await session.flush()
    return group, users


async def test_resolve_participants_no_mentions_splits_everyone(
    session: AsyncSession,
) -> None:
    group, (alice, bob) = await _seed(session)
    uow = _SessionUoW(session)

    parts = await resolve_participants(uow, group.id, alice.id, [])  # type: ignore[arg-type]
    assert {p.user_id for p in parts} == {alice.id, bob.id}


async def test_resolve_participants_all_keyword_splits_everyone(
    session: AsyncSession,
) -> None:
    group, (alice, bob) = await _seed(session)
    uow = _SessionUoW(session)

    parts = await resolve_participants(uow, group.id, alice.id, ["all"])  # type: ignore[arg-type]
    assert {p.user_id for p in parts} == {alice.id, bob.id}


async def test_resolve_participants_named_excludes_payer(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed_named(session, ["alice", "bob"])
    uow = _SessionUoW(session)

    # alice (the payer) names only bob → only bob participates; alice is excluded.
    parts = await resolve_participants(uow, group.id, alice.id, ["bob"])  # type: ignore[arg-type]
    assert [p.user_id for p in parts] == [bob.id]


async def test_resolve_participants_self_mention_includes_payer(
    session: AsyncSession,
) -> None:
    group, (alice, bob) = await _seed_named(session, ["alice", "bob"])
    uow = _SessionUoW(session)

    # Mentioning yourself opts you back in.
    parts = await resolve_participants(uow, group.id, alice.id, ["alice", "bob"])  # type: ignore[arg-type]
    assert {p.user_id for p in parts} == {alice.id, bob.id}


async def test_resolve_participants_unknown_handle_becomes_placeholder(
    session: AsyncSession,
) -> None:
    group, (alice,) = await _seed_named(session, ["alice"])
    uow = _SessionUoW(session)

    parts = await resolve_participants(uow, group.id, alice.id, ["charlie"])  # type: ignore[arg-type]
    assert len(parts) == 1
    assert parts[0].username == "charlie"
    assert parts[0].is_pending is True  # placeholder: telegram_user_id IS NULL

    # A pending placeholder row was actually created.
    placeholder = (
        await session.execute(select(User).where(User.username == "charlie"))
    ).scalar_one()
    assert placeholder.telegram_user_id is None


async def test_resolve_participants_deduplicates(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed_named(session, ["alice", "bob"])
    uow = _SessionUoW(session)

    parts = await resolve_participants(uow, group.id, alice.id, ["bob", "bob"])  # type: ignore[arg-type]
    assert [p.user_id for p in parts] == [bob.id]
