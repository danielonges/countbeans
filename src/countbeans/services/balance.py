"""Balance query service — derives net balances from the immutable ledger."""
import uuid
from collections import defaultdict
from dataclasses import dataclass

from countbeans.dto.domain import BalanceMap, GroupSummary, MemberBalance, Transfer

from .uow import UnitOfWork


async def compute_balances(uow: UnitOfWork, group_id: uuid.UUID) -> BalanceMap:
    return await uow.balances.compute_for_group(group_id)


async def get_group_summary(
    uow: UnitOfWork, group_id: uuid.UUID, simplify_debts: bool
) -> GroupSummary:
    raw = await compute_balances(uow, group_id)
    user_ids = {key.user_id for key in raw}
    username_map = await uow.balances.get_usernames(user_ids)

    balances = [
        MemberBalance(
            user_id=key.user_id,
            username=username_map.get(key.user_id),
            balance_cents=cents,
            currency=key.currency,
        )
        for key, cents in raw.items()
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


@dataclass(slots=True)
class _Party:
    """A debtor or creditor with the amount still left to match, drawn down to
    zero as transfers are emitted. Replaces a pair of index-synced arrays."""

    user_id: uuid.UUID
    remaining: int


def _greedy_transfers(balances: BalanceMap, *, by_amount: bool) -> list[Transfer]:
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
    for key, cents in balances.items():
        by_currency[key.currency][key.user_id] = cents

    def ordered(party: list[_Party]) -> list[_Party]:
        # Sort by amount descending then id, or by id alone — never by id first
        # when simplifying, which would inflate the transfer count.
        key = (
            (lambda p: (-p.remaining, p.user_id))
            if by_amount
            else (lambda p: (0, p.user_id))
        )
        return sorted(party, key=key)

    transfers: list[Transfer] = []
    for cur, bal in by_currency.items():
        debtors = ordered([_Party(uid, -cents) for uid, cents in bal.items() if cents < 0])
        creditors = ordered([_Party(uid, cents) for uid, cents in bal.items() if cents > 0])
        i = j = 0
        while i < len(debtors) and j < len(creditors):
            pay = min(debtors[i].remaining, creditors[j].remaining)
            transfers.append(
                Transfer(
                    from_user_id=debtors[i].user_id,
                    to_user_id=creditors[j].user_id,
                    amount_cents=pay,
                    currency=cur,
                )
            )
            debtors[i].remaining -= pay
            creditors[j].remaining -= pay
            if debtors[i].remaining == 0:
                i += 1
            if creditors[j].remaining == 0:
                j += 1
    return transfers


def _simplified_transfers(balances: BalanceMap) -> list[Transfer]:
    return _greedy_transfers(balances, by_amount=True)


def _raw_pairwise_transfers(balances: BalanceMap) -> list[Transfer]:
    return _greedy_transfers(balances, by_amount=False)
