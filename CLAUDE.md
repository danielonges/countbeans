# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

countbeans is a Telegram bot for tracking and splitting shared expenses within Telegram groups (Splitwise-style, but Telegram-native). Users interact via commands like `/addexpense`, `/balance`, and `/settleup` directly in group chats.

## Commands

```bash
# Install dependencies
uv sync

# Run the bot
uv run countbeans

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/path/to/test_file.py::test_name
```

## Architecture

The project has two runtime entry points that currently operate independently:

- **Telegram bot** (`src/countbeans/main.py`): Built with `aiogram` (async-first Bot API). Handles group chat commands and is the primary user-facing interface. Run via `uv run countbeans`.
- **FastAPI server** (`src/countbeans/apis/`): HTTP API layer, intended for AI agent interactions via `deepagents`.

Both share the config in `src/countbeans/config/` for settings (see below).

**Planned data layer**: PostgreSQL via SQLAlchemy + asyncpg, with Alembic for migrations. The schema (users, groups, group_members, expenses, expense_shares, settlements) is specified in the Product Spec below but not yet implemented. Balances are **derived** from the ledger, not stored.

## Commits

Use gitmoji prefixes for all commits. See https://gitmoji.dev/ for the full reference. Examples: 🎉 init, 🔧 config/tooling, ✨ new feature, 🐛 bug fix, ♻️ refactor, 🗑️ remove code/files, 📦 dependencies.

## Settings

All config lives in `src/countbeans/config/core.py` using `pydantic-settings`. Environment variables must be prefixed with `COUNTBEANS_`:

| Env var | Type | Description |
|---|---|---|
| `COUNTBEANS_API_ID` | `int` | Telegram API ID |
| `COUNTBEANS_API_HASH` | `str` | Telegram API hash |
| `COUNTBEANS_BOT_TOKEN` | `str` | Telegram bot token |

All fields are required — the app will raise a `ValidationError` at startup if any are missing. Use a `.env` file at the project root.

## Product Spec

Original design notes (SPEC-001). The bot is added to a Telegram group and tracks shared expenses conversationally, keeping a running tally of who owes whom and facilitating settlement — Splitwise-style, Telegram-only.

### Design principles

These cut across the whole data model and are the reason the schema below differs from a naive design:

- **Money is integer minor units (cents), never `float`.** Float arithmetic accumulates rounding error across a ledger. Store and compute in `BIGINT` cents; format to a decimal string only at display time.
- **Append-only ledger; balances are derived, never stored.** Expenses and settlements are immutable events. There is no `debts` table — a user's balance is computed by summing the ledger on read. This eliminates read-modify-write races, keeps a full audit trail, and means edits/deletes are done by *voiding* an event and re-adding, not mutating in place.
- **Every expense reconciles.** Per expense, the participant shares sum *exactly* to the expense amount. Even splits distribute leftover cents deterministically (see the algorithm), so the books always balance and the sum of all member balances in a group is zero.
- **Currency is explicit per event.** Each expense and settlement carries an ISO-4217 code. Balances are computed per currency; cross-currency netting requires an FX policy and is out of scope for now.

### Requirements

- Must work in any group with no manual configuration once added.
- Users add expenses (amount, participants, details); the bot tracks who paid what and computes shared debts.
- Users can view balances for themselves and others.
- Support multiple split methods (evenly, custom shares).
- Settling up (cash or digital) should be easy.
- All group and user data must persist across sessions.

**MoSCoW prioritization**

- *Must-have*: group expense tracking; basic expense input and derived balances; persistent storage; per-user balance summaries; settling up (full **or partial** payments).
- *Should-have*: unequal/custom splits; debt simplification (minimal set of transfers to settle a group); multi-currency support.
- *Could-have*: expense categories; notifications for outstanding debts; multiple payers per expense.
- *Won't-have*: any web or mobile interface outside Telegram.

### Key commands

- `/addexpense` — add an expense, e.g. `/addexpense 50 "Dinner" @user1 @user2`. Custom shares: `/addexpense 50 "Dinner" @user1:30 @user2:20`.
- `/balance` — show the caller's net balance with other group members (derived from the ledger).
- `/settleup` — record a settlement payment (full or partial) from one user to another, e.g. `/settleup @user1 20`.

### Components

- **Telegram bot** — listens for commands/messages in groups, parses command structure and parameters (amount, description, participants), and sends confirmation/balance responses back to the group.
- **Bot server** — FastAPI backend that processes parsed commands, manages multi-step interaction state, handles error cases (missing data, bad formatting), and talks to the database.
- **Expense manager** — validates and records expenses and settlements as ledger events, and computes derived balances (and, optionally, a simplified set of transfers).
- **Database** — persists users, group membership, and the immutable ledger of expenses, expense shares, and settlements.

### Schema (PostgreSQL)

There is intentionally **no `debts` table** — balances are derived (see below). Edits/deletes are done by setting `voided_at`, not by mutating rows.

