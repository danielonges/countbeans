"""Integration tests for the settle_up service function.

Uses an ephemeral Postgres container (Testcontainers via conftest.py).
Each test function gets a fresh session that rolls back on teardown.

The ``_SessionUoW`` wrapper re-uses the test's already-open session so that
``settle_up`` runs inside the same transaction and can see the seeded rows.
"""
import uuid

import pytest
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, Settlement, User
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.repositories import BalanceRepository, SettlementRepository
from countbeans.services.settlement import settle_up


class _SessionUoW:
    """Wraps an already-open AsyncSession — flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.settlements = SettlementRepository(session)
        self.balances = BalanceRepository(session)


def _make_user(**kwargs: object) -> User:
    return User(id=uuid_utils.uuid7(), **kwargs)


def _make_group(telegram_chat_id: int = 1, **kwargs: object) -> Group:
    return Group(
        id=uuid_utils.uuid7(),
        telegram_chat_id=telegram_chat_id,
        default_currency="SGD",
        **kwargs,
    )


def _make_group_member(group: Group, user: User) -> GroupMember:
    return GroupMember(group_id=group.id, user_id=user.id)


async def _seed(session: AsyncSession, *, n_users: int = 2) -> tuple[Group, list[User]]:
    group = _make_group()
    users = [_make_user() for _ in range(n_users)]
    session.add(group)
    session.add_all(users)
    await session.flush()
    session.add_all([_make_group_member(group, u) for u in users])
    await session.flush()
    return group, users


async def _add_expense(
    session: AsyncSession,
    group: Group,
    payer: User,
    participants: list[User],
    amount_cents: int = 1000,
    currency: str = "SGD",
) -> Expense:
    """Seed an expense split evenly among participants (payer included)."""
    n = len(participants)
    base, extra = divmod(amount_cents, n)
    expense = Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=amount_cents,
        currency=currency,
        description="test expense",
        created_by=payer.id,
    )
    session.add(expense)
    await session.flush()
    shares = [
        ExpenseShare(
            expense_id=expense.id,
            user_id=u.id,
            share_cents=base + (1 if i < extra else 0),
        )
        for i, u in enumerate(participants)
    ]
    session.add_all(shares)
    await session.flush()
    return expense


def _cmd(
    group: Group,
    from_user: User,
    to_user: User,
    amount_cents: int = 1000,
    currency: str = "SGD",
    event_id: uuid.UUID | None = None,
) -> SettleUpCommand:
    return SettleUpCommand(
        group_id=group.id,
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        amount_cents=amount_cents,
        currency=currency,
        event_id=event_id,
        created_by=from_user.id,
    )


async def test_settle_up_records_settlement(session: AsyncSession) -> None:
    group, (payer, payee) = await _seed(session)
    # payee paid, payer owes half
    await _add_expense(session, group, payee, [payer, payee], amount_cents=2000)
    uow = _SessionUoW(session)

    result = await settle_up(uow, _cmd(group, payer, payee))

    stmt = select(Settlement).where(Settlement.id == result.settlement_id)
    db_row = (await session.execute(stmt)).scalar_one_or_none()
    assert db_row is not None
    assert db_row.from_user_id == payer.id
    assert db_row.to_user_id == payee.id
    assert db_row.amount_cents == 1000
    assert db_row.currency == "SGD"


async def test_settle_up_returns_correct_result(session: AsyncSession) -> None:
    group, (payer, payee) = await _seed(session)
    await _add_expense(session, group, payee, [payer, payee], amount_cents=5000)
    uow = _SessionUoW(session)
    cmd = _cmd(group, payer, payee, amount_cents=2500, currency="SGD")

    result = await settle_up(uow, cmd)

    assert result.from_user_id == payer.id
    assert result.to_user_id == payee.id
    assert result.amount_cents == 2500
    assert result.currency == "SGD"
    assert result.event_id is None
    assert isinstance(result.settlement_id, uuid.UUID)


async def test_settle_up_same_user_rejected(session: AsyncSession) -> None:
    group, (payer, _) = await _seed(session)
    with pytest.raises(ValidationError, match="from_user_id and to_user_id must be different"):
        _cmd(group, payer, payer)


async def test_settle_up_zero_amount_rejected(session: AsyncSession) -> None:
    group, (payer, payee) = await _seed(session)
    with pytest.raises(ValidationError, match="amount_cents"):
        _cmd(group, payer, payee, amount_cents=0)


async def test_multiple_settlements_independent(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session)
    # alice paid for both — bob owes alice
    await _add_expense(session, group, alice, [alice, bob], amount_cents=3000)
    uow = _SessionUoW(session)

    # bob pays alice partially
    r1 = await settle_up(uow, _cmd(group, bob, alice, amount_cents=1000))
    # bob still owes alice 500 more (share was 1500, paid 1000)
    r2 = await settle_up(uow, _cmd(group, bob, alice, amount_cents=500))

    assert r1.settlement_id != r2.settlement_id

    stmt = select(Settlement).where(
        Settlement.id.in_([r1.settlement_id, r2.settlement_id])
    )
    rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 2

    by_id = {row.id: row for row in rows}
    assert by_id[r1.settlement_id].amount_cents == 1000
    assert by_id[r1.settlement_id].from_user_id == bob.id
    assert by_id[r2.settlement_id].amount_cents == 500
    assert by_id[r2.settlement_id].from_user_id == bob.id


async def test_settle_up_payer_has_no_debt_rejected(session: AsyncSession) -> None:
    """Settling with someone when you don't owe anyone must be rejected."""
    group, (alice, bob) = await _seed(session)
    # alice paid — alice has positive balance, bob is the debtor
    await _add_expense(session, group, alice, [alice, bob], amount_cents=2000)
    uow = _SessionUoW(session)

    # alice tries to pay bob even though alice is owed money
    with pytest.raises(ValueError, match="don't owe anyone"):
        await settle_up(uow, _cmd(group, alice, bob))


async def test_settle_up_recipient_not_owed_rejected(session: AsyncSession) -> None:
    """Settling with someone who isn't owed any money must be rejected."""
    group, (alice, bob, charlie) = await _seed(session, n_users=3)
    # alice paid for alice + bob; charlie has no expenses
    await _add_expense(session, group, alice, [alice, bob], amount_cents=2000)
    uow = _SessionUoW(session)

    # bob owes alice, but tries to pay charlie (who is owed nothing)
    with pytest.raises(ValueError, match="not owed any"):
        await settle_up(uow, _cmd(group, bob, charlie))
