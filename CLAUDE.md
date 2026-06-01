# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

countbeans is a Telegram bot for tracking and splitting shared expenses within Telegram groups (Splitwise-style, but Telegram-native). Users interact via commands like `/addexpense`, `/balance`, and `/settleup` directly in group chats.

## Commands

All tests run directly on the host. Integration tests use Testcontainers — pytest spins up an ephemeral Postgres container automatically, no manual Docker orchestration needed.

> **Note:** Testcontainers requires working host→container port-forwarding. This is broken on some Docker Desktop + Apple Silicon configurations (symptom: `Can't assign requested address` when connecting to `127.0.0.1`). OrbStack is a reliable alternative on Mac.

```bash
# Install dependencies
uv sync

# Run all tests
uv run pytest

# Run a single test
uv run pytest tests/path/to/test_file.py::test_name
```

```bash
# Development — auto-reloads bot on code changes (uses compose.dev.yml overlay)
docker compose -f compose.yml -f compose.dev.yml up --build

# Production-like — no auto-reload, code baked into image
docker compose up --build

# Start without rebuilding (after first run, no code changes)
docker compose up

# Stop and remove containers (data volume is preserved)
docker compose down

# Wipe everything including the database volume
docker compose down -v
```

## Database migrations (Alembic)

Migrations are **applied as a separate, explicit step** — never auto-run on bot
startup. The `migrate` service in `compose.yml` is profile-gated, so a plain
`docker compose up` starts only `db` + `bot` and never touches the schema. The
image ships `alembic/` and `alembic.ini`, so the migrate step needs no volume
mounts.

**Deploy ordering: migrate first, then start the bot.**

```bash
# Apply all pending migrations (run before deploying/starting the bot)
docker compose run --rm migrate

# Then start the bot
docker compose up -d bot
```

**Authoring a new migration.** Autogenerate compares the ORM models in
`src/countbeans/db/` against the live DB, so the DB must be reachable and at
head first. Because the bot image doesn't contain the `versions/` directory you
are *writing into*, bind-mount `alembic/` and `alembic.ini` when generating (the
applied `migrate` step above does not need this):

```bash
docker compose run --rm \
  -v "$PWD/alembic:/app/alembic" -v "$PWD/alembic.ini:/app/alembic.ini" \
  migrate uv run alembic revision --autogenerate -m "describe change"
```

Then review the generated file in `alembic/versions/`, apply it with
`docker compose run --rm migrate`, and commit it. `alembic check` (no pending
ops) and a `downgrade base` → `upgrade head` round-trip are good sanity checks.

- **Constraint naming:** `Base.metadata` carries a `naming_convention` (see
  `src/countbeans/db/_base.py`), so every PK/FK/unique/check/index gets a
  deterministic name. Name `CheckConstraint`s with the logical suffix only
  (e.g. `amount_positive`) — the convention prefixes `ck_<table>_`.
- **env.py** loads the DSN from `Settings` (never `alembic.ini`) and enables
  `compare_type` / `compare_server_default` so type and default drift is caught.
- **The Compose Postgres is not published to the host.** Run every DB-touching
  command — `migrate`, autogenerate, the bot — through Docker as shown above.

## Architecture

The architectural commitment is a **standalone, framework-agnostic service core** (`countbeans.services`, the "Expense manager") that owns all database access and knows nothing about Telegram or HTTP. Everything else is a **thin adapter** over it. Today there is exactly one adapter — the `aiogram` bot — calling the core **in-process** (same process, plain Python function calls, no network hop). An HTTP layer is **deferred** (see "The HTTP layer is deferred" below), not part of the current build.

```
Telegram  ──long-poll──▶  aiogram bot   (single process, single event loop)
                               │  parses grammar, FSM (multi-step state),
                               │  getChatMember / getChatMemberCount, formats replies
                               ▼
                    countbeans.services   ("Expense manager")
                    stateless, transactional; in-process call; returns DTOs
                               │
                    SQLAlchemy + asyncpg  ──▶  PostgreSQL

  ┄ deferred / optional, add only when a need is real (see below): ┄
  Telegram ──webhook──▶ HTTP shell (FastAPI or aiogram's aiohttp) ──▶ same services
```

