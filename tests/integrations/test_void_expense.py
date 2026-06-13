"""Integration tests for the void services (preview_last_expense + void_expense_by_id).

Exercises the SQL/outcome boundary directly (no aiogram): scope selection at
preview, the owner/creator/admin permission rule, the by-id pinning (group
mismatch, already-voided), and that a voided row drops out of the derived
balances. Each test rolls back via the session fixture.
"""

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, User
from countbeans.dto.results import VoidOutcome
from countbeans.services.repositories import (
    BalanceRepository,
    ExpenseRepository,
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)
from countbeans.services.void_expense import preview_last_expense, void_expense_by_id


class _SessionUoW:
    """Wraps an already-open AsyncSession — flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.expenses = ExpenseRepository(session)
        self.balances = BalanceRepository(session)
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)
        self.group_members = GroupMemberRepository(session)


def _make_user(**kw: object) -> User:
    return User(id=uuid_utils.uuid7(), **kw)


def _make_group(telegram_chat_id: int = 1) -> Group:
    return Group(
        id=uuid_utils.uuid7(), telegram_chat_id=telegram_chat_id, default_currency="SGD"
    )


async def _seed(session: AsyncSession) -> tuple[Group, User, User]:
    group = _make_group()
    alice, bob = _make_user(), _make_user()
    session.add(group)
    session.add_all([alice, bob])
    await session.flush()
    session.add_all(
        [GroupMember(group_id=group.id, user_id=u.id) for u in (alice, bob)]
    )
    await session.flush()
    return group, alice, bob


async def _add(
    session: AsyncSession,
    group: Group,
    payer: User,
    *,
    amount_cents: int = 1000,
    description: str | None = "dinner",
    event_id=None,
    created_by: User | None = None,
) -> Expense:
    expense = Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        event_id=event_id,
        payer_id=payer.id,
        amount_cents=amount_cents,
        currency="SGD",
        description=description,
        created_by=(created_by or payer).id,
    )
    session.add(expense)
    await session.flush()
    session.add(
        ExpenseShare(expense_id=expense.id, user_id=payer.id, share_cents=amount_cents)
    )
    await session.flush()
    return expense


async def test_preview_then_void_marks_row_and_clears_balance(
    session: AsyncSession,
) -> None:
    group, alice, bob = await _seed(session)
    # alice fronts SGD 10; bob holds the whole share → bob owes alice SGD 10.
    expense = await _add(session, group, alice, amount_cents=1000)
    await session.execute(
        update(ExpenseShare)
        .where(ExpenseShare.expense_id == expense.id)
        .values(user_id=bob.id)
    )
    await session.flush()
    uow = _SessionUoW(session)
    assert await uow.balances.compute_for_group(group.id)  # there's an outstanding debt

    preview = await preview_last_expense(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert preview is not None
    assert preview.expense_id == expense.id
    assert preview.amount_cents == 1000
    assert preview.description == "dinner"
    # The preview is a pure read — nothing voided yet.
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is None

    result = await void_expense_by_id(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        preview.expense_id,
        allow_any=False,
    )

    assert result.outcome is VoidOutcome.VOIDED
    assert result.amount_cents == 1000
    assert result.description == "dinner"
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is not None
    assert fresh.voided_by == alice.id
    # The voided expense drops out of the derived balances.
    assert await uow.balances.compute_for_group(group.id) == {}


async def test_preview_none_when_scope_empty(session: AsyncSession) -> None:
    group, _, _ = await _seed(session)
    uow = _SessionUoW(session)

    assert await preview_last_expense(uow, group.id, event_id=None) is None  # type: ignore[arg-type]


async def test_void_forbidden_for_non_owner(session: AsyncSession) -> None:
    group, alice, bob = await _seed(session)
    # bob both pays and records; alice tries to void without admin rights.
    expense = await _add(session, group, bob)
    uow = _SessionUoW(session)

    result = await void_expense_by_id(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        expense.id,
        allow_any=False,
    )
    assert result.outcome is VoidOutcome.FORBIDDEN
    assert result.payer_id == bob.id
    assert result.created_by == bob.id
    # Untouched.
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is None


async def test_void_allowed_for_creator_not_payer(session: AsyncSession) -> None:
    """Whoever *recorded* the expense (created_by) may void it, even if someone
    else is the payer."""
    group, alice, bob = await _seed(session)
    # bob is payer, alice recorded it (created_by=alice).
    expense = await _add(session, group, bob, created_by=alice)
    uow = _SessionUoW(session)

    result = await void_expense_by_id(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        expense.id,
        allow_any=False,
    )
    assert result.outcome is VoidOutcome.VOIDED
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is not None


async def test_void_allow_any_overrides_ownership(session: AsyncSession) -> None:
    group, alice, bob = await _seed(session)
    expense = await _add(session, group, bob)  # bob owns it
    uow = _SessionUoW(session)

    # alice isn't owner/creator, but allow_any (admin) lets her void.
    result = await void_expense_by_id(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        expense.id,
        allow_any=True,
    )
    assert result.outcome is VoidOutcome.VOIDED
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_by == alice.id


async def test_preview_targets_most_recent_in_scope(session: AsyncSession) -> None:
    group, alice, _ = await _seed(session)
    await _add(session, group, alice, amount_cents=1000, description="first")
    second = await _add(session, group, alice, amount_cents=2000, description="second")
    uow = _SessionUoW(session)

    preview = await preview_last_expense(uow, group.id, event_id=None)  # type: ignore[arg-type]
    # The newest (by created_at, UUID7-ordered insert) is the one offered.
    assert preview is not None
    assert preview.expense_id == second.id
    assert preview.description == "second"


async def test_preview_skips_already_voided(session: AsyncSession) -> None:
    """The selection ignores voided rows: a second /void offers the prior one."""
    group, alice, _ = await _seed(session)
    first = await _add(session, group, alice, description="first")
    second = await _add(session, group, alice, description="second")
    uow = _SessionUoW(session)

    p1 = await preview_last_expense(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert p1 is not None and p1.expense_id == second.id
    r1 = await void_expense_by_id(uow, group.id, alice.id, p1.expense_id, allow_any=False)  # type: ignore[arg-type]
    assert r1.outcome is VoidOutcome.VOIDED

    p2 = await preview_last_expense(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert p2 is not None and p2.expense_id == first.id
    r2 = await void_expense_by_id(uow, group.id, alice.id, p2.expense_id, allow_any=False)  # type: ignore[arg-type]
    assert r2.outcome is VoidOutcome.VOIDED

    assert await preview_last_expense(uow, group.id, event_id=None) is None  # type: ignore[arg-type]


async def test_void_by_id_nothing_when_already_voided(session: AsyncSession) -> None:
    """A double-confirm (or a void that landed meanwhile) is a no-op, not a crash
    and not a second write."""
    group, alice, _ = await _seed(session)
    expense = await _add(session, group, alice)
    uow = _SessionUoW(session)

    r1 = await void_expense_by_id(uow, group.id, alice.id, expense.id, allow_any=False)  # type: ignore[arg-type]
    assert r1.outcome is VoidOutcome.VOIDED
    voided_at = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()

    r2 = await void_expense_by_id(uow, group.id, alice.id, expense.id, allow_any=False)  # type: ignore[arg-type]
    assert r2.outcome is VoidOutcome.NOTHING
    # The original stamp survives untouched.
    fresh = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh == voided_at


async def test_void_by_id_rejects_foreign_group(session: AsyncSession) -> None:
    """A crafted confirm carrying another group's expense id writes nothing —
    the void is pinned to the group the button lives in."""
    group, alice, _ = await _seed(session)
    other = _make_group(telegram_chat_id=2)
    session.add(other)
    await session.flush()
    expense = await _add(session, group, alice)
    uow = _SessionUoW(session)

    result = await void_expense_by_id(
        uow,  # type: ignore[arg-type]
        other.id,  # confirm arrives from the other group's chat
        alice.id,
        expense.id,
        allow_any=True,
    )
    assert result.outcome is VoidOutcome.NOTHING
    fresh = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh is None
