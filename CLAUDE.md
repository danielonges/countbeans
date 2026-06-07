# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

countbeans is a Telegram bot for tracking and splitting shared expenses within Telegram groups (Splitwise-style, but Telegram-native). Users interact via commands like `/addexpense`, `/balance`, and `/settleup` directly in group chats.

## Commands

**Unit tests** run on the host. **Integration tests** need Postgres and run inside the Compose network (the `test` service against an ephemeral `test-db`) — reaching the DB container directly, which sidesteps host→container port-forwarding (broken on some Docker Desktop + Apple Silicon setups: `Can't assign requested address` on `127.0.0.1`). They get their DSN from `TEST_DATABASE_URL`; when it's unset, `pytest` **skips** them.

```bash
# Install dependencies
uv sync

# Install the pre-commit hook (once per clone) — runs pyright before each
# commit. .pre-commit-config.yaml is committed, but the installed hook in
# .git/hooks is not, so every fresh clone must run this or the hook won't fire.
uv run pre-commit install

# Unit tests on the host (integration tests skip — no TEST_DATABASE_URL)
uv run pytest tests/unit

# Integration tests in Docker (spins up ephemeral test-db, runs the suite)
docker compose --profile test run --rm test

# A single integration test (override the service command)
docker compose --profile test run --rm test uv run --no-sync pytest tests/integrations/test_balance.py::test_name -q

# Tear down the test-db afterwards (tmpfs is wiped)
docker compose --profile test down
```

> **CI:** set `TEST_DATABASE_URL` to a service-container Postgres (or run the Compose `test` service) so integration tests actually run rather than silently skipping.

> **Prefer `make dev` over raw `compose up` for the dev stack.** Migrations are a
> separate, explicit step applied *before* the bot starts (the bot never
> auto-migrates — see "Database migrations" below). `make dev` runs `migrate`
> then brings up the auto-reload stack, so a fresh pull that adds a migration
> can't start the bot against a stale schema (the `column ... does not exist`
> failure). The raw `docker compose ... up` below still works but skips that
> ordering — run `docker compose run --rm migrate` yourself first. See the
> `Makefile` for `dev` / `up` / `migrate` / `down` / `test` targets.

```bash
# Development — migrate first, then the auto-reload stack (preferred)
make dev
# …equivalently, the raw command (does NOT apply pending migrations):
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

> **Rebuild the image before autogenerating, and again before applying a
> brand-new migration.** The image bakes `src/` (and `alembic/`) at build time;
> the commands above bind-mount only `alembic/`, never `src/`. So after editing
> a model, run `docker compose build migrate` first — otherwise autogenerate
> diffs against the *stale baked models* and silently emits an **empty**
> migration (no error, just `pass`). Likewise, the plain `migrate` apply,
> `alembic check`, and the round-trip below don't bind-mount `alembic/`, so they
> won't see a migration file you just wrote until you `docker compose build
> migrate` to bake it in (this is also what production does — the deploy image
> ships migrations baked). A quick `... python -c "from countbeans.db import
> models, _base; print(_base.Base.metadata.tables['<table>'].indexes)"` in the
> container confirms it sees your change.

Then review the generated file in `alembic/versions/`, apply it with
`docker compose run --rm migrate`, and commit it. `alembic check` (no pending
ops) and a `downgrade base` → `upgrade head` round-trip are good sanity checks.
**Partial indexes** (e.g. `postgresql_where=`) autogenerate fine *once the image
is rebuilt* — an empty diff there is the staleness trap above, not an Alembic
limitation.

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
- **Bot layer** (`src/countbeans/bot/` — `server.py` + `handlers/` + `middleware/`, `aiogram`): a thin Telegram adapter and the **only runtime entry point today**. Parses command grammar (amount, description, participants), makes Telegram-only calls (`getChatMember`, `getChatMemberCount`), calls the service core in-process, and formats replies. Owns nothing the service core owns. Cross-cutting concerns live in middleware (see "Bot runtime & middleware" below): a per-update `UnitOfWork`, request-scoped logging context, and an admin gate that refuses group commands until the bot itself is an administrator. The per-command *caller*-admin checks for `/simplify` and `/currency` are separate inline `getChatMember` checks (`bot/utils/permissions.py`). aiogram's FSM is available for multi-step state but **nothing uses it today** — the `@all` coverage check is a non-blocking warning, not a confirm step.