The layers and their responsibilities:

- **Service core** (`src/countbeans/services/`, the "Expense manager"): the **only** place that issues SQL against the database (the `UnitOfWork` and repositories live here — see "Database sessions" below). Validates and records expenses/settlements as ledger events, computes derived balances, runs `simplify()`. **Stateless** (no per-conversation state) and **transactional** (one SQLAlchemy transaction per command). Accepts and returns plain **Pydantic DTOs** — never `aiogram` or HTTP request/response types — so it has no knowledge of who called it.
- **Bot layer** (`src/countbeans/main.py` + handlers, `aiogram`): a thin Telegram adapter and the **only runtime entry point today**. Parses command grammar (amount, description, participants), holds **all multi-step conversational state in aiogram's FSM** (the `@all` coverage confirm, admin-gated `/simplify` flows), makes Telegram-only calls (`getChatMember`, `getChatMemberCount`), calls the service core in-process, and formats replies. Owns nothing the service core owns.

**Why in-process, not HTTP-between-layers:** this is an append-only **money ledger** (every expense must reconcile exactly). An in-process call is one transaction boundary with unambiguous success/failure; an HTTP hop would force idempotency keys to avoid double-recording an expense on a timeout/retry — real complexity for no benefit, since web/mobile is explicitly out of scope and a Telegram expense bot never needs the bot and logic to scale apart. This rule holds even if an HTTP shell is added later: it sits *beside* the bot as another adapter, never *between* the bot and the ledger logic.

**Running it / deployment.** There is no separate "server" process — **the bot *is* the runtime.** Start it with `uv run countbeans` (wired via `[project.scripts]` → `countbeans.main:main`; equivalently `uv run python -m countbeans` through `__main__.py`). It runs on **long-polling** (`dp.start_polling`, exactly what `main.py` does today) with **no inbound HTTP server at all**, so for the Telegram-only, low-traffic scope this single process is the whole production runtime — deploy it as **one supervised, always-on process** (a systemd unit, or a container with a restart policy), not behind a load balancer; there is nothing else to deploy. **Single-instance constraint:** Telegram permits only one `getUpdates` consumer per bot token, so exactly **one** poller may run at a time — a second concurrent instance gets HTTP `409 Conflict`. Long-polling therefore can't be horizontally scaled or run blue/green; needing multiple replicas or zero-downtime deploys is itself one of the triggers to adopt the deferred **webhook** shell (which *can* sit behind a load balancer).

**The HTTP layer is deferred.** Web/mobile is explicitly *Won't-have* and the bot is low-traffic, so there is **no HTTP server and no FastAPI**: the `fastapi`/`uvicorn` dependencies have been **removed** from `pyproject.toml`, and `src/countbeans/apis/` is not built. Add an HTTP shell — re-adding the relevant deps (`fastapi`/`uvicorn`, or just `aiohttp` via aiogram) at that point — **only** when a concrete need appears, and even then it's an *additive* adapter over the existing service core, never a rewrite:
  - **Webhooks** (lower latency / no long-poll connection at scale) — webhooks need an inbound HTTP server (long-polling does not); host them on aiogram's own aiohttp server *or* FastAPI.
  - **Ops probes** (`/healthz`, readiness) — only if the deploy target probes HTTP for liveness; otherwise a polling process needs none.
  - **A non-Telegram client** (admin dashboard, web UI) — would wrap the *same* services as a second thin, stateless adapter; nothing in the core changes.

