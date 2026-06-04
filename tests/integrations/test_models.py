import pytest
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, Settlement, User


def _user(**kwargs) -> User:
    return User(id=uuid_utils.uuid7(), **kwargs)


def _group(telegram_chat_id: int = 1, **kwargs) -> Group:
    kwargs.setdefault("default_currency", "SGD")
    return Group(id=uuid_utils.uuid7(), telegram_chat_id=telegram_chat_id, **kwargs)


def _expense(group: Group, payer: User, **kwargs) -> Expense:
    kwargs.setdefault("amount_cents", 1000)
    kwargs.setdefault("currency", "SGD")
    return Expense(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        payer_id=payer.id,
        created_by=payer.id,
        **kwargs,
    )


def _settlement(group: Group, from_user: User, to_user: User, **kwargs) -> Settlement:
    kwargs.setdefault("amount_cents", 500)
    kwargs.setdefault("currency", "SGD")
    return Settlement(
        id=uuid_utils.uuid7(),
        group_id=group.id,
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        **kwargs,
    )


# --- User ---


async def test_user_nullable_fields_persist(session: AsyncSession) -> None:
    user = _user()
    session.add(user)
    await session.flush()
    await session.refresh(user)
    assert user.telegram_user_id is None
    assert user.username is None
    assert user.first_name is None
    assert user.last_name is None


async def test_user_placeholder_accepts_null_telegram_id(session: AsyncSession) -> None:
    user = _user(username="alice")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    assert user.telegram_user_id is None


async def test_user_id_is_generated(session: AsyncSession) -> None:
    user = _user()
    session.add(user)
    await session.flush()
    assert user.id is not None


async def test_user_telegram_user_id_unique(session: AsyncSession) -> None:
    session.add_all([_user(telegram_user_id=42), _user(telegram_user_id=42)])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_user_pending_placeholder_username_unique(session: AsyncSession) -> None:
    # The partial unique index enforces at most one PENDING placeholder
    # (telegram_user_id IS NULL) per username.
    session.add_all([_user(username="bob"), _user(username="bob")])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_user_claimed_and_placeholder_may_share_username(
    session: AsyncSession,
) -> None:
    # The index is partial, so it constrains only placeholders: a claimed user
    # and a placeholder (or two claimed users) may share a username across a
    # rename/reuse without violating it.
    session.add_all(
        [
            _user(username="carol", telegram_user_id=1),
            _user(username="carol"),  # pending placeholder, same handle
            _user(username="carol", telegram_user_id=2),
        ]
    )
    await session.flush()  # must not raise


# --- Group ---


async def test_group_simplify_debts_defaults_true(session: AsyncSession) -> None:
    group = _group()
    session.add(group)
    await session.flush()
    await session.refresh(group)
    assert group.simplify_debts is True


async def test_group_currency_check_rejects_short(session: AsyncSession) -> None:
    session.add(_group(default_currency="US"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_group_currency_check_rejects_long(session: AsyncSession) -> None:
    # 4 chars overflows VARCHAR(3) → truncation (DBAPIError) before the CHECK runs.
    session.add(_group(default_currency="USDD"))
    with pytest.raises(DBAPIError):
        await session.flush()


async def test_group_telegram_chat_id_unique(session: AsyncSession) -> None:
    session.add_all([_group(telegram_chat_id=999), _group(telegram_chat_id=999)])
    with pytest.raises(IntegrityError):
        await session.flush()


# --- Expense ---


async def test_expense_rejects_zero_amount(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    session.add(_expense(group, user, amount_cents=0))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_expense_rejects_negative_amount(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    session.add(_expense(group, user, amount_cents=-100))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_expense_currency_check_rejects_short(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    session.add(_expense(group, user, currency="US"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_expense_currency_check_rejects_long(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    # 4 chars overflows VARCHAR(3) → truncation (DBAPIError) before the CHECK runs.
    session.add(_expense(group, user, currency="USDD"))
    with pytest.raises(DBAPIError):
        await session.flush()


# --- ExpenseShare ---


async def test_expense_share_rejects_negative(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    expense = _expense(group, user)
    session.add(expense)
    await session.flush()
    session.add(ExpenseShare(expense_id=expense.id, user_id=user.id, share_cents=-1))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_expense_share_accepts_zero(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    expense = _expense(group, user)
    session.add(expense)
    await session.flush()
    share = ExpenseShare(expense_id=expense.id, user_id=user.id, share_cents=0)
    session.add(share)
    await session.flush()
    await session.refresh(share)
    assert share.share_cents == 0


# --- Settlement ---


async def test_settlement_rejects_self(session: AsyncSession) -> None:
    group, user = _group(), _user()
    session.add_all([group, user])
    await session.flush()
    session.add(_settlement(group, user, user))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_settlement_rejects_zero_amount(session: AsyncSession) -> None:
    group, user_a, user_b = _group(), _user(), _user()
    session.add_all([group, user_a, user_b])
    await session.flush()
    session.add(_settlement(group, user_a, user_b, amount_cents=0))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_settlement_rejects_negative_amount(session: AsyncSession) -> None:
    group, user_a, user_b = _group(), _user(), _user()
    session.add_all([group, user_a, user_b])
    await session.flush()
    session.add(_settlement(group, user_a, user_b, amount_cents=-50))
    with pytest.raises(IntegrityError):
        await session.flush()
