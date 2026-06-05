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

from countbeans.db.models import (
    Expense,
    ExpenseShare,
    Group,
    GroupMember,
    Settlement,
    User,
)
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.repositories import BalanceRepository, SettlementRepository
from countbeans.services.settlement import owed_by_currency, settle_all, settle_up


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

    result = await settle_up(uow, _cmd(group, payer, payee), simplify_debts=True)  # type: ignore[arg-type]

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

    result = await settle_up(uow, cmd, simplify_debts=True)  # type: ignore[arg-type]

    assert result.from_user_id == payer.id
    assert result.to_user_id == payee.id
    assert result.amount_cents == 2500
    assert result.currency == "SGD"
    assert result.event_id is None
    assert isinstance(result.settlement_id, uuid.UUID)


async def test_settle_up_same_user_rejected(session: AsyncSession) -> None:
    group, (payer, _) = await _seed(session)
    with pytest.raises(
        ValidationError, match="from_user_id and to_user_id must be different"
    ):
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
    r1 = await settle_up(uow, _cmd(group, bob, alice, amount_cents=1000), simplify_debts=True)  # type: ignore[arg-type]
    # bob still owes alice 500 more (share was 1500, paid 1000) — the cap
    # re-derives from the post-r1 balances, so this second payment is allowed.
    r2 = await settle_up(uow, _cmd(group, bob, alice, amount_cents=500), simplify_debts=True)  # type: ignore[arg-type]

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
    """A creditor (owes nothing) has no suggested outgoing payment, so settling
    is rejected."""
    group, (alice, bob) = await _seed(session)
    # alice paid — alice has positive balance, bob is the debtor
    await _add_expense(session, group, alice, [alice, bob], amount_cents=2000)
    uow = _SessionUoW(session)

    # alice tries to pay bob even though alice is owed money
    with pytest.raises(ValueError, match="debt runs in that direction"):
        await settle_up(uow, _cmd(group, alice, bob), simplify_debts=True)  # type: ignore[arg-type]


async def test_settle_up_recipient_not_owed_rejected(session: AsyncSession) -> None:
    """Paying someone the suggested settlement never routes a payment to (here a
    zero-balance member) is rejected."""
    group, (alice, bob, charlie) = await _seed(session, n_users=3)
    # alice paid for alice + bob; charlie has no expenses
    await _add_expense(session, group, alice, [alice, bob], amount_cents=2000)
    uow = _SessionUoW(session)

    # bob owes alice, but tries to pay charlie (who is owed nothing)
    with pytest.raises(ValueError, match="debt runs in that direction"):
        await settle_up(uow, _cmd(group, bob, charlie), simplify_debts=True)  # type: ignore[arg-type]


async def test_settle_up_overpayment_rejected(session: AsyncSession) -> None:
    """An explicit amount exceeding the suggested transfer is rejected, so a
    balance can never be flipped by overpaying."""
    group, (payer, payee) = await _seed(session)
    # payee paid 2000 split evenly → payer owes exactly 1000.
    await _add_expense(session, group, payee, [payer, payee], amount_cents=2000)
    uow = _SessionUoW(session)

    with pytest.raises(ValueError, match="is owed in that direction"):
        await settle_up(uow, _cmd(group, payer, payee, amount_cents=1500), simplify_debts=True)  # type: ignore[arg-type]

    # No settlement row was written.
    rows = (await session.execute(select(Settlement))).scalars().all()
    assert rows == []


async def _expense_with_shares(
    session: AsyncSession, group: Group, payer: User, shares: list[tuple[User, int]]
) -> Expense:
    """Seed an expense with explicit per-user shares (payer may be excluded)."""
    expense = Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=sum(c for _, c in shares),
        currency="SGD",
        description="test expense",
        created_by=payer.id,
    )
    session.add(expense)
    await session.flush()
    session.add_all(
        [
            ExpenseShare(expense_id=expense.id, user_id=u.id, share_cents=c)
            for u, c in shares
        ]
    )
    await session.flush()
    return expense


async def test_settle_up_caps_at_suggested_not_net_debt(session: AsyncSession) -> None:
    """The motivating regression: when a caller's net debt is spread across
    several creditors, /settleup @alice is capped at what's suggested to *Alice*,
    not the caller's whole net debt."""
    group, (caller, alice, carol) = await _seed(session, n_users=3)
    # caller owes alice 3000 and carol 2000 → net debt 5000, but only 3000 to alice.
    await _expense_with_shares(session, group, alice, [(caller, 3000)])
    await _expense_with_shares(session, group, carol, [(caller, 2000)])
    uow = _SessionUoW(session)

    # Auto-fill resolves to the alice-specific 3000, not the 5000 net debt.
    owed = await owed_by_currency(uow, group.id, caller.id, alice.id, simplify_debts=True)  # type: ignore[arg-type]
    assert owed == {"SGD": 3000}

    # Trying to dump the whole 5000 net debt on alice is rejected.
    with pytest.raises(ValueError, match="is owed in that direction"):
        await settle_up(uow, _cmd(group, caller, alice, amount_cents=5000), simplify_debts=True)  # type: ignore[arg-type]

    # Settling exactly what's owed to alice succeeds.
    result = await settle_up(uow, _cmd(group, caller, alice, amount_cents=3000), simplify_debts=True)  # type: ignore[arg-type]
    assert result.amount_cents == 3000


async def test_settle_all_zeroes_the_group(session: AsyncSession) -> None:
    """/settleup @all records every suggested transfer, driving all balances to
    zero in one transaction."""
    group, (caller, alice, carol) = await _seed(session, n_users=3)
    await _expense_with_shares(session, group, alice, [(caller, 3000)])
    await _expense_with_shares(session, group, carol, [(caller, 2000)])
    uow = _SessionUoW(session)

    results = await settle_all(uow, group.id, simplify_debts=True)  # type: ignore[arg-type]

    # Two suggested transfers: caller→alice 3000, caller→carol 2000.
    assert len(results) == 2
    assert {(r.from_user_id, r.to_user_id, r.amount_cents) for r in results} == {
        (caller.id, alice.id, 3000),
        (caller.id, carol.id, 2000),
    }

    # Every balance is now zero — the group is fully settled.
    remaining = await uow.balances.compute_for_group(group.id)  # type: ignore[arg-type]
    assert remaining == {}


async def test_settle_all_on_settled_group_records_nothing(
    session: AsyncSession,
) -> None:
    group, _ = await _seed(session, n_users=2)  # no expenses → nothing outstanding
    uow = _SessionUoW(session)

    results = await settle_all(uow, group.id, simplify_debts=True)  # type: ignore[arg-type]
    assert results == []
    rows = (await session.execute(select(Settlement))).scalars().all()
    assert rows == []