**Why in-process, not HTTP-between-layers:** this is an append-only **money ledger** (every expense must reconcile exactly). An in-process call is one transaction boundary with unambiguous success/failure; an HTTP hop would force idempotency keys to avoid double-recording an expense on a timeout/retry — real complexity for no benefit, since web/mobile is explicitly out of scope and a Telegram expense bot never needs the bot and logic to scale apart. This rule holds even if an HTTP shell is added later: it sits *beside* the bot as another adapter, never *between* the bot and the ledger logic.

**Running it / deployment.** There is no separate "server" process — **the bot *is* the runtime.** Start it with `uv run countbeans` (wired via `[project.scripts]` → `countbeans.main:main`; equivalently `uv run python -m countbeans` through `__main__.py`). `main.py` only sets up logging and calls `bot/server.py`'s `run()` — the actual composition root. It runs on **long-polling** (`dp.start_polling` in `run()`) with **no inbound HTTP server at all**, so for the Telegram-only, low-traffic scope this single process is the whole production runtime — deploy it as **one supervised, always-on process** (a systemd unit, or a container with a restart policy), not behind a load balancer; there is nothing else to deploy. **Single-instance constraint:** Telegram permits only one `getUpdates` consumer per bot token, so exactly **one** poller may run at a time — a second concurrent instance gets HTTP `409 Conflict`. Long-polling therefore can't be horizontally scaled or run blue/green; needing multiple replicas or zero-downtime deploys is itself one of the triggers to adopt the deferred **webhook** shell (which *can* sit behind a load balancer).

**The HTTP layer is deferred.** Web/mobile is explicitly *Won't-have* and the bot is low-traffic, so there is **no HTTP server and no FastAPI**: the `fastapi`/`uvicorn` dependencies have been **removed** from `pyproject.toml`, and `src/countbeans/apis/` is not built. Add an HTTP shell — re-adding the relevant deps (`fastapi`/`uvicorn`, or just `aiohttp` via aiogram) at that point — **only** when a concrete need appears, and even then it's an *additive* adapter over the existing service core, never a rewrite:
  - **Webhooks** (lower latency / no long-poll connection at scale) — webhooks need an inbound HTTP server (long-polling does not); host them on aiogram's own aiohttp server *or* FastAPI.
  - **Ops probes** (`/healthz`, readiness) — only if the deploy target probes HTTP for liveness; otherwise a polling process needs none.
  - **A non-Telegram client** (admin dashboard, web UI) — would wrap the *same* services as a second thin, stateless adapter; nothing in the core changes.

**Naming & cross-layer flow.** The Product Spec below says "**the bot**" to mean the product as a whole, for readability — it is *not* a claim that the bot adapter does the work itself. Concretely, every **database read or write** named anywhere in this spec — recording an expense, the onboarding upsert into `users`/`group_members`, **claiming** a placeholder, deriving balances, running `simplify()` — is performed by the **service core**, the only layer that issues SQL (the bot may *demarcate* a transaction via the `UnitOfWork` — see "Database sessions" — but runs no queries itself). The bot adapter only **parses, holds FSM state, calls Telegram APIs** (`getChatMember`, `getChatMemberCount`), and **formats replies**. So a typical command flows: *bot parses + checks Telegram* → *service core validates, writes, and derives in one transaction* → *bot formats the reply*. The cross-layer cases are the same shape — e.g. the `@all` **coverage check** combines a bot-layer `getChatMemberCount` with a service-core `known` count; the bot records the expense among known members and appends a non-blocking warning when there's a gap (no confirm step).

All layers share the config in `src/countbeans/config/` for settings (see below).

**Bot runtime & middleware (`bot/server.py`).** `run()` is the composition root: it builds the `Dispatcher`, registers middleware, includes the handler routers, publishes the command menus, and starts long-polling. Three middlewares wrap every update, and **their registration order is load-bearing**:

