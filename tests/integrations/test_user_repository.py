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
from countbeans.services.repositories import GroupMemberRepository, UserRepository


async def _count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return result.scalar_one()


def _group() -> Group:
    return Group(id=uuid_utils.uuid7(), telegram_chat_id=1, default_currency="SGD")


async def test_upsert_new_user_inserts_claimed_row(session: AsyncSession) -> None:
    repo = UserRepository(session)
    user = await repo.upsert(
        telegram_user_id=100, username="alice", first_name="Alice", last_name=None
    )
    assert user.telegram_user_id == 100
    assert user.username == "alice"
    assert await _count_users(session) == 1


async def test_upsert_existing_user_refreshes_fields_no_duplicate(
    session: AsyncSession,
) -> None:
    repo = UserRepository(session)
    first = await repo.upsert(
        telegram_user_id=100, username="alice", first_name="Alice", last_name=None
    )
    again = await repo.upsert(
        telegram_user_id=100, username="alice_renamed", first_name="Al", last_name="Ice"
    )

    assert again.id == first.id
    assert again.username == "alice_renamed"
    assert again.first_name == "Al"
    assert again.last_name == "Ice"
    assert await _count_users(session) == 1


async def test_upsert_claims_pending_placeholder(session: AsyncSession) -> None:
    repo = UserRepository(session)
    group = _group()
    session.add(group)
    await session.flush()
    placeholder = await repo.resolve_mention("bob")  # mentioned, never seen
    await GroupMemberRepository(session).ensure_member(group.id, placeholder.id)
    assert placeholder.telegram_user_id is None

    # Claiming is gated on the interaction's group (security review #1): bob's
    # placeholder lives in this group, so the claim proceeds.
    claimed = await repo.upsert(
        telegram_user_id=200,
        username="bob",
        first_name="Bob",
        last_name=None,
        claim_in_group=group.id,
    )

    # Same surrogate row — claiming is an UPDATE, not a new insert.
    assert claimed.id == placeholder.id
    assert claimed.telegram_user_id == 200
    assert claimed.first_name == "Bob"
    assert await _count_users(session) == 1


async def test_claim_allowed_when_placeholder_in_same_group(
    session: AsyncSession,
) -> None:
    """The claim fires when the placeholder is a member of the interaction's group."""
    repo = UserRepository(session)
    group = _group()
    session.add(group)
    await session.flush()
    placeholder = await repo.resolve_mention("alice")
    await GroupMemberRepository(session).ensure_member(group.id, placeholder.id)

    claimed = await repo.upsert(
        telegram_user_id=999,
        username="alice",
        first_name="A",
        last_name=None,
        claim_in_group=group.id,
    )
    assert claimed.id == placeholder.id
    assert claimed.telegram_user_id == 999
    assert await _count_users(session) == 1


async def test_claim_refused_when_placeholder_in_other_group(
    session: AsyncSession,
) -> None:
    """A placeholder created in group A is NOT claimed by a same-username user
    interacting in an unrelated group B — the cross-group hijack the gate blocks
    (security review #1)."""
    repo = UserRepository(session)
    gm = GroupMemberRepository(session)
    group_a = Group(id=uuid_utils.uuid7(), telegram_chat_id=1, default_currency="SGD")
    group_b = Group(id=uuid_utils.uuid7(), telegram_chat_id=2, default_currency="SGD")
    session.add_all([group_a, group_b])
    await session.flush()
    placeholder = await repo.resolve_mention("alice")
    await gm.ensure_member(group_a.id, placeholder.id)  # placeholder lives in A only

    claimed = await repo.upsert(
        telegram_user_id=999,
        username="alice",
        first_name="A",
        last_name=None,
        claim_in_group=group_b.id,  # interacting in B
    )

    assert claimed.id != placeholder.id  # a fresh claimed row, not the placeholder
    assert claimed.telegram_user_id == 999
    reread = (
        await session.execute(select(User).where(User.id == placeholder.id))
    ).scalar_one()
    assert reread.telegram_user_id is None  # group-A placeholder still unclaimed
    assert await _count_users(session) == 2


async def test_claim_preserves_existing_ledger_rows(session: AsyncSession) -> None:
    """A share bound to the placeholder must still reference the same users.id
    after the claim — the no-fan-out-rewrite guarantee."""
    repo = UserRepository(session)
    group = _group()
    payer = await repo.upsert(
        telegram_user_id=300, username="payer", first_name=None, last_name=None
    )
    placeholder = await repo.resolve_mention("charlie")
    session.add(group)
    await session.flush()
    # The placeholder is a member of the group (every mention ensures this), so the
    # group-scoped claim gate (security review #1) lets the claim through.
    await GroupMemberRepository(session).ensure_member(group.id, placeholder.id)

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
    session.add(
        ExpenseShare(expense_id=expense.id, user_id=placeholder.id, share_cents=50)
    )
    await session.flush()

    claimed = await repo.upsert(
        telegram_user_id=400,
        username="charlie",
        first_name="Charlie",
        last_name=None,
        claim_in_group=group.id,
    )
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
    claimed = await repo.upsert(
        telegram_user_id=500, username="dave", first_name=None, last_name=None
    )

    resolved = await repo.resolve_mention("dave")  # a handle we already know
    assert resolved.id == claimed.id
    assert await _count_users(session) == 1  # no duplicate placeholder spawned


async def test_resolve_mention_reuses_pending_placeholder(
    session: AsyncSession,
) -> None:
    repo = UserRepository(session)
    first = await repo.resolve_mention("erin")
    second = await repo.resolve_mention("erin")
    assert first.id == second.id
    assert await _count_users(session) == 1


async def test_resolve_mention_creates_placeholder_for_unknown_handle(
    session: AsyncSession,
) -> None:
    repo = UserRepository(session)
    placeholder = await repo.resolve_mention("frank")
    assert placeholder.telegram_user_id is None
    assert placeholder.username == "frank"
    assert await _count_users(session) == 1


async def test_find_by_mention_returns_none_for_unknown(session: AsyncSession) -> None:
    """find_by_mention is lookup-only: an unknown handle yields None and creates
    nothing (so /settleup never spawns a stray placeholder)."""
    repo = UserRepository(session)
    assert await repo.find_by_mention("ghost") is None
    assert await _count_users(session) == 0


async def test_find_by_mention_finds_existing_prefers_claimed(
    session: AsyncSession,
) -> None:
    repo = UserRepository(session)
    placeholder = await repo.resolve_mention("heidi")  # placeholder created
    claimed = User(id=uuid_utils.uuid7(), telegram_user_id=700, username="heidi")
    session.add(claimed)
    await session.flush()

    found = await repo.find_by_mention("heidi")
    assert found is not None
    assert found.id == claimed.id  # claimed wins over the placeholder
    assert found.id != placeholder.id
    # Lookup must not create anything: only the placeholder + claimed exist.
    assert await _count_users(session) == 2


async def test_resolve_mention_claimed_wins_over_placeholder(
    session: AsyncSession,
) -> None:
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
    user = await repo.upsert(
        telegram_user_id=700, username=None, first_name="NoHandle", last_name=None
    )
    assert user.telegram_user_id == 700
    assert user.username is None
    assert await _count_users(session) == 1