```sql
-- Telegram users seen by the bot
CREATE TABLE users (
  user_id     BIGINT PRIMARY KEY,
  username    VARCHAR(255),                -- @handle (mutable on Telegram; not a key)
  first_name  VARCHAR(255),
  last_name   VARCHAR(255)
);

-- Telegram groups the bot is in
CREATE TABLE groups (
  group_id          BIGINT PRIMARY KEY,    -- Telegram chat ID
  group_name        VARCHAR(255),
  default_currency  CHAR(3) NOT NULL DEFAULT 'USD',   -- ISO 4217
  CHECK (LENGTH(default_currency) = 3)
);

-- Membership, so we can split "evenly among everyone in the group".
-- PK includes joined_at so that a user who leaves and rejoins can be
-- represented as a new membership period without losing history.
CREATE TABLE group_members (
  group_id   BIGINT NOT NULL REFERENCES groups(group_id),
  user_id    BIGINT NOT NULL REFERENCES users(user_id),
  joined_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  left_at    TIMESTAMP WITH TIME ZONE,    -- NULL = still a member
  PRIMARY KEY (group_id, user_id, joined_at)
);

-- Immutable expense events; soft-deleted via voided_at
CREATE TABLE expenses (
  expense_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  group_id      BIGINT NOT NULL REFERENCES groups(group_id),
  payer_id      BIGINT NOT NULL REFERENCES users(user_id),
  amount_cents  BIGINT NOT NULL CHECK (amount_cents > 0),   -- integer minor units
  currency      CHAR(3) NOT NULL CHECK (LENGTH(currency) = 3),
  description   VARCHAR(255),
  created_by    BIGINT NOT NULL REFERENCES users(user_id),
  created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  voided_at     TIMESTAMP WITH TIME ZONE,                   -- NULL = active
  voided_by     BIGINT REFERENCES users(user_id)
);

-- Per-participant shares; MUST sum to expenses.amount_cents (enforced in app)
CREATE TABLE expense_shares (
  expense_id   BIGINT NOT NULL REFERENCES expenses(expense_id),
  user_id      BIGINT NOT NULL REFERENCES users(user_id),
  share_cents  BIGINT NOT NULL CHECK (share_cents >= 0),
  PRIMARY KEY (expense_id, user_id)
);

-- Settlement payments (cash or digital); also immutable events
CREATE TABLE settlements (
  settlement_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  group_id       BIGINT NOT NULL REFERENCES groups(group_id),
  from_user_id   BIGINT NOT NULL REFERENCES users(user_id),  -- pays
  to_user_id     BIGINT NOT NULL REFERENCES users(user_id),  -- receives
  amount_cents   BIGINT NOT NULL CHECK (amount_cents > 0),
  currency       CHAR(3) NOT NULL CHECK (LENGTH(currency) = 3),
  created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (from_user_id <> to_user_id)
);
```

### Deriving balances

A user's net balance in a group, **per currency** (positive = the group owes them, negative = they owe the group):

```
balance(u) =  Σ amount_cents  of active expenses where payer = u      -- money they fronted
            − Σ share_cents   of their shares on active expenses       -- what they consumed
            + Σ amount_cents  of settlements where from_user = u       -- payments they made
            − Σ amount_cents  of settlements where to_user   = u       -- payments they received
```

Because shares always sum to the expense amount, the balances of all members in a group sum to zero — a useful invariant to assert in tests.

### Splitting an expense

Even splits must reconcile to the cent. Compute a base share and distribute the leftover cents one-per-participant in a deterministic order:

```python
def even_shares(amount_cents: int, participants: list[int]) -> dict[int, int]:
    """Split amount_cents across participants so the shares sum exactly to amount_cents."""
    base, remainder = divmod(amount_cents, len(participants))
    ordered = sorted(participants)  # deterministic remainder allocation
    return {u: base + (1 if i < remainder else 0) for i, u in enumerate(ordered)}
```

Recording an expense is a single atomic write; there are no balances to update:

```python
def add_expense(group_id, payer_id, amount_cents, participants, currency,
                description, custom_shares=None):
    if custom_shares is not None:
        if sum(custom_shares.values()) != amount_cents:
            raise ValueError("custom shares must sum to the expense amount")
        shares = custom_shares
    else:
        # participants is the set who consume; expand from group_members for "everyone"
        shares = even_shares(amount_cents, participants)

    with db.transaction():
        expense_id = db.insert_expense(
            group_id, payer_id, amount_cents, currency, description, created_by=payer_id
        )
        db.insert_expense_shares(expense_id, shares)
    return expense_id
```

Note the payer is just another participant: include them in `participants` if they share the cost, omit them if they don't. Their net position falls out of the balance formula either way.

### Debt simplification (should-have)

Given net balances (which sum to zero), produce a minimal-ish set of transfers by repeatedly matching the largest debtor with the largest creditor:

```python
def simplify(balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """Return (debtor, creditor, cents) transfers that settle the group."""
    debtors   = sorted([u, -b] for u, b in balances.items() if b < 0)
    creditors = sorted([u,  b] for u, b in balances.items() if b > 0)
    transfers, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        transfers.append((debtors[i][0], creditors[j][0], pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers
```
