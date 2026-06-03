"""settle_up service function — records a settlement payment in the ledger."""
import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID (pydantic DTOs reject uuid_utils.UUID)

from countbeans.db.models import Settlement
from countbeans.dto.commands import SettleUpCommand
from countbeans.dto.domain import BalanceKey
from countbeans.dto.results import SettlementCreatedResult

from .uow import UnitOfWork


async def settle_up(uow: UnitOfWork, cmd: SettleUpCommand) -> SettlementCreatedResult:
    if cmd.from_user_id == cmd.to_user_id:
        raise ValueError("from_user_id and to_user_id must be different users")

    balances = await uow.balances.compute_for_group(cmd.group_id)
    payer_balance = balances.get(BalanceKey(cmd.from_user_id, cmd.currency), 0)
    recipient_balance = balances.get(BalanceKey(cmd.to_user_id, cmd.currency), 0)

    if payer_balance >= 0:
        raise ValueError(
            f"You don't owe anyone in {cmd.currency} — your balance is already "
            f"{'zero' if payer_balance == 0 else 'positive'}."
        )
    if recipient_balance <= 0:
        raise ValueError(
            f"That person is not owed any {cmd.currency} — settling with them "
            "would not reduce a real debt."
        )

    settlement_id = uuid_utils.uuid7()
    settlement = Settlement(
        id=settlement_id,
        group_id=cmd.group_id,
        from_user_id=cmd.from_user_id,
        to_user_id=cmd.to_user_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
    )
    await uow.settlements.add(settlement)
    return uow.settlements._to_dto(settlement)
