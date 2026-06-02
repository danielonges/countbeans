"""Balance query service — derives net balances from the immutable ledger."""
import uuid
from collections import defaultdict

from countbeans.dto.domain import GroupSummary, MemberBalance, Transfer

from .uow import UnitOfWork


async def compute_balances(
    uow: UnitOfWork, group_id: uuid.UUID
) -> dict[tuple[uuid.UUID, str], int]:
    return await uow.balances.compute_for_group(group_id)


async def get_group_summary(
    uow: UnitOfWork, group_id: uuid.UUID, simplify_debts: bool
) -> GroupSummary:
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

    # The toggle is presentation-only: `balances` above is derived straight from
    # the ledger and is identical either way — only the suggested-transfer view
    # changes (see CLAUDE.md "Debt simplification").
    suggest = _simplified_transfers if simplify_debts else _raw_pairwise_transfers

    return GroupSummary(
        group_id=group_id,
        balances=balances,
        suggested_transfers=suggest(raw),
    )


def _greedy_transfers(
    balances: dict[tuple[uuid.UUID, str], int], *, by_amount: bool
) -> list[Transfer]:
    """Settle each currency by matching debtors to creditors, drawing both sides
    down as it goes. The match order is the only knob:

    * ``by_amount=True``  — largest debtor vs. largest creditor (debt
      simplification): the greedy heuristic that keeps the transfer count low.
      The true minimum is NP-complete, so this is "minimal-ish", not minimal.
    * ``by_amount=False`` — match in id order (the raw, unsimplified view): a
      valid settlement that just doesn't reorder for fewness.

    Both tie-break by id, so output is deterministic, and both are always valid
    settlements: balances sum to zero per currency, so every balance ends at
    zero. See CLAUDE.md "Debt simplification"."""
    by_currency: dict[str, dict[uuid.UUID, int]] = defaultdict(dict)
    for (uid, cur), cents in balances.items():
        by_currency[cur][uid] = cents

    def ordered(party: list[tuple[uuid.UUID, int]]) -> list[tuple[uuid.UUID, int]]:
        # Sort by amount descending then id, or by id alone — never by id first
        # when simplifying, which would inflate the transfer count.
        key = (lambda x: (-x[1], x[0])) if by_amount else (lambda x: (0, x[0]))
        return sorted(party, key=key)

    transfers: list[Transfer] = []
    for cur, bal in by_currency.items():
        debtors = ordered([(uid, -cents) for uid, cents in bal.items() if cents < 0])
        creditors = ordered([(uid, cents) for uid, cents in bal.items() if cents > 0])
        # Mutable remaining amounts, drawn down as debtors/creditors are matched.
        d_rem = [amt for _, amt in debtors]
        c_rem = [amt for _, amt in creditors]
        i = j = 0
        while i < len(debtors) and j < len(creditors):
            pay = min(d_rem[i], c_rem[j])
            transfers.append(
                Transfer(
                    from_user_id=debtors[i][0],
                    to_user_id=creditors[j][0],
                    amount_cents=pay,
                    currency=cur,
                )
            )
            d_rem[i] -= pay
            c_rem[j] -= pay
            if d_rem[i] == 0:
                i += 1
            if c_rem[j] == 0:
                j += 1
    return transfers


def _simplified_transfers(
    balances: dict[tuple[uuid.UUID, str], int],
) -> list[Transfer]:
    return _greedy_transfers(balances, by_amount=True)


def _raw_pairwise_transfers(
    balances: dict[tuple[uuid.UUID, str], int],
) -> list[Transfer]:
    return _greedy_transfers(balances, by_amount=False)
