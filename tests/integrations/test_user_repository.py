"""Integration tests for UserRepository onboarding & placeholder claiming.

Needs Postgres — run `docker compose --profile test run --rm test` (or set
TEST_DATABASE_URL); skips otherwise (see conftest.py). Each test gets a fresh
session that rolls back on teardown, so rows never leak.

The load-bearing case is *claiming*: when a previously-mentioned @handle first
interacts, its pending placeholder is turned into the real identity by a
single-row UPDATE, and every ledger row already bound to that surrogate
users.id follows automatically — no fan-out rewrite (CLAUDE.md "Onboarding &
membership").
"""
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, User
from countbeans.services.repositories import UserRepository


async def _count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return result.scalar_one()


def _group() -> Group:
    return Group(id=uuid_utils.uuid7(), telegram_chat_id=1, default_currency="SGD")


async def test_upsert_new_user_inserts_claimed_row(session: AsyncSession) -> None:
    repo = UserRepository(session)
    user = await repo.upsert(telegram_user_id=100, username="alice", first_name="Alice", last_name=None)
    assert user.telegram_user_id == 100
    assert user.username == "alice"
    assert await _count_users(session) == 1


async def test_upsert_existing_user_refreshes_fields_no_duplicate(session: AsyncSession) -> None:
    repo = UserRepository(session)
    first = await repo.upsert(telegram_user_id=100, username="alice", first_name="Alice", last_name=None)
    again = await repo.upsert(telegram_user_id=100, username="alice_renamed", first_name="Al", last_name="Ice")

    assert again.id == first.id
    assert again.username == "alice_renamed"
    assert again.first_name == "Al"
    assert again.last_name == "Ice"
    assert await _count_users(session) == 1


async def test_upsert_claims_pending_placeholder(session: AsyncSession) -> None:
    repo = UserRepository(session)
    placeholder = await repo.resolve_mention("bob")  # mentioned, never seen
    assert placeholder.telegram_user_id is None

    claimed = await repo.upsert(telegram_user_id=200, username="bob", first_name="Bob", last_name=None)

    # Same surrogate row — claiming is an UPDATE, not a new insert.
    assert claimed.id == placeholder.id
    assert claimed.telegram_user_id == 200
    assert claimed.first_name == "Bob"
    assert await _count_users(session) == 1


async def test_claim_preserves_existing_ledger_rows(session: AsyncSession) -> None:
    """A share bound to the placeholder must still reference the same users.id
    after the claim — the no-fan-out-rewrite guarantee."""
    repo = UserRepository(session)
    group = _group()
    payer = await repo.upsert(telegram_user_id=300, username="payer", first_name=None, last_name=None)
    placeholder = await repo.resolve_mention("charlie")
    session.add(group)
    await session.flush()

    expense = Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        amount_cents=100,
        currency="SGD",
        created_by=payer.id,
    )
    session.add(expense)
    await session.flush()
    session.add(ExpenseShare(expense_id=expense.id, user_id=placeholder.id, share_cents=50))
    await session.flush()

    claimed = await repo.upsert(telegram_user_id=400, username="charlie", first_name="Charlie", last_name=None)
    assert claimed.id == placeholder.id

    share = (
        await session.execute(
            select(ExpenseShare).where(ExpenseShare.expense_id == expense.id)
        )
    ).scalar_one()
    assert share.user_id == claimed.id  # the share followed the claim, untouched
    assert claimed.telegram_user_id == 400


async def test_resolve_mention_prefers_claimed_user(session: AsyncSession) -> None:
    repo = UserRepository(session)
    claimed = await repo.upsert(telegram_user_id=500, username="dave", first_name=None, last_name=None)

    resolved = await repo.resolve_mention("dave")  # a handle we already know
    assert resolved.id == claimed.id
    assert await _count_users(session) == 1  # no duplicate placeholder spawned


async def test_resolve_mention_reuses_pending_placeholder(session: AsyncSession) -> None:
    repo = UserRepository(session)
    first = await repo.resolve_mention("erin")
    second = await repo.resolve_mention("erin")
    assert first.id == second.id
    assert await _count_users(session) == 1


async def test_resolve_mention_creates_placeholder_for_unknown_handle(session: AsyncSession) -> None:
    repo = UserRepository(session)
    placeholder = await repo.resolve_mention("frank")
    assert placeholder.telegram_user_id is None
    assert placeholder.username == "frank"
    assert await _count_users(session) == 1


async def test_resolve_mention_claimed_wins_over_placeholder(session: AsyncSession) -> None:
    """Rename/reuse edge: a claimed row and a placeholder share one handle. The
    claimed identity must win — order_by puts telegram_user_id IS NULL last."""
    repo = UserRepository(session)
    placeholder = await repo.resolve_mention("grace")
    claimed = User(id=uuid_utils.uuid7(), telegram_user_id=600, username="grace")
    session.add(claimed)
    await session.flush()

    resolved = await repo.resolve_mention("grace")
    assert resolved.id == claimed.id
    assert resolved.id != placeholder.id


async def test_upsert_without_username_inserts_fresh(session: AsyncSession) -> None:
    repo = UserRepository(session)
    user = await repo.upsert(telegram_user_id=700, username=None, first_name="NoHandle", last_name=None)
    assert user.telegram_user_id == 700
    assert user.username is None
    assert await _count_users(session) == 1