1. **`LoggingContextMiddleware`** — stamps a per-request `request_id` (plus user/chat/command) onto every log line via a `contextvar` (`logging/core.py`); registered first so the id is present on the "transaction opened" line.
2. **`TransactionalMiddleware`** — opens one `UnitOfWork` per update, puts it in `data["uow"]`, and commits / rolls back around the handler. This *is* the "one transaction per command" boundary described under "Database sessions".
3. **`AdminGateMiddleware`** (messages only) — refuses group commands until the **bot itself** is an administrator, reading the durable `groups.bot_is_admin` flag and self-healing with a single `getChatMember(bot)` when it reads false. Registered last so `data["uow"]` exists; it passes through private chats and non-message updates so `/start` and the membership streams are never blocked. (This is distinct from the per-command *caller*-admin checks in `/simplify` and `/currency`.)

The logging + transactional pair also wrap the **membership streams** (`handlers/membership.py`): `my_chat_member` keeps `groups.bot_is_admin` current as the bot is added/promoted/demoted (and posts the welcome / promote-me nudge), while `chat_member` onboards members on join and sets `group_members.left_at` on leave. The `chat_member` stream is only delivered while the bot is an admin, so polling opts into both via `dp.resolve_used_update_types()`.

**Data layer**: PostgreSQL via SQLAlchemy + asyncpg, with Alembic for migrations. The schema — `users`, `groups`, `group_members`, `events`, `event_members`, `expenses`, `expense_shares`, `settlements` — is implemented in `src/countbeans/db/models.py` (the authoritative source; see "Database migrations" above and the schema note at the start of the Product Spec). Balances are **derived** from the ledger, not stored.

**Database sessions — caller-managed Unit of Work.** There is no DI framework (no FastAPI `Depends`); session lifecycle is handled explicitly via a **caller-managed Unit of Work**. The session is *passed into* service functions, not opened by them — the transaction boundary lives one level above the operation, which is what enables atomic multi-op composition and rollback-per-test.