**Naming & cross-layer flow.** The Product Spec below says "**the bot**" to mean the product as a whole, for readability — it is *not* a claim that the bot adapter does the work itself. Concretely, every **database read or write** named anywhere in this spec — recording an expense, the onboarding upsert into `users`/`group_members`, **claiming** a placeholder, deriving balances, running `simplify()` — is performed by the **service core**, the only layer that issues SQL (the bot may *demarcate* a transaction via the `UnitOfWork` — see "Database sessions" — but runs no queries itself). The bot adapter only **parses, holds FSM state, calls Telegram APIs** (`getChatMember`, `getChatMemberCount`), and **formats replies**. So a typical command flows: *bot parses + checks Telegram* → *service core validates, writes, and derives in one transaction* → *bot formats the reply*. The cross-layer cases are the same shape — e.g. the `@all` **coverage check** combines a bot-layer `getChatMemberCount` with a service-core `known` count, and the bot's FSM drives block-until-confirmed before the service core records anything.

All layers share the config in `src/countbeans/config/` for settings (see below).

**Planned data layer**: PostgreSQL via SQLAlchemy + asyncpg, with Alembic for migrations. The schema (users, groups, group_members, expenses, expense_shares, settlements) is specified in the Product Spec below but not yet implemented. Balances are **derived** from the ledger, not stored.

**Database sessions — caller-managed Unit of Work.** There is no DI framework (no FastAPI `Depends`); session lifecycle is handled explicitly via a **caller-managed Unit of Work**. The session is *passed into* service functions, not opened by them — the transaction boundary lives one level above the operation, which is what enables atomic multi-op composition and rollback-per-test.

- **The service core defines a `UnitOfWork`** — an async context manager that wraps the `async_sessionmaker`, opens one `AsyncSession` + transaction, exposes the repositories (`uow.expenses`, `uow.users`, …), and **commits on clean exit / rolls back on exception**. It is the *only* type holding SQLAlchemy objects and exposes none of them, so callers never import `AsyncSession`/`select`.
- **Service / use-case functions take the `UnitOfWork` explicitly** as their first argument and **never commit** — they only do work and return DTOs (`async def add_expense(uow, cmd) -> ExpenseCreatedResult`). Commit/rollback is owned by whoever opened the UoW. This is the rule that makes several ops compose into one atomic transaction and lets tests roll back.
- **Composition root** (`main.py`): build the engine and `async_sessionmaker(engine, expire_on_commit=False)` **once** at startup, wrap them in a `uow_factory` (a callable returning a fresh `UnitOfWork`), hand that factory to the bot via aiogram's DI (`dp["uow_factory"] = …`), and `await engine.dispose()` on shutdown.
- **The thin transactional wrapper at the call site is an aiogram middleware**: it opens one UoW per update, puts it in `data["uow"]`, and commits / rolls back around the handler — so **one transaction per command** falls out of the middleware boundary and handlers never write `async with`. The handler receives `uow` and passes it **explicitly** into service calls (`await add_expense(data["uow"], cmd)`). (A `@transactional` decorator over a service facade is an equivalent wrapper if per-handler control is preferred.)
- **Tests** construct a `UnitOfWork` over a transaction and roll it back at the end — service functions run with no commits, fully isolated and fast, with no aiogram or running bot in the loop.

The deliberate trade vs. each service method opening its own session: the boundary sits in the wrapper (middleware/test), *above* the operation, so the bot adapter **demarcates** the transaction (through the `UnitOfWork` abstraction, never raw SQLAlchemy) even though all SQL stays in the core. The two-phase `@all` flow is, correctly, **two** commands → two UoWs → two transactions: read the `known` count, FSM-confirm, then record.

**Pydantic DTOs — shared vocabulary in `countbeans.dto`.** The service core accepts and returns plain Pydantic models — never `aiogram` types, never SQLAlchemy ORM rows, never raw dicts. These live in a dedicated **`src/countbeans/dto/`** package so both the bot layer and the service core can import them without either depending on the other's internals. Three sub-modules:

- **`dto/commands.py` — inbound to the service core.** One class per mutating operation, carrying everything the core needs, with no Telegram types: `AddExpenseCommand`, `SettleUpCommand`, `OnboardUserCommand`. The bot parses the raw Telegram message and constructs one of these — the handoff point between layers.
- **`dto/results.py` — outbound from the service core after a write.** Confirmations returned to the bot for reply formatting: `ExpenseCreatedResult`, `SettlementCreatedResult`. Carry only what the bot needs to format a reply (IDs, computed cents, participant list) — not full ledger rows.
- **`dto/domain.py` — read-side representations.** Derived views returned by queries: `MemberBalance`, `Transfer`, `GroupSummary`. Used by `/balance` and `/group` responses. `Transfer` is what `simplify()` returns: `(debtor_id, creditor_id, amount_cents)`.

