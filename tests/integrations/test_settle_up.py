"""Integration tests for the settle_up service function.

Uses an ephemeral Postgres container (Testcontainers via conftest.py).
Each test function gets a fresh session that rolls back on teardown.

The ``_SessionUoW`` wrapper re-uses the test's already-open session so that
``settle_up`` runs inside the same transaction and can see the seeded rows.
"""
import uuid
from typing import AsyncGenerator

import pytest
import uuid_utils
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Group, GroupMember, Settlement, User
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.repositories import SettlementRepository
from countbeans.services.settlement import settle_up


class _SessionUoW:
    """Wraps an already-open AsyncSession — flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.settlements = SettlementRepository(session)


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
    uow = _SessionUoW(session)

    result = await settle_up(uow, _cmd(group, payer, payee))  # type: ignore[arg-type]

    stmt = select(Settlement).where(Settlement.id == result.settlement_id)
    db_row = (await session.execute(stmt)).scalar_one_or_none()
    assert db_row is not None
    assert db_row.from_user_id == payer.id
    assert db_row.to_user_id == payee.id
    assert db_row.amount_cents == 1000
    assert db_row.currency == "SGD"


async def test_settle_up_returns_correct_result(session: AsyncSession) -> None:
    group, (payer, payee) = await _seed(session)
    uow = _SessionUoW(session)
    cmd = _cmd(group, payer, payee, amount_cents=2500, currency="SGD")

    result = await settle_up(uow, cmd)  # type: ignore[arg-type]

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
    uow = _SessionUoW(session)

    r1 = await settle_up(uow, _cmd(group, alice, bob, amount_cents=1000))  # type: ignore[arg-type]
    r2 = await settle_up(uow, _cmd(group, bob, alice, amount_cents=500))   # type: ignore[arg-type]

    assert r1.settlement_id != r2.settlement_id

    stmt = select(Settlement).where(
        Settlement.id.in_([r1.settlement_id, r2.settlement_id])
    )
    rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 2

    by_id = {row.id: row for row in rows}
    assert by_id[r1.settlement_id].amount_cents == 1000
    assert by_id[r1.settlement_id].from_user_id == alice.id
    assert by_id[r2.settlement_id].amount_cents == 500
    assert by_id[r2.settlement_id].from_user_id == bob.id
