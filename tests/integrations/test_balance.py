"""Integration tests for the balance service — derives ledger balances from the DB.

Uses an ephemeral Postgres container (Testcontainers via conftest.py).
Each test rolls back via the session fixture.
"""
import uuid
from datetime import datetime, timezone

import uuid_utils
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, Settlement, User
from countbeans.dto.domain import Transfer
from countbeans.services.balance import compute_balances, get_group_summary
from countbeans.services.repositories import BalanceRepository, GroupRepository, UserRepository


class _SessionUoW:
    def __init__(self, session: AsyncSession) -> None:
        self.balances = BalanceRepository(session)
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)


def _settles(
    balances: dict[tuple[uuid.UUID, str], int], transfers: list[Transfer]
) -> bool:
    """True iff applying the transfers drives every balance to zero."""
    net = dict(balances)
    for t in transfers:
        net[(t.from_user_id, t.currency)] = (
            net.get((t.from_user_id, t.currency), 0) + t.amount_cents
        )
        net[(t.to_user_id, t.currency)] = (
            net.get((t.to_user_id, t.currency), 0) - t.amount_cents
        )
    return all(v == 0 for v in net.values())


def _user(**kw: object) -> User:
    # Use a stdlib uuid.UUID (what the DB returns) so ids compare/hash equal to
    # the uuid.UUID fields on DTOs and the SQL-derived balance keys. A raw
    # uuid_utils.UUID hashes differently, breaking dict lookups and set compares.
    return User(id=uuid.UUID(str(uuid_utils.uuid7())), **kw)


async def _seed(session: AsyncSession, n: int = 2) -> tuple[Group, list[User]]:
    # Source the group through the repository so group.id is a real uuid.UUID
    # (RETURNING-processed), exactly as production does — DTOs built from it then
    # validate cleanly, instead of carrying a raw uuid_utils.UUID.
    group = await GroupRepository(session).upsert(telegram_chat_id=1, group_name=None)
    users = [_user() for _ in range(n)]
    session.add_all(users)
    await session.flush()
    session.add_all([GroupMember(group_id=group.id, user_id=u.id) for u in users])
    await session.flush()
    return group, users


def _expense(group: Group, payer: User, amount: int, currency: str = "SGD") -> Expense:
    return Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=amount,
        currency=currency,
        created_by=payer.id,
    )


def _share(expense: Expense, user: User, cents: int) -> ExpenseShare:
    return ExpenseShare(expense_id=expense.id, user_id=user.id, share_cents=cents)


def _settlement(group: Group, frm: User, to: User, amount: int, currency: str = "SGD") -> Settlement:
    return Settlement(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        from_user_id=frm.id,
        to_user_id=to.id,
        amount_cents=amount,
        currency=currency,
    )


async def test_no_data_empty_balances(session: AsyncSession) -> None:
    group, _ = await _seed(session)
    uow = _SessionUoW(session)
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]
    assert raw == {}


async def test_single_expense_balances(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session)
    exp = _expense(group, alice, 100)
    session.add(exp)
    await session.flush()
    session.add_all([_share(exp, alice, 50), _share(exp, bob, 50)])
    await session.flush()

    uow = _SessionUoW(session)
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]

    assert raw[(alice.id, "SGD")] == 50   # +100 fronted − 50 share
    assert raw[(bob.id, "SGD")] == -50
    assert sum(raw.values()) == 0


async def test_settlement_zeroes_balance(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session)
    exp = _expense(group, alice, 100)
    session.add(exp)
    await session.flush()
    session.add_all([_share(exp, alice, 50), _share(exp, bob, 50)])
    s = _settlement(group, bob, alice, 50)
    session.add(s)
    await session.flush()

    uow = _SessionUoW(session)
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]
    assert raw == {}


async def test_sum_to_zero_three_users(session: AsyncSession) -> None:
    group, (alice, bob, carol) = await _seed(session, n=3)
    exp = _expense(group, alice, 90)
    session.add(exp)
    await session.flush()
    session.add_all([_share(exp, alice, 30), _share(exp, bob, 30), _share(exp, carol, 30)])
    await session.flush()

    uow = _SessionUoW(session)
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]
    assert sum(raw.values()) == 0


async def test_get_group_summary(session: AsyncSession) -> None:
    group, (alice, bob, carol) = await _seed(session, n=3)
    exp = _expense(group, alice, 90)
    session.add(exp)
    await session.flush()
    session.add_all([_share(exp, alice, 30), _share(exp, bob, 30), _share(exp, carol, 30)])
    await session.flush()

    uow = _SessionUoW(session)
    summary = await get_group_summary(uow, group.id, simplify_debts=True)  # type: ignore[arg-type]

    assert len(summary.balances) == 3
    alice_bal = next(b for b in summary.balances if b.user_id == alice.id)
    assert alice_bal.balance_cents == 60
    assert len(summary.suggested_transfers) == 2
    assert all(t.to_user_id == alice.id for t in summary.suggested_transfers)


async def test_voided_expense_excluded(session: AsyncSession) -> None:
    group, (alice, bob) = await _seed(session)
    exp = _expense(group, alice, 100)
    exp.voided_at = datetime.now(timezone.utc)
    session.add(exp)
    await session.flush()
    session.add_all([_share(exp, alice, 50), _share(exp, bob, 50)])
    await session.flush()

    uow = _SessionUoW(session)
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]
    assert raw == {}


async def test_group_summary_honors_simplify_toggle(session: AsyncSession) -> None:
    # Net balances: a -1, b -9, c +9, d +1 — a case where simplified needs 2
    # transfers (b→c 9, a→d 1) but raw pairwise needs 3.
    group, (a, b, c, d) = await _seed(session, n=4)
    e1 = _expense(group, c, 10)
    session.add(e1)
    await session.flush()
    session.add_all([_share(e1, a, 1), _share(e1, b, 9)])
    e2 = _expense(group, d, 1)
    session.add(e2)
    await session.flush()
    session.add_all([_share(e2, c, 1)])
    await session.flush()

    uow = _SessionUoW(session)
    on = await get_group_summary(uow, group.id, simplify_debts=True)  # type: ignore[arg-type]
    off = await get_group_summary(uow, group.id, simplify_debts=False)  # type: ignore[arg-type]
    raw = await compute_balances(uow, group.id)  # type: ignore[arg-type]

    # Presentation-only: the per-member balances are identical under either flag.
    assert set(on.balances) == set(off.balances)

    # The simplified view is the reduced, deterministic transfer set.
    assert {(t.from_user_id, t.to_user_id, t.amount_cents) for t in on.suggested_transfers} == {
        (b.id, c.id, 9),
        (a.id, d.id, 1),
    }

    # Both views settle the same balances exactly; simplified is never larger.
    assert _settles(raw, on.suggested_transfers)
    assert _settles(raw, off.suggested_transfers)
    assert len(on.suggested_transfers) <= len(off.suggested_transfers)


async def test_set_simplify_debts_persists(session: AsyncSession) -> None:
    repo = GroupRepository(session)
    group = await repo.upsert(telegram_chat_id=4242, group_name="G")
    assert group.simplify_debts is True  # model default

    await repo.set_simplify_debts(group.id, False)
    await session.refresh(group)  # reload from DB to confirm it persisted
    assert group.simplify_debts is False