**Conventions that apply to every DTO:**

- `model_config = ConfigDict(frozen=True)` on every class — DTOs are immutable by construction, preventing accidental mutation after the service returns them and enabling use as dict keys.
- **Money fields are always `int` (cents), never `float` or `Decimal`.** The spec rule ("integer minor units, never float") is enforced by the type. Formatting to a display string (e.g. `"$12.50"`) happens in the bot layer, never in a DTO.
- **IDs are `uuid.UUID`.** The bot never constructs raw UUID strings — UUIDs are generated in the service core via `uuid_utils.uuid7()` and surfaced to the bot as typed fields.
- **Currency is `str` (ISO 4217, 3 chars).** No currency enum for now — a plain `str` keeps the DTO portable and avoids the overhead of a registry for a single default currency.
- **No `from_attributes=True`.** DTOs are **not** built directly from ORM rows via `model_validate(row)` — that would tie DTO field names to ORM attribute names. Instead, each repository method maps rows to DTOs explicitly (`_to_dto(row) -> SomeResult`). The ORM schema can evolve without the DTO shape changing, and the boundary stays airtight.
- **Validation at construction.** Pydantic validators on commands enforce the same rules as the prose spec: `amount_cents > 0`, `from_user_id != to_user_id` on `SettleUpCommand`, non-empty `participants`. This gives one deterministic place where bad input is rejected — before it ever reaches a service function.

**Dependency direction:** `countbeans.dto` has no imports from `services`, `main`, or `aiogram` — it is a leaf package. `services` imports from `dto`; the bot layer imports from `dto`; neither imports from the other.

## Commits

Use gitmoji prefixes for all commits. See https://gitmoji.dev/ for the full reference. Examples: 🎉 init, 🔧 config/tooling, ✨ new feature, 🐛 bug fix, ♻️ refactor, 🗑️ remove code/files, 📦 dependencies.

## Settings

All config lives in `src/countbeans/config/core.py` using `pydantic-settings`. Environment variables must be prefixed with `COUNTBEANS_`:

| Env var | Type | Description |
|---|---|---|
| `COUNTBEANS_API_ID` | `int` | Telegram API ID |
| `COUNTBEANS_API_HASH` | `str` | Telegram API hash |
| `COUNTBEANS_BOT_TOKEN` | `str` | Telegram bot token |
| `COUNTBEANS_DATABASE_URL` | `str` | SQLAlchemy async DSN, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `COUNTBEANS_LOG_LEVEL` | `str` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

All fields are required — the app will raise a `ValidationError` at startup if any are missing. Use a `.env` file at the project root (copy `.env.example` and fill in the values). When running via Docker Compose, `COUNTBEANS_DATABASE_URL` is injected automatically by `compose.yml`; the other three must be present in `.env`.

> **TODO (before production deploy): set up proper secret management for DB credentials.** The current `compose.yml` hardcodes the Postgres username and password — fine for local dev since the DB is not exposed to the internet, but not for a real server. The right approach depends on the deploy target: Railway/Render/Fly.io provision Postgres and inject `DATABASE_URL` automatically (nothing to do); a VPS needs either a `.env` created over SSH or Docker Secrets; larger setups use a secrets manager (AWS Secrets Manager, Vault, etc.). No code changes are needed — `pydantic-settings` reads from the environment regardless of where the value comes from. Revisit this when the deployment target is chosen.

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

- *Must-have*: group expense tracking; basic expense input and derived balances; persistent storage; per-user **and whole-group** balance summaries; settling up (full **or partial** payments); **debt simplification (a reduced set of transfers to settle a group), as a per-group setting an admin can toggle on or off**.
- *Should-have*: uneven splits (exact amounts, percentages, weights) and selecting a subset of the group; multi-currency support.
- *Could-have*: expense categories; notifications for outstanding debts; multiple payers per expense; a group info command surfacing membership, coverage gap, and activity.
- *Won't-have*: any web or mobile interface outside Telegram.

