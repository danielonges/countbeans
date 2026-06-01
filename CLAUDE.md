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
- **FastAPI server** (`src/countbeans/apis/`): HTTP API layer (planned; not yet implemented).

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
- **Surrogate PKs are UUID7, generated in the app layer.** UUID7 is time-ordered so B-tree indexes stay sequential (no fragmentation), IDs are non-enumerable (no `id=1,2,3` scraping), and there is no collision risk if data is ever merged across instances. Use `uuid_utils.uuid7()` (Rust-backed). `groups.telegram_chat_id` and `users.telegram_user_id` are Telegram-assigned `BIGINT`s and stay that way.
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
- *Should-have*: uneven splits (exact amounts, percentages, weights) and selecting a subset of the group; debt simplification (minimal set of transfers to settle a group); multi-currency support.
- *Could-have*: expense categories; notifications for outstanding debts; multiple payers per expense.
- *Won't-have*: any web or mobile interface outside Telegram.

### Key commands

- `/addexpense <amount> "<desc>" [@user …]` — record an expense. Splits among the named users plus the payer; omit mentions (or use `@all`) to split with the whole group. Per-user suffixes pick the split mode — `@a:30` exact amount, `@a:60%` percentage, `@a:2` weight (see "Splitting an expense").
- `/balance` — show the caller's net balance with other group members (derived from the ledger).
- `/settleup` — record a settlement payment (full or partial) from one user to another, e.g. `/settleup @user1 20`.

### Components

- **Telegram bot** — listens for commands/messages in groups, parses command structure and parameters (amount, description, participants), and sends confirmation/balance responses back to the group.
- **Bot server** — FastAPI backend that processes parsed commands, manages multi-step interaction state, handles error cases (missing data, bad formatting), and talks to the database.
- **Expense manager** — validates and records expenses and settlements as ledger events, and computes derived balances (and, optionally, a simplified set of transfers).
- **Database** — persists users, group membership, and the immutable ledger of expenses, expense shares, and settlements.

### Onboarding & membership

**Platform constraint:** a Bot-API bot cannot enumerate a group's members at any permission level — there is no roster API. The bot only learns a user exists when that user *interacts* with it, or when it is named in a command. This shapes the whole onboarding model.

**Implicit self-onboarding.** There is no explicit join ceremony. The first time a user issues any command, the bot upserts them into `users` and `group_members`, capturing `telegram_user_id`, `username`, and names from the update's `from` field. Interaction doubles as consent to be tracked in a financial ledger.

**Mentioned-but-unseen participants (placeholders).** A split may name someone (`@bob`) the bot has never seen, and the Bot API cannot reliably resolve a bare `@username` to a user ID. So a mention of an unknown handle creates a **pending placeholder** — a `users` row with `telegram_user_id IS NULL`, known only by its `username`. When that person later interacts, the bot has their real Telegram ID and **claims** the placeholder by setting `telegram_user_id` on that same row. Because every table references the surrogate `users.id`, all their existing shares and settlements bind to the now-real identity automatically — claiming is a single-row `UPDATE`, no fan-out rewrite.

**The bot requires admin rights.** Admin status does *not* unlock a member roster — enumeration is impossible at every permission level, so the core onboarding model (implicit self-onboarding + placeholders) is unchanged. What admin buys is **accurate membership going forward** and freeform input:

- It always receives `my_chat_member` (added/removed/promoted), so it can detect its own status, create the `groups` row, and post a welcome.
- As an admin it receives the rich `chat_member` join/leave/ban stream (opt in via `allowed_updates`), so `group_members` can be kept **accurate from the event stream** — set a row on join, set `left_at` on leave — rather than drifting.
- Privacy mode is off for admin bots: the bot sees all group messages, not just commands/replies/@mentions. This enables freeform expense parsing later, but note the privacy implication — a financial bot now sees all chatter.

