"""Statement query service — a paginated chronological view of the ledger.

Reads the merged expense+settlement stream (group-wide, or scoped to one user),
resolves usernames for just the requested page, and returns a StatementPage. A
pure read: it writes nothing and derives nothing about balances.
"""
import uuid

from countbeans.dto.domain import StatementEntry, StatementPage

from .uow import UnitOfWork

DEFAULT_PAGE_SIZE = 8


async def get_statement_page(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> StatementPage:
    raw = await uow.ledger.list_entries(group_id, user_id=user_id)
    total = len(raw)

    # Clamp the page so a stale Next/Prev button (history changed underneath it)
    # never lands out of range.
    last_page = max(0, (total - 1) // page_size) if total else 0
    page = min(max(page, 0), last_page)

    window = raw[page * page_size : page * page_size + page_size]

    ids: set[uuid.UUID] = set()
    for e in window:
        ids.add(e.actor_id)
        if e.counterparty_id is not None:
            ids.add(e.counterparty_id)
    names = await uow.balances.get_usernames(ids)

    entries = [
        StatementEntry(
            kind=e.kind,
            created_at=e.created_at,
            amount_cents=e.amount_cents,
            currency=e.currency,
            description=e.description,
            actor_username=names.get(e.actor_id),
            counterparty_username=(
                names.get(e.counterparty_id) if e.counterparty_id is not None else None
            ),
            participant_count=e.participant_count,
            voided=e.voided,
        )
        for e in window
    ]
    return StatementPage(entries=entries, page=page, page_size=page_size, total=total)
