"""settle_up service function — records a settlement payment in the ledger."""

import logging
import uuid

logger = logging.getLogger(__name__)

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID (pydantic DTOs reject uuid_utils.UUID)

from countbeans.db.models import Settlement
from countbeans.dto.commands import SettleUpCommand
from countbeans.dto.results import SettlementCreatedResult

from .balance import suggested_owed, suggested_owed_by_currency, suggested_transfers
from .errors import DomainError
from .uow import UnitOfWork


def _fmt(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


async def owed_by_currency(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    *,
    simplify_debts: bool,
    event_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Per-currency amounts ``from_id`` is suggested to pay ``to_id`` within the
    given scope. Drives the amount-less /settleup auto-fill and its multi-currency
    hint. An empty dict means the current suggested settlement routes no payment
    between this pair."""
    balances = await uow.balances.compute_for_group(group_id, event_id=event_id)
    return suggested_owed_by_currency(
        balances, from_id, to_id, simplify_debts=simplify_debts
    )


async def settle_up(
    uow: UnitOfWork, cmd: SettleUpCommand, *, simplify_debts: bool
) -> SettlementCreatedResult:
    logger.debug(
        "settle_up: group=%s from=%s to=%s amount=%d currency=%s event=%s",
        cmd.group_id,
        cmd.from_user_id,
        cmd.to_user_id,
        cmd.amount_cents,
        cmd.currency,
        cmd.event_id,
    )
    # from != to is guaranteed by SettleUpCommand's users_must_differ validator.

    # A settlement is only valid *along a suggested transfer*, and never for more
    # than that transfer's amount — so balances can never flip (CLAUDE.md
    # "Debt simplification"; see services.balance.suggested_owed). This single
    # check subsumes the old payer-negative / recipient-positive sign checks:
    # both cases yield owed == 0.
    balances = await uow.balances.compute_for_group(cmd.group_id, event_id=cmd.event_id)
    owed = suggested_owed(
        balances,
        cmd.from_user_id,
        cmd.to_user_id,
        cmd.currency,
        simplify_debts=simplify_debts,
    )
    if owed <= 0:
        raise DomainError(
            f"That settlement isn't a suggested payment in {cmd.currency} — no "
            "debt runs in that direction. Run /balance all to see who owes whom."
        )
    if cmd.amount_cents > owed:
        raise DomainError(
            f"Only {_fmt(owed, cmd.currency)} is owed in that direction — settle "
            "that or less, or omit the amount to settle in full."
        )

    settlement_id = uuid_utils.uuid7()
    settlement = Settlement(
        id=settlement_id,
        group_id=cmd.group_id,
        event_id=cmd.event_id,
        from_user_id=cmd.from_user_id,
        to_user_id=cmd.to_user_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
    )
    await uow.settlements.add(settlement)
    logger.debug("settle_up: recorded settlement_id=%s", settlement_id)
    return uow.settlements._to_dto(settlement)


async def settle_all(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    *,
    simplify_debts: bool,
    event_id: uuid.UUID | None = None,
) -> list[SettlementCreatedResult]:
    """Record a settlement for every outstanding suggested transfer in the scope,
    zeroing it at once ("clear the board"). Honors the simplify toggle, so it
    records exactly the transfers /balance all would show. One transaction;
    returns the recorded settlements (empty when the scope is already settled).

    Each transfer becomes a real, ordinary settlement event — like any other
    /settleup — so the ledger stays a faithful audit trail (no special
    "everyone settled" marker). Admin-gating lives in the bot layer.
    """
    logger.debug("settle_all: group=%s event=%s", group_id, event_id)
    balances = await uow.balances.compute_for_group(group_id, event_id=event_id)
    results: list[SettlementCreatedResult] = []
    for transfer in suggested_transfers(balances, simplify_debts=simplify_debts):
        settlement = Settlement(
            id=uuid_utils.uuid7(),
            group_id=group_id,
            event_id=event_id,
            from_user_id=transfer.from_user_id,
            to_user_id=transfer.to_user_id,
            amount_cents=transfer.amount_cents,
            currency=transfer.currency,
        )
        await uow.settlements.add(settlement)
        results.append(uow.settlements._to_dto(settlement))
    logger.debug("settle_all: recorded %d settlements", len(results))
    return results
