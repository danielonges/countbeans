"""Integration tests for the statement service — paginates the merged ledger.

Exercises StatementRepository + get_statement_page against Postgres: scope
filtering (group vs. one user), newest-first ordering across the
expense/settlement merge, page windowing + clamping, and the per-entry fields
(usernames, participant count, voided flag). Each test rolls back via the
session fixture.
"""

from datetime import datetime, timedelta, timezone

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import (
    Expense,
    ExpenseShare,
    Group,
    GroupMember,
    Settlement,
    User,
)
from countbeans.services.repositories import (
    BalanceRepository,
    GroupRepository,
    StatementRepository,
    UserRepository,
)
from countbeans.services.statements import get_statement_page


class _SessionUoW:
    def __init__(self, session: AsyncSession) -> None:
        self.ledger = StatementRepository(session)
        self.balances = BalanceRepository(session)
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)


_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


async def _seed(
    session: AsyncSession, usernames: list[str]
) -> tuple[Group, list[User]]:
    group = Group(id=uuid_utils.uuid7(), telegram_chat_id=1, default_currency="SGD")
    users = [
        User(id=uuid_utils.uuid7(), telegram_user_id=1000 + i, username=name)
        for i, name in enumerate(usernames)
    ]
    session.add(group)
    session.add_all(users)
    await session.flush()
    session.add_all([GroupMember(group_id=group.id, user_id=u.id) for u in users])
    await session.flush()
    return group, users


async def _add_expense(
    session: AsyncSession,
    group: Group,
    payer: User,
    amount: int,
    shares: list[tuple[User, int]],
    *,
    created_at: datetime,
    description: str | None = None,
    voided: bool = False,
) -> Expense:
    exp = Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=amount,
        currency="SGD",
        description=description,
        created_by=payer.id,
        created_at=created_at,
        voided_at=created_at if voided else None,
    )
    session.add(exp)
    await session.flush()
    session.add_all(
        [
            ExpenseShare(expense_id=exp.id, user_id=u.id, share_cents=c)
            for u, c in shares
        ]
    )
    await session.flush()
    return exp


async def _add_settlement(
    session: AsyncSession,
    group: Group,
    frm: User,
    to: User,
    amount: int,
    *,
    created_at: datetime,
) -> Settlement:
    s = Settlement(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        from_user_id=frm.id,
        to_user_id=to.id,
        amount_cents=amount,
        currency="SGD",
        created_at=created_at,
    )
    session.add(s)
    await session.flush()
    return s


async def test_empty_ledger(session: AsyncSession) -> None:
    group, _ = await _seed(session, ["alice", "bob"])
    uow = _SessionUoW(session)
    page = await get_statement_page(uow, group.id)  # type: ignore[arg-type]
    assert page.total == 0
    assert page.entries == []


async def test_group_scope_newest_first_with_fields(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session, ["alice", "bob"])
    await _add_expense(
        session,
        group,
        alice,
        100,
        [(alice, 50), (bob, 50)],
        created_at=_at(1),
        description="Dinner",
    )
    await _add_settlement(session, group, bob, alice, 50, created_at=_at(2))

    uow = _SessionUoW(session)
    page = await get_statement_page(uow, group.id)  # type: ignore[arg-type]

    assert page.total == 2
    # Newest first: the settlement (t=2) precedes the expense (t=1).
    settle, expense = page.entries
    assert settle.kind == "settlement"
    assert settle.actor_username == "bob"
    assert settle.counterparty_username == "alice"
    assert settle.amount_cents == 50

    assert expense.kind == "expense"
    assert expense.description == "Dinner"
    assert expense.actor_username == "alice"
    assert expense.counterparty_username is None
    assert expense.participant_count == 2
    assert expense.voided is False


async def test_pagination_windows_and_clamps(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session, ["alice", "bob"])
    for i in range(1, 6):  # five expenses at t=1..5
        await _add_expense(
            session,
            group,
            alice,
            10 * i,
            [(alice, 10 * i)],
            created_at=_at(i),
            description=f"e{i}",
        )

    uow = _SessionUoW(session)

    p0 = await get_statement_page(uow, group.id, page=0, page_size=2)  # type: ignore[arg-type]
    assert p0.total == 5
    assert [e.description for e in p0.entries] == ["e5", "e4"]  # newest first

    p1 = await get_statement_page(uow, group.id, page=1, page_size=2)  # type: ignore[arg-type]
    assert [e.description for e in p1.entries] == ["e3", "e2"]

    p2 = await get_statement_page(uow, group.id, page=2, page_size=2)  # type: ignore[arg-type]
    assert [e.description for e in p2.entries] == ["e1"]

    # A stale Next tap past the end clamps to the last page rather than erroring.
    over = await get_statement_page(uow, group.id, page=9, page_size=2)  # type: ignore[arg-type]
    assert over.page == 2
    assert [e.description for e in over.entries] == ["e1"]


async def test_user_scope_only_includes_involvement(session: AsyncSession) -> None:
    group, (alice, bob, carol) = await _seed(session, ["alice", "bob", "carol"])
    # e1: alice pays, alice+bob share (carol uninvolved)
    await _add_expense(
        session,
        group,
        alice,
        100,
        [(alice, 50), (bob, 50)],
        created_at=_at(1),
        description="e1",
    )
    # e2: carol pays, carol-only share (alice + bob uninvolved)
    await _add_expense(
        session,
        group,
        carol,
        30,
        [(carol, 30)],
        created_at=_at(2),
        description="e2",
    )
    # s: bob pays carol (alice uninvolved)
    await _add_settlement(session, group, bob, carol, 20, created_at=_at(3))

    uow = _SessionUoW(session)

    alice_page = await get_statement_page(uow, group.id, user_id=alice.id)  # type: ignore[arg-type]
    assert [e.description for e in alice_page.entries] == ["e1"]  # payer of e1 only

    bob_page = await get_statement_page(uow, group.id, user_id=bob.id)  # type: ignore[arg-type]
    # bob: a share in e1, and the sender of the settlement → 2 entries, newest first.
    assert bob_page.total == 2
    assert bob_page.entries[0].kind == "settlement"
    assert bob_page.entries[1].description == "e1"

    carol_page = await get_statement_page(uow, group.id, user_id=carol.id)  # type: ignore[arg-type]
    # carol: payer of e2 and recipient of the settlement → 2 entries.
    assert carol_page.total == 2
    assert {e.kind for e in carol_page.entries} == {"expense", "settlement"}


async def test_voided_expense_included_and_flagged(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session, ["alice", "bob"])
    await _add_expense(
        session,
        group,
        alice,
        100,
        [(alice, 50), (bob, 50)],
        created_at=_at(1),
        description="oops",
        voided=True,
    )

    uow = _SessionUoW(session)
    page = await get_statement_page(uow, group.id)  # type: ignore[arg-type]
    # Unlike balance derivation, a statement keeps voided rows — flagged.
    assert page.total == 1
    assert page.entries[0].voided is True