### Key commands

- `/addexpense <amount> "<desc>" [@user …]` — record an expense. Splits among the named users plus the payer; omit mentions (or use `@all`) to split with the whole group. Per-user suffixes pick the split mode — `@a:30` exact amount, `@a:60%` percentage, `@a:2x` weight (see "Splitting an expense").
- `/balance [all]` — `/balance` shows the caller's own net position with other members; `/balance all` shows **every member's** net balance (per currency) plus the suggested settle-up transfers. Both are derived from the ledger. The suggested transfers honor the group's **debt-simplification setting**: when on, they are the simplified (reduced) set; when off, they are the raw pairwise debts. The per-member net balances are identical either way — the toggle only changes how the *suggested transfers* are presented.
- `/settleup` — record a settlement payment (full or partial) from one user to another, e.g. `/settleup @user1 20`.
- `/simplify [on|off]` — view or change the group's debt-simplification setting. `/simplify` with no argument reports the current state (any member). `/simplify on` / `/simplify off` flips it and is **admin-only**: the bot checks the caller via `getChatMember` and refuses (no state change) unless their status is `creator` or `administrator`. The setting is purely presentational — see "Debt simplification".
- `/group` — show group info: name, default currency, and the **debt-simplification setting** (on/off); the **known members** the bot can split among, with pending placeholders flagged separately (mentioned but not yet `/start`-ed); the **coverage gap** (`known` vs `getChatMemberCount`) so people can see who still needs to join; and a quick activity summary (active expenses and total tracked, per currency).

### Components

These are **layered adapters over one shared service core**, all in a single process — see "Architecture" above for the interaction model and the rationale for keeping the boundary in-process rather than over HTTP.

