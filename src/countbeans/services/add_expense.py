"""add_expense service function with split-computation helpers."""

import logging
import uuid

logger = logging.getLogger(__name__)

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID (pydantic DTOs reject uuid_utils.UUID)

from countbeans.db.models import Expense
from countbeans.dto.commands import AddExpenseCommand
from countbeans.dto.domain import MemberInfo
from countbeans.dto.results import ExpenseCreatedResult

from .uow import UnitOfWork

Id = uuid.UUID


def _is_split_everyone(mentions: list[str]) -> bool:
    """True when an expense should split among the whole group: no @mentions at
    all, or only the @all keyword."""
    return not any(h.lower() != "all" for h in mentions)


async def resolve_participants(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    payer_id: uuid.UUID,
    mentions: list[str],
    *,
    event_id: uuid.UUID | None = None,
) -> list[MemberInfo]:
    """Resolve who an expense is split among, from the raw @handles parsed off
    the command.

    * **No named mentions** (none at all, or only ``@all``) → every member the
      bot knows in the group, the payer included.
    * **Named mentions** → exactly those users; the payer is **not** added
      automatically — mention yourself to be included. Unknown handles become
      pending placeholders.

    When ``event_id`` is given the scope is the **event roster** (a deliberate
    opt-in subset of the group): ``@all``/empty splits the roster — no group
    coverage check, since the roster is intentional — and named mentions join the
    roster implicitly (CLAUDE.md "Events"). The payer is always ensured onto the
    group (and the event roster, when scoped) first, so a "split everyone"
    reflects them and a payer who only ever pays still appears. Returns one
    MemberInfo per participant, deduplicated, order preserved.
    """
    await uow.group_members.ensure_member(group_id, payer_id)
    if event_id is not None:
        await uow.events.ensure_member(event_id, payer_id)

    if _is_split_everyone(mentions):
        scope = f"event={event_id}" if event_id else "group"
        logger.debug("resolve_participants: split=everyone scope=%s", scope)
        if event_id is not None:
            return await uow.events.list_members(event_id)
        return await uow.group_members.list_members(group_id)

    participants: list[MemberInfo] = []
    seen: set[uuid.UUID] = set()
    for handle in mentions:
        if handle.lower() == "all":
            continue
        user = await uow.users.resolve_mention(handle)
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
    logger.debug("resolve_participants: split=named count=%d", len(participants))
    return participants


def apportion(amount_cents: int, weights: dict[Id, int]) -> dict[Id, int]:
    """Split amount_cents proportionally to weights, summing exactly to amount_cents.

    Uses the largest-remainder method so cents always reconcile.
    Tie-breaks by UUID for determinism.
    """
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
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
                raise ValueError("percentages must sum to 100")
            return apportion(amount_cents, params)
        case "exact":
            assert params is not None
            if sum(params.values()) != amount_cents:
                raise ValueError("exact shares must sum to the expense amount")
            return dict(params)
        case _:
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
