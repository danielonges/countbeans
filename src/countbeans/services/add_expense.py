"""add_expense service function with split-computation helpers."""

import logging
import uuid

logger = logging.getLogger(__name__)

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID (pydantic DTOs reject uuid_utils.UUID)

from countbeans.db.models import Expense
from countbeans.dto.commands import AddExpenseCommand, MentionedUser
from countbeans.dto.domain import MemberInfo
from countbeans.dto.results import ExpenseCreatedResult

from .errors import DomainError
from .uow import UnitOfWork

Id = uuid.UUID


async def resolve_participants(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    payer_id: uuid.UUID,
    named_handles: list[str],
    *,
    mentioned_users: list[MentionedUser] | None = None,
    event_id: uuid.UUID | None = None,
) -> list[MemberInfo]:
    """Resolve who an expense is split among, from the @handles parsed off the
    command plus any ``text_mention`` entities the bot resolved to real Telegram
    identities. The ``@all`` keyword is bot-layer grammar: the handler strips it
    (and recognizes "split everyone") via ``parsing.is_all`` before calling here,
    so this function never sees the literal — an empty list *is* "split everyone".

    * **No participants named at all** → every member the bot knows in the group,
      the payer included.
    * **Named participants** → exactly those users; the payer is **not** added
      automatically — mention yourself to be included.
      - ``mentioned_users`` carry a real ``telegram_user_id`` → resolve to a
        **claimed** user (hijack-proof, no username placeholder; security review #1).
      - unknown ``named_handles`` become pending placeholders.

    When ``event_id`` is given the scope is the **event roster** (a deliberate
    opt-in subset of the group): an empty list splits the roster — no group
    coverage check, since the roster is intentional — and named participants join
    the roster implicitly (CLAUDE.md "Events"). The payer is always ensured onto
    the group (and the event roster, when scoped) first, so a "split everyone"
    reflects them and a payer who only ever pays still appears. Returns one
    MemberInfo per participant, deduplicated, order preserved.
    """
    mentioned_users = mentioned_users or []
    await uow.group_members.ensure_member(group_id, payer_id)
    if event_id is not None:
        await uow.events.ensure_member(event_id, payer_id)

    if not named_handles and not mentioned_users:
        scope = f"event={event_id}" if event_id else "group"
        logger.debug("resolve_participants: split=everyone scope=%s", scope)
        if event_id is not None:
            return await uow.events.list_members(event_id)
        return await uow.group_members.list_members(group_id)

    participants: list[MemberInfo] = []
    seen: set[uuid.UUID] = set()

    async def _add(user) -> None:
        await uow.group_members.ensure_member(group_id, user.id)
        if event_id is not None:
            await uow.events.ensure_member(event_id, user.id)
        if user.id not in seen:
            participants.append(
                MemberInfo(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    is_pending=user.telegram_user_id is None,
                )
            )
            seen.add(user.id)

    # Real-id mentions first: a text_mention gives us the permanent telegram_user_id,
    # so resolve straight to a claimed user (claim_in_group converges a matching
    # in-group placeholder) — never a username placeholder.
    for m in mentioned_users:
        user = await uow.users.upsert(
            telegram_user_id=m.telegram_user_id,
            username=m.username,
            first_name=m.first_name,
            last_name=m.last_name,
            claim_in_group=group_id,
        )
        await _add(user)
    for handle in named_handles:
        await _add(await uow.users.resolve_mention(handle))
    logger.debug("resolve_participants: split=named count=%d", len(participants))
    return participants


def apportion(amount_cents: int, weights: dict[Id, int]) -> dict[Id, int]:
    """Split amount_cents proportionally to weights, summing exactly to amount_cents.

    Uses the largest-remainder method so cents always reconcile.
    Tie-breaks by UUID for determinism.
    """
    total = sum(weights.values())
    if total <= 0:
        raise DomainError("weights must sum to a positive value")
    shares: dict[Id, int] = {}
    remainders: list[tuple[int, Id]] = []
    allocated = 0
    for k, w in weights.items():
        exact = amount_cents * w
        shares[k] = exact // total
        allocated += shares[k]
        remainders.append((exact % total, k))
    remainders.sort(key=lambda r: (-r[0], r[1]))
    for _, k in remainders[: amount_cents - allocated]:
        shares[k] += 1
    return shares


def compute_shares(
    amount_cents: int,
    participants: list[Id],
    mode: str = "equal",
    params: dict[Id, int] | None = None,
) -> dict[Id, int]:
    match mode:
        case "equal":
            return apportion(amount_cents, {u: 1 for u in participants})
        case "weighted":
            assert params is not None
            return apportion(amount_cents, params)
        case "percent":
            assert params is not None
            if sum(params.values()) != 100:
                raise DomainError("percentages must sum to 100")
            return apportion(amount_cents, params)
        case "exact":
            assert params is not None
            if sum(params.values()) != amount_cents:
                raise DomainError("exact shares must sum to the expense amount")
            return dict(params)
        case _:
            # Unreachable through a validated command (split_mode is a Literal) —
            # a programming-error guard, so deliberately NOT a DomainError.
            raise ValueError(f"unknown split mode: {mode}")


async def add_expense(uow: UnitOfWork, cmd: AddExpenseCommand) -> ExpenseCreatedResult:
    logger.debug(
        "add_expense: group=%s payer=%s amount=%d currency=%s participants=%d event=%s",
        cmd.group_id,
        cmd.payer_id,
        cmd.amount_cents,
        cmd.currency,
        len(cmd.participants),
        cmd.event_id,
    )
    shares = compute_shares(
        cmd.amount_cents, list(cmd.participants), cmd.split_mode, cmd.split_params
    )
    expense_id = uuid_utils.uuid7()
    expense = Expense(
        id=expense_id,
        group_id=cmd.group_id,
        event_id=cmd.event_id,
        payer_id=cmd.payer_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
        description=cmd.description,
        created_by=cmd.created_by,
    )
    await uow.expenses.add(expense, shares)
    logger.debug("add_expense: recorded expense_id=%s", expense_id)
    return ExpenseCreatedResult(
        expense_id=expense_id,
        group_id=cmd.group_id,
        payer_id=cmd.payer_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
        description=cmd.description,
        shares=shares,
        event_id=cmd.event_id,
    )