- **The service core defines a `UnitOfWork`** — an async context manager that wraps the `async_sessionmaker`, opens one `AsyncSession` + transaction, exposes the repositories (`uow.expenses`, `uow.users`, …), and **commits on clean exit / rolls back on exception**. It is the *only* type holding SQLAlchemy objects and exposes none of them, so callers never import `AsyncSession`/`select`.
- **Service / use-case functions take the `UnitOfWork` explicitly** as their first argument and **never commit** — they only do work and return DTOs (`async def add_expense(uow, cmd) -> ExpenseCreatedResult`). Commit/rollback is owned by whoever opened the UoW. This is the rule that makes several ops compose into one atomic transaction and lets tests roll back.
- **Composition root** (`bot/server.py`'s `run()`, invoked by `main.py`): build the engine and `async_sessionmaker(engine, expire_on_commit=False)` **once** at startup, wrap them in a `uow_factory` (a callable returning a fresh `UnitOfWork`), pass that factory into `TransactionalMiddleware(uow_factory)`, and `await engine.dispose()` on shutdown.
- **The thin transactional wrapper at the call site is an aiogram middleware**: it opens one UoW per update, puts it in `data["uow"]`, and commits / rolls back around the handler — so **one transaction per command** falls out of the middleware boundary and handlers never write `async with`. The handler receives `uow` and passes it **explicitly** into service calls (`await add_expense(data["uow"], cmd)`). (A `@transactional` decorator over a service facade is an equivalent wrapper if per-handler control is preferred.)
- **Tests** construct a `UnitOfWork` over a transaction and roll it back at the end — service functions run with no commits, fully isolated and fast, with no aiogram or running bot in the loop.

The deliberate trade vs. each service method opening its own session: the boundary sits in the wrapper (middleware/test), *above* the operation, so the bot adapter **demarcates** the transaction (through the `UnitOfWork` abstraction, never raw SQLAlchemy) even though all SQL stays in the core.

**Pydantic DTOs — shared vocabulary in `countbeans.dto`.** The service core accepts and returns plain Pydantic models — never `aiogram` types, never SQLAlchemy ORM rows, never raw dicts. These live in a dedicated **`src/countbeans/dto/`** package so both the bot layer and the service core can import them without either depending on the other's internals. Three sub-modules:

- **`dto/commands.py` — inbound to the service core.** One class per mutating operation, carrying everything the core needs, with no Telegram types: `AddExpenseCommand`, `SettleUpCommand`, `OnboardUserCommand`. `AddExpenseCommand` and `SettleUpCommand` carry an optional `event_id`; event management adds `CreateEventCommand`, `SetActiveEventCommand`, `SetEventStatusCommand`, and `EditEventRosterCommand` (see "Events"). The bot parses the raw Telegram message and constructs one of these — the handoff point between layers.
- **`dto/results.py` — outbound from the service core after a write.** Confirmations returned to the bot for reply formatting: `OnboardResult`, `ExpenseCreatedResult`, `SettlementCreatedResult`, `EventCreatedResult`. Carry only what the bot needs to format a reply (IDs, computed cents, participant list) — not full ledger rows.
- **`dto/domain.py` — read-side representations.** Derived views returned by queries: `MemberBalance`, `Transfer`, `GroupSummary`, `MemberInfo`, `ActivitySummary`, `GroupInfo`, and the `StatementEntry` / `StatementPage` pair. Used by `/balance`, `/group`, and `/statements` responses. `Transfer` is what debt simplification returns — `from_user_id`, `to_user_id`, `amount_cents`, `currency` — computed by `suggested_transfers` / `_simplified_transfers` in `services/balance.py` (the "`simplify()`" the spec refers to; there is no function literally named `simplify`).

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
| `COUNTBEANS_BOT_TOKEN` | `str` | Telegram bot token (from @BotFather) |
| `COUNTBEANS_DATABASE_URL` | `str` | SQLAlchemy async DSN, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `COUNTBEANS_LOG_LEVEL` | `str` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `COUNTBEANS_DB_POOL_PRE_PING` | `bool` | Liveness-check pooled connections on checkout, transparently replacing stale ones (default: `True`) |
| `COUNTBEANS_DB_POOL_RECYCLE_SECONDS` | `int` | Retire pooled connections older than this many seconds; `-1` disables (default: `1800`) |

`BOT_TOKEN` and `DATABASE_URL` are required — the app raises a `ValidationError` at startup if either is missing. Use a `.env` file at the project root (copy `.env.example` and fill in the values). When running via Docker Compose, `COUNTBEANS_DATABASE_URL` is injected automatically by `compose.yml`, so only `COUNTBEANS_BOT_TOKEN` must be present in `.env`. (The bot uses the Telegram **Bot API** via aiogram, which needs only the bot token — there is no MTProto `API_ID`/`API_HASH`.)

> **TODO (before production deploy): set up proper secret management for DB credentials.** The current `compose.yml` hardcodes the Postgres username and password — fine for local dev since the DB is not exposed to the internet, but not for a real server. The right approach depends on the deploy target: Railway/Render/Fly.io provision Postgres and inject `DATABASE_URL` automatically (nothing to do); a VPS needs either a `.env` created over SSH or Docker Secrets; larger setups use a secrets manager (AWS Secrets Manager, Vault, etc.). No code changes are needed — `pydantic-settings` reads from the environment regardless of where the value comes from. Revisit this when the deployment target is chosen.

## Product Spec

countbeans is a Telegram bot for tracking and splitting shared expenses within a
group (Splitwise-style, Telegram-native): members record expenses with `/addexpense`,
view derived balances with `/balance`, settle up with `/settleup`, and optionally
scope a bounded set of expenses to an ad-hoc **event** (a trip, a dinner series).

**The full product spec lives in [docs/spec.md](docs/spec.md)** — requirements and
MoSCoW, command grammar (`/addexpense`, `/balance`, `/settleup`, `/simplify`,
`/group`, `/event`), onboarding & placeholders, the schema design, the
`apportion`/`compute_shares`/`simplify` algorithms, and the Events model. Read it
before changing behavior in those areas.

> **The authoritative schema is the SQLAlchemy ORM in `src/countbeans/db/`** (with
> migrations in `alembic/versions/`), **not** the DDL in the spec — treat the spec's
> `CREATE TABLE` block as design reference that may lag the code.

### Invariants (must follow)

These are load-bearing for correctness; the *why* is in [docs/spec.md](docs/spec.md).

- **Money is integer minor units (cents)** — `int`, never `float`/`Decimal`. Format to
  a display string only in the bot layer.
- **Append-only ledger; balances are derived, never stored.** No `debts` table. Edits
  and deletes are done by **voiding** an event (`voided_at`) and re-adding, never by
  mutating a row in place.
- **Every expense reconciles:** participant `share_cents` sum *exactly* to the expense
  `amount_cents` (largest-remainder apportionment for equal/percent/weighted; exact mode
  validates the given sum).
- **Surrogate PKs are UUID7**, generated in the app layer via `uuid_utils.uuid7()`.
  `telegram_user_id` / `telegram_chat_id` are Telegram-assigned `BIGINT`s.
- **Placeholders:** a mentioned-but-unseen `@handle` is a `users` row with
  `telegram_user_id IS NULL`, **claimed** later by a single-row `UPDATE` (everything
  references the surrogate `users.id`, so no fan-out rewrite). At most one pending
  placeholder per username.
- **Debt simplification is presentation-only.** `simplify()` is a pure function of
  derived balances computed at read time; it **writes nothing** and never materializes.
  Toggling `simplify_debts` must leave every derived balance identical — assert
  **equality vs the never-toggled baseline**, not merely sum-zero.
- **Events are a scope dimension on the one ledger, not a second ledger.** Expenses /
  settlements carry a nullable `event_id` (NULL = general). Reconciliation holds per
  `(scope, currency)`; scopes never net against each other; **never materialize** a
  closed event into the general balance.
- **Constraint naming** flows from `Base.metadata.naming_convention` in
  `src/countbeans/db/_base.py` — name `CheckConstraint`s with the logical suffix only.

### The `@all` / `all` keyword (command grammar)

The reserved "everyone" keyword has **one definition** — `ALL_KEYWORD` plus the
`is_all` / `is_all_selector` predicates in `src/countbeans/bot/utils/parsing.py`.
Every command routes through these; no handler compares the literal `"all"`
itself, so the spelling can never drift between commands. It appears in **two
grammatical families** (the `@`-prefix is the only difference):

- **`@all` — a token in the `@mention`/target namespace**, sitting among
  `@username` args: `/addexpense … @all` (split everyone), `/settleup @all`
  (admin whole-group settle), `/event add @all` (fold the whole known group onto
  the roster; `/event remove @all` has no meaning and is refused). Test an
  already-extracted `@handle` (without the `@`) with **`is_all`**.
- **bare `all` — a positional view selector** that pairs with `me`:
  `/balance all`, `/statements all`. Test the whitespace-split args with
  **`is_all_selector`**.

**The bot layer is the sole interpreter.** Keyword recognition is bot grammar:
the `/addexpense` handler strips `@all` (via `is_all`) before calling
`resolve_participants`, which receives only real handles — an **empty list *is*
"split everyone."** The service core never sees the literal `"all"`. A new
command that needs "everyone" reuses these predicates rather than re-typing the
string.

### The `#general` write-scope override (command grammar)

`#general` is the **one-off escape hatch from active-event mode**: on `/addexpense`
and `/settleup`, adding it forces *that single write* to the general (no-event)
scope even while an event is active — without `/event pause` (so no admin, and no
forgotten-`resume` that would silently mis-file later trip expenses). For a *run*
of general writes, `/event pause` is still the tool; `#general` is for one.

Like `@all`, it has **one definition** — `GENERAL_KEYWORD` + `extract_general_flag`
in `src/countbeans/bot/utils/parsing.py` — so the two commands can't drift. It is
its own **`#`-prefixed namespace**, distinct from the `@mention` target family
(`is_all`) and the bare view-selector family (`is_all_selector`). The matcher is
whole-token and case-insensitive (`#generals` / glued text never match).

**The bot layer is the sole interpreter**, exactly as with `@all`:

- Each handler runs `extract_general_flag` on the args **after** a quoted
  description is removed, so a literal `#general` inside quotes stays description
  text. `/addexpense` strips it from the mention region before
  `unquoted_description` / `parse_participants`; `/settleup` strips it before its
  anchored grammar regexes match.
- The flag collapses to **`event_id = None`** for that command — the service core
  still only ever sees an `event_id` (a value or NULL) and knows nothing of the
  keyword. Downstream the handler keys off the *effective* scope (`scoped_event`),
  not the raw active event, so currency fallback, the coverage warning, and the
  reply wording all behave as if no event were active.
- The reply **confirms** an exercised override mid-event ("ℹ️ Logged as
  general …") so a deliberate opt-out is visible — but there is **no per-reply
  nudge on ordinary event expenses**; the scope echo is the signal, and the
  `#general` hint lives in `/event info` and the command usage texts.