**Enforcement.** On `my_chat_member` (and by checking `getChatMember` for the bot itself), if the bot is not an administrator it posts a message asking to be promoted and **refuses to process commands until it is**. This is the trade for accuracy: a heavier, scarier install for a money bot, accepted deliberately.

**Membership at split time.** With the `chat_member` stream maintaining `group_members`, "split evenly among everyone" can trust it. A `getChatMember` check at split time remains cheap insurance against missed events, but is no longer load-bearing.

**Why a surrogate key (not the Telegram ID).** The Telegram user ID is the only stable, unique, permanent identifier — usernames are optional, mutable, and reusable, so keying on them would silently split or merge identities on rename/reuse (a money bug) and couldn't represent username-less users at all. But placeholders have *no* Telegram ID yet. The surrogate `users.id` squares this: it's the uniform key everything references, while `telegram_user_id` starts NULL (pending) and is filled in on claim. Treat `username` strictly as a display alias and placeholder match hint, never as identity.

### Schema (PostgreSQL)

There is intentionally **no `debts` table** — balances are derived (see below). Edits/deletes are done by setting `voided_at`, not by mutating rows.

```sql
-- Identities. Surrogate `id` is the stable key everything references, so a
-- placeholder can be "claimed" later by just filling in telegram_user_id —
-- no foreign keys need rewriting.
--   * telegram_user_id IS NULL  -> pending placeholder (known only by @username)
--   * telegram_user_id IS NOT NULL -> claimed; this is the trustworthy identity
-- username is a mutable display alias and a match hint, never an identity.
-- App invariant: at most one pending placeholder per username.
CREATE TABLE users (
  id                UUID PRIMARY KEY,        -- UUID7, generated in app layer
  telegram_user_id  BIGINT UNIQUE,           -- NULL until the placeholder is claimed
  username          VARCHAR(255),
  first_name        VARCHAR(255),
  last_name         VARCHAR(255)
);

-- Telegram groups the bot is in
CREATE TABLE groups (
  id                UUID PRIMARY KEY,       -- UUID7, generated in app layer
  telegram_chat_id  BIGINT UNIQUE NOT NULL, -- Telegram chat ID
  group_name        VARCHAR(255),
  default_currency  CHAR(3) NOT NULL DEFAULT 'USD',   -- ISO 4217
  CHECK (LENGTH(default_currency) = 3)
);

-- Membership, so we can split "evenly among everyone in the group".
-- PK includes joined_at so that a user who leaves and rejoins can be
-- represented as a new membership period without losing history.
CREATE TABLE group_members (
  group_id   UUID NOT NULL REFERENCES groups(id),
  user_id    UUID NOT NULL REFERENCES users(id),
  joined_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  left_at    TIMESTAMP WITH TIME ZONE,    -- NULL = still a member
  PRIMARY KEY (group_id, user_id, joined_at)
);

-- Immutable expense events; soft-deleted via voided_at
CREATE TABLE expenses (
  expense_id    UUID PRIMARY KEY,            -- UUID7, generated in app layer
  group_id      UUID NOT NULL REFERENCES groups(id),
  payer_id      UUID NOT NULL REFERENCES users(id),
  amount_cents  BIGINT NOT NULL CHECK (amount_cents > 0),   -- integer minor units
  currency      CHAR(3) NOT NULL CHECK (LENGTH(currency) = 3),
  description   VARCHAR(255),
  created_by    UUID NOT NULL REFERENCES users(id),
  created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  voided_at     TIMESTAMP WITH TIME ZONE,                   -- NULL = active
  voided_by     UUID REFERENCES users(id)
);

-- Per-participant shares; MUST sum to expenses.amount_cents (enforced in app)
CREATE TABLE expense_shares (
  expense_id   UUID NOT NULL REFERENCES expenses(expense_id),
  user_id      UUID NOT NULL REFERENCES users(id),
  share_cents  BIGINT NOT NULL CHECK (share_cents >= 0),
  PRIMARY KEY (expense_id, user_id)
);

-- Settlement payments (cash or digital); also immutable events
CREATE TABLE settlements (
  settlement_id  UUID PRIMARY KEY,           -- UUID7, generated in app layer
  group_id       UUID NOT NULL REFERENCES groups(id),
  from_user_id   UUID NOT NULL REFERENCES users(id),    -- pays
  to_user_id     UUID NOT NULL REFERENCES users(id),    -- receives
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

A split is two independent choices: **who** is in it (participant selection) and **how** the amount is divided among them (split mode). The only universal rule is that **shares sum exactly to `amount_cents`**. None of this touches the schema — every mode just writes `expense_shares` rows, and a non-participant simply has no row.

**Participant selection** — splitting with only some of the group:

- **Named subset (default):** `/addexpense 50 Dinner @a @b` splits among the named users **plus the payer**. This is how you split with only selected people — only those listed (and the payer) get a share.
- **Everyone:** with no mentions (or an `@all` keyword), split across all current members from `group_members`, validated at split time via `getChatMember` (see Onboarding).
- **Excluding the payer:** the payer is included by default; excluding them (they paid but didn't partake) just drops their share, leaving them owed the full amount.

**Split modes** — dividing the amount unevenly:

| Mode | Command example | Rule |
|---|---|---|
| Equal | `/addexpense 60 Dinner @a @b` | even split across participants |
| Exact | `/addexpense 50 Dinner @a:30 @b:20` | per-person cents; must sum to the amount |
| Percentage | `/addexpense 50 Dinner @a:60% @b:40%` | percentages must sum to 100 |
| Weighted | `/addexpense 50 Dinner @a:2 @b:1` | split in proportion to integer weights |

Equal, percentage, and weighted splits are the *same* operation — apportion the amount in proportion to integer weights — using the **largest-remainder method** so the cents always reconcile. Exact mode skips apportionment and takes the given cents after validating their sum.

```python
def apportion(amount_cents: int, weights: dict[Id, int]) -> dict[Id, int]:
    """Split amount_cents in proportion to integer weights, summing exactly to
    amount_cents (largest-remainder method)."""
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    shares, remainders, allocated = {}, [], 0
    for k, w in weights.items():
        exact = amount_cents * w
        shares[k] = exact // total           # floor
        allocated += shares[k]
        remainders.append((exact % total, k))
    # hand the leftover cents to the largest remainders (deterministic tie-break by id)
    remainders.sort(key=lambda r: (-r[0], r[1]))
    for _, k in remainders[: amount_cents - allocated]:
        shares[k] += 1
    return shares


