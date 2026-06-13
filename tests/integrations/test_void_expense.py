"""Integration tests for the void services (list_void_candidates + void_entry).

Exercises the SQL/outcome boundary directly (no aiogram): the merged
expense+settlement browse list, the per-kind permission rule, the by-id pinning
(group mismatch, already-voided), and that a voided row — of either kind —
drops out of the derived balances. Each test rolls back via the session fixture.
"""

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
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
from countbeans.dto.results import VoidOutcome
from countbeans.services.repositories import (
    BalanceRepository,
    ExpenseRepository,
    GroupMemberRepository,
    GroupRepository,
    SettlementRepository,
    UserRepository,
)
from countbeans.services.void_expense import list_void_candidates, void_entry


class _SessionUoW:
    """Wraps an already-open AsyncSession — flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.expenses = ExpenseRepository(session)
        self.settlements = SettlementRepository(session)
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
    share_holder: User | None = None,
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
        ExpenseShare(
            expense_id=expense.id,
            user_id=(share_holder or payer).id,
            share_cents=amount_cents,
        )
    )
    await session.flush()
    return expense


async def _settle(
    session: AsyncSession,
    group: Group,
    from_user: User,
    to_user: User,
    *,
    amount_cents: int = 1000,
) -> Settlement:
    result = await SettlementRepository(session).add(
        group_id=group.id,
        event_id=None,
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        amount_cents=amount_cents,
        currency="SGD",
    )
    return (
        await session.execute(
            select(Settlement).where(Settlement.id == result.settlement_id)
        )
    ).scalar_one()


async def test_preview_then_void_marks_row_and_clears_balance(
    session: AsyncSession,
) -> None:
    group, alice, bob = await _seed(session)
    # alice fronts SGD 10; bob holds the whole share → bob owes alice SGD 10.
    expense = await _add(session, group, alice, amount_cents=1000, share_holder=bob)
    uow = _SessionUoW(session)
    assert await uow.balances.compute_for_group(group.id)  # there's an outstanding debt

    candidates = await list_void_candidates(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert len(candidates) == 1
    preview = candidates[0]
    assert preview.kind == "expense"
    assert preview.entry_id == expense.id
    assert preview.amount_cents == 1000
    assert preview.description == "dinner"
    # The browse is a pure read — nothing voided yet.
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is None

    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        "expense",
        preview.entry_id,
        allow_any=False,
    )

    assert result.outcome is VoidOutcome.VOIDED
    assert result.kind == "expense"
    assert result.amount_cents == 1000
    assert result.description == "dinner"
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_at is not None
    assert fresh.voided_by == alice.id
    # The voided expense drops out of the derived balances.
    assert await uow.balances.compute_for_group(group.id) == {}


async def test_candidates_empty_when_scope_empty(session: AsyncSession) -> None:
    group, _, _ = await _seed(session)
    uow = _SessionUoW(session)

    assert await list_void_candidates(uow, group.id, event_id=None) == []  # type: ignore[arg-type]


async def test_candidates_merge_expenses_and_settlements_newest_first(
    session: AsyncSession,
) -> None:
    group, alice, bob = await _seed(session)
    expense = await _add(session, group, alice, description="dinner")
    settlement = await _settle(session, group, bob, alice, amount_cents=500)
    uow = _SessionUoW(session)

    candidates = await list_void_candidates(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert [(c.kind, c.entry_id) for c in candidates] == [
        ("settlement", settlement.id),
        ("expense", expense.id),
    ]


async def test_void_settlement_restores_the_debt(session: AsyncSession) -> None:
    """Voiding a settlement re-opens the debt it had cleared — by either party."""
    group, alice, bob = await _seed(session)
    # bob owes alice SGD 10, then settles it → balances are flat.
    await _add(session, group, alice, amount_cents=1000, share_holder=bob)
    settlement = await _settle(session, group, bob, alice, amount_cents=1000)
    uow = _SessionUoW(session)
    assert await uow.balances.compute_for_group(group.id) == {}

    # The *recipient* (alice) may void it too — both standings moved.
    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        "settlement",
        settlement.id,
        allow_any=False,
    )

    assert result.outcome is VoidOutcome.VOIDED
    assert result.kind == "settlement"
    assert result.actor_id == bob.id
    assert result.counterparty_id == alice.id
    fresh = (
        await session.execute(select(Settlement).where(Settlement.id == settlement.id))
    ).scalar_one()
    assert fresh.voided_at is not None
    assert fresh.voided_by == alice.id
    # The debt is outstanding again.
    assert await uow.balances.compute_for_group(group.id) != {}


async def test_void_settlement_forbidden_for_third_party(
    session: AsyncSession,
) -> None:
    group, alice, bob = await _seed(session)
    carol = _make_user()
    session.add(carol)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=carol.id))
    await session.flush()
    settlement = await _settle(session, group, bob, alice)
    uow = _SessionUoW(session)

    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        carol.id,
        "settlement",
        settlement.id,
        allow_any=False,
    )
    assert result.outcome is VoidOutcome.FORBIDDEN
    assert result.actor_id == bob.id and result.counterparty_id == alice.id
    fresh = (
        await session.execute(
            select(Settlement.voided_at).where(Settlement.id == settlement.id)
        )
    ).scalar_one()
    assert fresh is None


async def test_void_expense_forbidden_for_non_owner(session: AsyncSession) -> None:
    group, alice, bob = await _seed(session)
    # bob both pays and records; alice tries to void without admin rights.
    expense = await _add(session, group, bob)
    uow = _SessionUoW(session)

    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        "expense",
        expense.id,
        allow_any=False,
    )
    assert result.outcome is VoidOutcome.FORBIDDEN
    assert result.actor_id == bob.id
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

    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        "expense",
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
    result = await void_entry(
        uow,  # type: ignore[arg-type]
        group.id,
        alice.id,
        "expense",
        expense.id,
        allow_any=True,
    )
    assert result.outcome is VoidOutcome.VOIDED
    fresh = (
        await session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh.voided_by == alice.id


async def test_candidates_skip_already_voided(session: AsyncSession) -> None:
    """The browse ignores voided rows: voiding one surfaces the next."""
    group, alice, _ = await _seed(session)
    first = await _add(session, group, alice, description="first")
    second = await _add(session, group, alice, description="second")
    uow = _SessionUoW(session)

    candidates = await list_void_candidates(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert [c.entry_id for c in candidates] == [second.id, first.id]

    r1 = await void_entry(uow, group.id, alice.id, "expense", second.id, allow_any=False)  # type: ignore[arg-type]
    assert r1.outcome is VoidOutcome.VOIDED

    candidates = await list_void_candidates(uow, group.id, event_id=None)  # type: ignore[arg-type]
    assert [c.entry_id for c in candidates] == [first.id]


async def test_void_by_id_nothing_when_already_voided(session: AsyncSession) -> None:
    """A double-confirm (or a void that landed meanwhile) is a no-op, not a crash
    and not a second write."""
    group, alice, _ = await _seed(session)
    expense = await _add(session, group, alice)
    uow = _SessionUoW(session)

    r1 = await void_entry(uow, group.id, alice.id, "expense", expense.id, allow_any=False)  # type: ignore[arg-type]
    assert r1.outcome is VoidOutcome.VOIDED
    voided_at = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()

    r2 = await void_entry(uow, group.id, alice.id, "expense", expense.id, allow_any=False)  # type: ignore[arg-type]
    assert r2.outcome is VoidOutcome.NOTHING
    # The original stamp survives untouched.
    fresh = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh == voided_at


async def test_void_by_id_rejects_foreign_group(session: AsyncSession) -> None:
    """A crafted confirm carrying another group's entry id writes nothing —
    the void is pinned to the group the button lives in."""
    group, alice, _ = await _seed(session)
    other = _make_group(telegram_chat_id=2)
    session.add(other)
    await session.flush()
    expense = await _add(session, group, alice)
    uow = _SessionUoW(session)

    result = await void_entry(
        uow,  # type: ignore[arg-type]
        other.id,  # confirm arrives from the other group's chat
        alice.id,
        "expense",
        expense.id,
        allow_any=True,
    )
    assert result.outcome is VoidOutcome.NOTHING
    fresh = (
        await session.execute(select(Expense.voided_at).where(Expense.id == expense.id))
    ).scalar_one()
    assert fresh is None
