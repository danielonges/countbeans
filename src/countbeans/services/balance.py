"""Balance query service — derives net balances from the immutable ledger."""
import uuid
from collections import defaultdict

from countbeans.dto.domain import GroupSummary, MemberBalance, Transfer

from .uow import UnitOfWork


async def compute_balances(
    uow: UnitOfWork, group_id: uuid.UUID
) -> dict[tuple[uuid.UUID, str], int]:
    return await uow.balances.compute_for_group(group_id)


async def get_group_summary(uow: UnitOfWork, group_id: uuid.UUID) -> GroupSummary:
    raw = await compute_balances(uow, group_id)
    user_ids = {uid for uid, _ in raw}
    username_map = await uow.balances.get_usernames(user_ids)

    balances = [
        MemberBalance(
            user_id=uid,
            username=username_map.get(uid),
            balance_cents=cents,
            currency=cur,
        )
        for (uid, cur), cents in raw.items()
    ]

    return GroupSummary(
        group_id=group_id,
        balances=balances,
        suggested_transfers=_raw_pairwise_transfers(raw),
    )


def _raw_pairwise_transfers(
    balances: dict[tuple[uuid.UUID, str], int],
) -> list[Transfer]:
    by_currency: dict[str, dict[uuid.UUID, int]] = defaultdict(dict)
    for (uid, cur), cents in balances.items():
        by_currency[cur][uid] = cents

    transfers: list[Transfer] = []
    for cur, bal in by_currency.items():
        for debtor_id, debt in bal.items():
            if debt >= 0:
                continue
            owed = -debt
            for creditor_id, credit in bal.items():
                if credit <= 0:
                    continue
                pay = min(owed, credit)
                if pay > 0:
                    transfers.append(
                        Transfer(
                            from_user_id=debtor_id,
                            to_user_id=creditor_id,
                            amount_cents=pay,
                            currency=cur,
                        )
                    )
    return transfers