def compute_shares(amount_cents, participants, mode="equal", params=None):
    match mode:
        case "equal":
            return apportion(amount_cents, {u: 1 for u in participants})
        case "weighted":                         # params: {id: weight}
            return apportion(amount_cents, params)
        case "percent":                          # params: {id: percent}
            if sum(params.values()) != 100:
                raise ValueError("percentages must sum to 100")
            return apportion(amount_cents, params)
        case "exact":                            # params: {id: cents}
            if sum(params.values()) != amount_cents:
                raise ValueError("exact shares must sum to the expense amount")
            return params
```

Equal split is just unit weights; percentage and weighted splits pass the percentages/weights straight through, since `apportion` is scale-invariant. Recording the expense is then a single atomic write — there are no balances to update:

```python
def add_expense(group_id, payer_id, amount_cents, currency, description,
                participants, mode="equal", params=None):
    shares = compute_shares(amount_cents, participants, mode, params)
    with db.transaction():
        expense_id = db.insert_expense(
            group_id, payer_id, amount_cents, currency, description, created_by=payer_id
        )
        db.insert_expense_shares(expense_id, shares)
    return expense_id
```

The payer is just another participant: their share is computed like anyone else's, and their net position (paid − consumed) falls out of the balance formula. Excluding the payer simply means they get no `expense_shares` row.

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