- **Telegram bot** (`aiogram`) — listens for commands/messages in groups, parses command structure and parameters (amount, description, participants), and sends confirmation/balance responses back to the group. **Owns all multi-step interaction state** via aiogram's FSM (e.g. the `@all` coverage confirm and admin-gated `/simplify` flows) and makes Telegram-only calls (`getChatMember`, `getChatMemberCount`). Calls the Expense manager directly (in-process); runs no SQL itself (a middleware may open/commit a `UnitOfWork` per update — see "Database sessions" — but the bot issues no queries).
- **HTTP shell** (deferred — FastAPI or aiogram's aiohttp) — **not part of the current build.** The runtime today is the bot on long-polling with no inbound HTTP server. If one is added later (for webhooks, a `/healthz` probe, or a non-Telegram client), it is a *second* thin, **stateless** adapter over the same Expense manager — no per-conversation state (that lives in the bot's FSM), no business logic of its own, and never an HTTP hop between the bot and the ledger. See "Architecture".
- **Expense manager** (`countbeans.services`) — the **only** component that talks to the database. Validates and records expenses and settlements as ledger events, computes derived balances (and, optionally, a simplified set of transfers). Stateless and transactional (one transaction per command); accepts/returns Pydantic DTOs, with no knowledge of Telegram or HTTP.
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
  -- Debt-simplification toggle (admin-only via /simplify). Purely a display
  -- preference: it changes how /balance all *suggests* transfers, never the
  -- ledger or derived balances, so flipping it any number of times is safe.
  simplify_debts    BOOLEAN NOT NULL DEFAULT FALSE,
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
- **Everyone:** with no mentions (or an `@all` keyword), split across all current members from `group_members` (pending placeholders included). Because the bot can't enumerate the real roster, `@all` means "everyone the bot knows" — see the coverage check below.
- **Excluding the payer:** the payer is included by default; excluding them (they paid but didn't partake) just drops their share, leaving them owed the full amount.

**Split modes** — dividing the amount unevenly:

| Mode | Command example | Rule |
|---|---|---|
| Equal | `/addexpense 60 Dinner @a @b` | even split across participants |
| Exact | `/addexpense 50 Dinner @a:30 @b:20` | per-person cents; must sum to the amount |
| Percentage | `/addexpense 50 Dinner @a:60% @b:40%` | percentages must sum to 100 |
| Weighted | `/addexpense 50 Dinner @a:2x @b:1x` | split in proportion to integer weights |

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

### Command parsing & validation

`/addexpense` grammar:

```
/addexpense <amount> <description> [<participant> ...]
<participant> ::= "@" handle [ ":" <suffix> ]
<suffix>      ::= number          ; exact amount   e.g. @a:30
                | number "%"      ; percentage     e.g. @a:60%
                | number "x"      ; weight/shares  e.g. @a:2x
```

Parse first, then validate:

1. **Amount** — positive, at most 2 decimal places, parsed to integer cents **from the string** (never via `float`). Reject `0`, negatives, and >2 dp. Currency is the group default.
2. **Description** — a quoted `"…"` string, or the run of words between the amount and the first `@mention`. Use quotes if it contains `@`. May be empty.
3. **One mode per command, inferred from the suffixes:**
   - No suffix on any mention → **equal**.
   - All suffixes end in `%` → **percentage**; must sum to 100.
   - All suffixes end in `x` → **weighted**.
   - All suffixes are bare numbers → **exact**; must sum to the amount.
   - **Mixing families is rejected** (`@a:30 @b:40%` → error), and in any non-equal mode **every** participant must carry a suffix of that family (`@a:60% @b` → error).
4. **Participants** — duplicate handles are rejected. The payer is added automatically (deduped if they also @mention themselves); an explicit exclude flag drops the payer's share.
5. **`@all`** — must appear alone with no suffixes; splits equally across all current `group_members` (placeholders included). Omitting mentions entirely is equivalent to `@all`. Since the bot cannot enumerate the real roster, it runs a **coverage check** and **blocks until confirmed** on any gap (see below).
6. **Unknown handles are not errors** — they become pending placeholders (see Onboarding).
7. Fractional percentages (≤2 dp) are carried as integer **basis points** (sum must be 10000) so `apportion` stays integer-only.

`/settleup @user <amount>` — `<amount>` follows rule 1; `@user` must resolve to a participant (or becomes a placeholder); settling with yourself is rejected.

**`@all` coverage check (block-until-confirmed).** The bot can't list members, but `getChatMemberCount` (available to any bot) tells it how many there *are*. At `@all` time compare the count it can split among against the real count:

```
known  = active rows in group_members      # who the bot can actually split among
actual = getChatMemberCount() - 1          # minus the bot itself
```

- `known == actual` → the bot demonstrably knows everyone; record the expense.
- `known <  actual` → there are members the bot has never seen and **cannot name**. **Do not record.** Reply with who *would* be included and the gap ("splitting among these 3; this group has 5 — 2 I haven't seen yet"), and require the payer to either confirm "just these people" or have the missing members `/start` first. The only way to actually include an unseen member is for them to interact — there is no API to pull them in (admin's `chat_member` stream captures *future* joiners, never pre-existing silent ones).

### Debt simplification (must-have, per-group toggle)

Given net balances (which sum to zero), produce a minimal-ish set of transfers by repeatedly matching the largest debtor with the largest creditor:

```python
def simplify(balances: dict[Id, int]) -> list[tuple[Id, Id, int]]:
    """Return (debtor, creditor, cents) transfers that settle the group.
    Sort by amount descending (largest debtor vs largest creditor) so the
    transfer count stays low; tie-break by id to keep the output deterministic.
    NB: sort by amount, not id — plain sorted() orders by id first and inflates
    the transfer count, defeating the point of simplification."""
    debtors   = sorted(([u, -b] for u, b in balances.items() if b < 0), key=lambda x: (-x[1], x[0]))
    creditors = sorted(([u,  b] for u, b in balances.items() if b > 0), key=lambda x: (-x[1], x[0]))
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

This is a heuristic, not a true optimum: the provably-minimum transfer set is NP-complete (it reduces from subset-sum). Largest-debtor-vs-largest-creditor greedy gets a good, deterministic reduction cheaply, which is the right trade here — hence "minimal-ish," never "minimal." Even Splitwise's own "simplify debts" is a knowingly-suboptimal greedy of this kind (see https://antoncao.me/blog/splitwise), so matching that bar is the deliberate choice, not a shortcut. Whatever it returns is always a *valid* settlement (every balance zeroes out), because balances sum to zero and each transfer clears at least one side.

> **Flagged future optimization — the `n − k` decomposition (NOT implemented).** The exact minimum number of transfers is `n − k`, where `n` is the count of members with a nonzero balance and `k` is the largest number of **disjoint subgroups whose balances each net to zero**. Every such zero-sum subgroup settles entirely within itself (a group of size `s` needs `s − 1` transfers), so more independent subgroups means fewer transfers — greedy's worst case is the `k = 1` end (`n − 1`). The catch: finding the maximum `k` *is* the NP-complete core (the subset-sum wall itself, cf. "Optimal Account Balancing"), so this is **not a free win over greedy** — it's the hard problem, just named. It is, however, tractable at the `n` a Telegram chat actually has: for small groups a bounded subset-sum / backtracking search could compute the exact optimum if transfer counts ever look bloated in practice. Recorded here as a deliberate option, not a requirement — greedy already delivers the must-have (a valid, deterministic, reduced settlement). Any such optimizer must still obey **presentation-only**: compute at read time, write nothing to the ledger.

**A per-group toggle, admin-only.** Simplification is controlled by `groups.simplify_debts`, flipped with `/simplify on|off`. Only a group **admin** may change it: on a set request the bot calls `getChatMember(chat_id, caller_id)` and proceeds only if the caller's status is `creator` or `administrator`; otherwise it refuses and leaves the setting untouched. (This is independent of the bot's *own* required admin rights — see Onboarding.) Reading the setting (`/simplify` with no argument, or `/group`) is open to any member. The setting defaults to **off**: a new group sees raw pairwise debts until an admin deliberately opts in, which is the least-surprising default for a money bot — with simplification on you may be asked to pay someone you never directly transacted with, and that should be a choice the group makes.

**The toggle is presentation-only; balances never move.** This is the design rule that makes flipping it safe any number of times. The single source of truth is the append-only ledger (expenses, expense_shares, settlements); every net balance is **derived** from that ledger on read (see "Deriving balances") and `simplify()` is a *pure function of those balances* that returns suggested transfers. Simplification therefore:

- **writes nothing** — it never inserts settlements, never voids or mutates anything, never persists its output. It runs at read time for `/balance all` and is thrown away.
- **changes only the suggested-transfer view** — `simplify_debts = true` renders the reduced transfer set; `false` renders the raw pairwise debts. The per-member net balances `/balance all` prints are computed straight from the ledger and are byte-for-byte identical under either setting.

Because the toggle touches no rows that feed the balance formula, toggling on → off → on → … leaves balances exactly where they were. There is **no "apply simplification" step** that nets the ledger down; that would be the one implementation that *could* corrupt balances on toggle, and it is deliberately excluded. The invariant to assert in tests: for any ledger and any sequence of `simplify_debts` flips, each member's derived balance equals what the toggle-free ledger derives — i.e. **equality against the never-toggled baseline** is the load-bearing assertion. Do **not** assert only "balances sum to zero": every settlement moves `+x`/`−x`, so the sum stays zero even when individual balances are wrong, which means a materialize-on-toggle bug can corrupt balances while still passing a sum-zero check. Sum-zero is a sanity check, never the proof of accuracy.

**Acting on a suggestion is a normal settlement.** When a user runs `/settleup` after seeing a simplified suggestion (e.g. "A pays C 30"), that records an ordinary settlement event between the actual `from`/`to` users — a real transfer of obligation, no different from any other settlement. It is a genuine ledger fact, not a materialization of the simplification, so later toggling the setting off (and showing raw pairwise debts again) still yields correct, consistent balances. Suggestions are advisory; only `/settleup` moves money in the ledger.
