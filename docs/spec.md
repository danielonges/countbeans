# Product Spec (SPEC-001)

> Design record extracted from `CLAUDE.md`. This is the *why* and the original
> design narrative. Where this spec and the code disagree, the **code is
> authoritative** — in particular, the live schema is the ORM in
> `src/countbeans/db/`, not the DDL reproduced below. The binding correctness
> invariants are summarized in `CLAUDE.md`; the full reasoning lives here.

Original design notes (SPEC-001). The bot is added to a Telegram group and tracks shared expenses conversationally, keeping a running tally of who owes whom and facilitating settlement — Splitwise-style, Telegram-only.

## Design principles

These cut across the whole data model and are the reason the schema below differs from a naive design:

- **Money is integer minor units (cents), never `float`.** Float arithmetic accumulates rounding error across a ledger. Store and compute in `BIGINT` cents; format to a decimal string only at display time.
- **Append-only ledger; balances are derived, never stored.** Expenses and settlements are immutable events. There is no `debts` table — a user's balance is computed by summing the ledger on read. This eliminates read-modify-write races, keeps a full audit trail, and means edits/deletes are done by *voiding* an event and re-adding, not mutating in place.
- **Every expense reconciles.** Per expense, the participant shares sum *exactly* to the expense amount. Even splits distribute leftover cents deterministically (see the algorithm), so the books always balance and the sum of all member balances in a group is zero.
- **Surrogate PKs are UUID7, generated in the app layer.** UUID7 is time-ordered so B-tree indexes stay sequential (no fragmentation), IDs are non-enumerable (no `id=1,2,3` scraping), and there is no collision risk if data is ever merged across instances. Use `uuid_utils.uuid7()` (Rust-backed). `groups.telegram_chat_id` and `users.telegram_user_id` are Telegram-assigned `BIGINT`s and stay that way.
- **Currency is explicit per event.** Each expense and settlement carries an ISO-4217 code. Balances are computed per currency; cross-currency netting requires an FX policy and is out of scope for now.

## Requirements

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

## Key commands

- `/addexpense <amount> "<desc>" [@user …]` (alias: `/add`, an unpublished typed accelerator) — record an expense. Splits among **only the named users** (the payer is excluded unless they `@mention` themselves); omit mentions (or use `@all`) to split among the whole group, payer included. Per-user suffixes pick the split mode — `@a:30` exact amount, `@a:60%` percentage, `@a:2x` weight (see "Splitting an expense"). A **bare `/addexpense`** (no args) instead launches an **interactive, button-driven wizard** — guided amount/description entry, a tap-to-toggle participant roster with a one-tap equal-split commit, and uneven split-mode screens with a reconciliation-gated Confirm — recording the same expense (see "The interactive `/addexpense` wizard").
- `/balance [all]` — `/balance` shows the caller's own net position with other members; `/balance all` shows **every member's** net balance (per currency) plus the suggested settle-up transfers. Both are derived from the ledger. The suggested transfers honor the group's **debt-simplification setting**: when on, they are the simplified (reduced) set; when off, they are the raw pairwise debts. The per-member net balances are identical either way — the toggle only changes how the *suggested transfers* are presented. When an event is active, `/balance` defaults to that event's scope; a scope can be named read-only (`/balance general`, `/balance "<event>"`) to peek at another without ending the active one (see "Events").
- `/settleup` — record a settlement payment (full or partial) from one user to another, e.g. `/settleup @user1 20`. Omit the amount (`/settleup @user1`) to settle the full suggested amount you owe them. `@user1` must resolve to **someone the bot already knows** (an unknown handle is rejected, never turned into a placeholder — that would leave a stray on a mistyped command). `/settleup @all` is a reserved **admin-only** action that records every suggested transfer at once to zero the whole group (a "clear the board"; the bot checks `getChatMember` like `/simplify`). While an event is active it auto-tags the settlement to that event; settle a *general* debt without leaving event mode by adding `#general` (`/settleup @bob #general`), or `/event pause` first for a run of them (the event stays open).
- `/simplify [on|off]` — view or change the group's debt-simplification setting. `/simplify` with no argument reports the current state (any member). `/simplify on` / `/simplify off` flips it and is **admin-only**: the bot checks the caller via `getChatMember` and refuses (no state change) unless their status is `creator` or `administrator`. The setting is purely presentational — see "Debt simplification".
- `/group` — show group info: name, default currency, and the **debt-simplification setting** (on/off); the **known members** the bot can split among, with pending placeholders flagged separately (mentioned but not yet `/start`-ed); the **coverage gap** (`known` vs `getChatMemberCount`) so people can see who still needs to join; the **active event** (if any) and the list of open events; and a quick activity summary (active expenses and total tracked, per currency).
- `/event …` — manage ad-hoc event scopes (see "Events"). A group has **at most one open event at a time**. `/event new "<name>" [CUR]` begins one (create + open + activate; rejected if one is already open — close it first); `/event pause` / `/event resume` stop or restore auto-tagging without closing (so you can log a run of *general* expenses mid-event — or add `#general` to a single `/addexpense`/`/settleup` for a one-off, no pause needed); `/event close` finishes the open event and frees the slot; `/event reopen "<name>"` reopens a closed one (only when none is open); `/event add|remove @user` edit the roster; `/event list` and `/event` (no arg) report events and the active scope. Any member may run these.

## Components

These are **layered adapters over one shared service core**, all in a single process — see "Architecture" above for the interaction model and the rationale for keeping the boundary in-process rather than over HTTP.

- **Telegram bot** (`aiogram`) — listens for commands/messages in groups, parses command structure and parameters (amount, description, participants), and sends confirmation/balance responses back to the group. **Owns any multi-step interaction state** via aiogram's FSM (`MemoryStorage`) — today the **interactive `/addexpense` wizard** (a bare `/addexpense`) uses it to hold the in-flight draft; the `@all` coverage check is still a non-blocking warning, not a confirm step, and the `/simplify`/`/currency` admin gates are inline `getChatMember` checks — and makes Telegram-only calls (`getChatMember`, `getChatMemberCount`). Calls the Expense manager directly (in-process); runs no SQL itself (a middleware may open/commit a `UnitOfWork` per update — see "Database sessions" — but the bot issues no queries).
- **HTTP shell** (deferred — FastAPI or aiogram's aiohttp) — **not part of the current build.** The runtime today is the bot on long-polling with no inbound HTTP server. If one is added later (for webhooks, a `/healthz` probe, or a non-Telegram client), it is a *second* thin, **stateless** adapter over the same Expense manager — no per-conversation state (that lives in the bot's FSM), no business logic of its own, and never an HTTP hop between the bot and the ledger. See "Architecture".
- **Expense manager** (`countbeans.services`) — the **only** component that talks to the database. Validates and records expenses and settlements as ledger events, computes derived balances (and, optionally, a simplified set of transfers). Stateless and transactional (one transaction per command); accepts/returns Pydantic DTOs, with no knowledge of Telegram or HTTP.
- **Database** — persists users, group membership, and the immutable ledger of expenses, expense shares, and settlements.

## Onboarding & membership

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

## Schema (PostgreSQL)

> **Reference only.** The authoritative schema is the SQLAlchemy ORM in
> `src/countbeans/db/` (with migrations in `alembic/versions/`). The DDL below
> is the original design and may lag the code — consult the ORM models for the
> live shape.

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
  default_currency  CHAR(3) NOT NULL DEFAULT 'SGD',   -- ISO 4217
  -- Debt-simplification toggle (admin-only via /simplify). Purely a display
  -- preference: it changes how /balance all *suggests* transfers, never the
  -- ledger or derived balances, so flipping it any number of times is safe.
  simplify_debts    BOOLEAN NOT NULL DEFAULT TRUE,
  -- Active event for "active-event mode" (see "Events"): when non-NULL it points
  -- at the group's single OPEN event and /addexpense & /settleup auto-tag to it;
  -- NULL = general tracking (no open event, or the open event is paused).
  -- Shared across all members and durable across restarts, so it lives in the
  -- DB here, NOT in aiogram FSM (FSM holds only per-conversation multi-step
  -- state). FK added after `events` exists — groups<->events is circular.
  active_event_id   UUID REFERENCES events(id),
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

-- Ad-hoc sub-scopes within a group (e.g. a trip) for tracking a bounded set of
-- expenses separately from regular tracking. An event is a *scope dimension* on
-- the one shared ledger, never a separate ledger: expenses/settlements carry a
-- nullable event_id (NULL = general/regular tracking) and balances are derived
-- per scope. Isolated by design — the general balance excludes event-tagged
-- rows and each event settles independently; there is no cross-event netting.
-- App invariant: at most one OPEN event per group at a time (enforced by the
-- partial unique index below) — a new event opens only after the current one is
-- closed, so which event is in play is never ambiguous. Closed events may
-- freely reuse a name.
CREATE TABLE events (
  id                UUID PRIMARY KEY,                    -- UUID7, generated in app layer
  group_id          UUID NOT NULL REFERENCES groups(id),
  name              VARCHAR(255) NOT NULL,
  default_currency  CHAR(3),                             -- NULL = inherit groups.default_currency
  status            VARCHAR(16) NOT NULL DEFAULT 'open', -- 'open' | 'closed'
  created_by        UUID NOT NULL REFERENCES users(id),
  created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at         TIMESTAMP WITH TIME ZONE,            -- NULL = open; set when status -> 'closed'
  CHECK (status IN ('open', 'closed')),
  CHECK (default_currency IS NULL OR LENGTH(default_currency) = 3)
);

-- At most one OPEN event per group: a new event can be opened only after the
-- current one is closed. (The app also enforces this with a friendly
-- "close <name> first" instead of surfacing the raw index violation.)
CREATE UNIQUE INDEX uq_events_one_open_per_group ON events (group_id) WHERE status = 'open';

-- Explicit per-event roster: a deliberate opt-in SUBSET of the group (the trip
-- attendees). `@all` inside an active event means THIS roster, not the whole
-- group, so the group-level getChatMemberCount coverage check does not apply.
-- Grows implicitly (the creator on /event new; anyone named as a participant
-- in an event expense) and explicitly (/event add|remove). References users.id,
-- so claiming a placeholder needs no rewrite here either.
CREATE TABLE event_members (
  event_id  UUID NOT NULL REFERENCES events(id),
  user_id   UUID NOT NULL REFERENCES users(id),
  PRIMARY KEY (event_id, user_id)
);

-- Immutable expense events; soft-deleted via voided_at
CREATE TABLE expenses (
  expense_id    UUID PRIMARY KEY,            -- UUID7, generated in app layer
  group_id      UUID NOT NULL REFERENCES groups(id),
  event_id      UUID REFERENCES events(id),                 -- NULL = general/regular tracking
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
  event_id       UUID REFERENCES events(id),            -- NULL = general/regular tracking
  from_user_id   UUID NOT NULL REFERENCES users(id),    -- pays
  to_user_id     UUID NOT NULL REFERENCES users(id),    -- receives
  amount_cents   BIGINT NOT NULL CHECK (amount_cents > 0),
  currency       CHAR(3) NOT NULL CHECK (LENGTH(currency) = 3),
  created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (from_user_id <> to_user_id)
);
```

## Deriving balances

A user's net balance in a group, **per currency** (positive = the group owes them, negative = they owe the group):

```
balance(u) =  Σ amount_cents  of active expenses where payer = u      -- money they fronted
            − Σ share_cents   of their shares on active expenses       -- what they consumed
            + Σ amount_cents  of settlements where from_user = u       -- payments they made
            − Σ amount_cents  of settlements where to_user   = u       -- payments they received
```

Because shares always sum to the expense amount, the balances of all members in a group sum to zero — a useful invariant to assert in tests.

**Per scope.** With events (see "Events"), every term above is filtered by `event_id`: the general balance sums only rows where `event_id IS NULL`, and an event's balance sums only that event's rows. The sum-to-zero invariant therefore holds **per `(scope, currency)`** — each event, and the general scope, independently sums to zero. Scopes never net against each other (isolated by design).

## Splitting an expense

A split is two independent choices: **who** is in it (participant selection) and **how** the amount is divided among them (split mode). The only universal rule is that **shares sum exactly to `amount_cents`**. None of this touches the schema — every mode just writes `expense_shares` rows, and a non-participant simply has no row.

**Participant selection** — splitting with only some of the group:

- **Named subset:** `/addexpense 50 Dinner @a @b` splits among **only the named users** — the payer is **not** added automatically. This is the "I paid, these people owe me" case: `/addexpense 25.50 Lunch @a` leaves `@a` owing the full 25.50 and the payer owed it. Mention yourself (`@a @me`) to be included in the split.
- **Everyone:** with no mentions (or an `@all` keyword), split across all current members from `group_members` (the payer included, pending placeholders included). Because the bot can't enumerate the real roster, `@all` means "everyone the bot knows" — see the coverage check below.
- **Including the payer:** the payer is a participant only in the "everyone" case or when they `@mention` themselves; otherwise they paid but didn't partake, so they get no share and are owed the full amount.

(Resolution lives in `resolve_participants` in `services/add_expense.py`; the bot passes the parsed `@handles` and gets back the participant `MemberInfo` list.)

**Split modes** — dividing the amount unevenly:

| Mode | Command example | Rule |
|---|---|---|
| Equal | `/addexpense 60 Dinner @a @b` | even split across participants |
| Exact | `/addexpense 50 Dinner @a:30 @b:20` | per-person cents; must sum to the amount |
| Percentage | `/addexpense 50 Dinner @a:60% @b:40%` | percentages must sum to 100 |
| Weighted | `/addexpense 50 Dinner @a:2x @b:1x` | split in proportion to integer weights |

Equal, percentage, and weighted splits are the *same* operation — apportion the amount in proportion to integer weights — using the **largest-remainder method** so the cents always reconcile. Exact mode skips apportionment and takes the given cents after validating their sum. Rule violations raise `DomainError` (`services/errors.py`, a `ValueError` subclass marking messages safe to show the user — the handler replies with it verbatim).

```python
def apportion(amount_cents: int, weights: dict[Id, int]) -> dict[Id, int]:
    """Split amount_cents in proportion to integer weights, summing exactly to
    amount_cents (largest-remainder method)."""
    total = sum(weights.values())
    if total <= 0:
        raise DomainError("weights must sum to a positive value")
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
                raise DomainError("percentages must sum to 100")
            return apportion(amount_cents, params)
        case "exact":                            # params: {id: cents}
            if sum(params.values()) != amount_cents:
                raise DomainError("exact shares must sum to the expense amount")
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

At the service level the payer is just another participant *when included*: their share is computed like anyone else's, and their net position (paid − consumed) falls out of the balance formula. When the payer is not a participant (the default for a named split — see "Participant selection") they simply get no `expense_shares` row, so they are owed the full amount. Whether to include the payer is decided one layer up, in `resolve_participants`; `compute_shares` only ever sees the final participant list.

## Command parsing & validation

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
2. **Description** — a quoted string, or the run of words between the amount and the first `@mention`. Use quotes if it contains `@`. Any **matching quote pair** is accepted — straight `”…”`/`’…’`, curly/smart `”…”`/`’…’`, guillemets `«…»`, or backticks `` `…` `` — and a **backslash escapes the next character**, so the closing quote can appear inside (`"she said \"hi\""`). A curly opener must be closed by its curly partner (no mixing). Parsed by `extract_quoted_description` in `bot/parsing.py`; an unmatched opener (e.g. an apostrophe in `it's`) is skipped, not treated as a quote. May be empty.
3. **One mode per command, inferred from the suffixes:**
   - No suffix on any mention → **equal**.
   - All suffixes end in `%` → **percentage**; must sum to 100.
   - All suffixes end in `x` → **weighted**.
   - All suffixes are bare numbers → **exact**; must sum to the amount.
   - **Mixing families is rejected** (`@a:30 @b:40%` → error), and in any non-equal mode **every** participant must carry a suffix of that family (`@a:60% @b` → error).
4. **Participants** — naming one or more @handles splits among **only those users**; the payer is **not** added automatically (mention yourself to be included). With no @mentions, split among everyone the bot knows. Duplicate handles are deduplicated.
5. **`@all`** — splits equally across all current `group_members` (the payer and placeholders included). Omitting mentions entirely is equivalent to `@all`. Since the bot cannot enumerate the real roster, it compares known members against `getChatMemberCount` (see below): it splits among the members it knows and **appends a non-blocking warning** when there's a gap. This is the deliberate behavior — a blocking confirm step is **not** built (see the coverage-check note below for the rationale).
6. **Unknown handles are not errors** — they become pending placeholders (see Onboarding).
7. Fractional percentages (≤2 dp) are carried as integer **basis points** (sum must be 10000) so `apportion` stays integer-only.

`/settleup @user [<amount>]` — `<amount>` (optional; omit to settle the full suggested amount) follows rule 1; `@user` must resolve to **someone already known** — unlike `/addexpense`, a settleup mention is looked up with `find_by_mention` and an unknown handle is **rejected** (you can't owe someone the bot has never seen), so a typo never spawns a stray placeholder; settling with yourself is rejected. `@all` is reserved (admin-only whole-group settle), never parsed as a username.

**`@all` coverage check (non-blocking warning).** The bot can't list members, but `getChatMemberCount` (available to any bot) tells it how many there *are*. At `@all` time compare the count it can split among against the real count:

```
known  = active rows in group_members      # who the bot can actually split among
actual = getChatMemberCount() - 1          # minus the bot itself
```

- `known == actual` → the bot demonstrably knows everyone; record the expense, no warning.
- `known <  actual` → there are members the bot has never seen and **cannot name**. **Record the expense among the known members anyway, and append a non-blocking warning** naming the gap ("split among the 3 I know — 2 more haven't interacted yet; ask them to /join to be included"). The only way to actually include an unseen member is for them to interact — there is no API to pull them in (admin's `chat_member` stream captures *future* joiners, never pre-existing silent ones).

This is a deliberate trade. A **block-until-confirmed** flow (refuse the write, hold the parsed expense in aiogram FSM, and require the payer to confirm "just these people" or wait for the missing members to `/join`) was considered and **intentionally not built**: the warning already surfaces the gap, a genuinely wrong split is recoverable by voiding, and real groups are usually small enough that the bot has seen everyone. The check is also skipped entirely **inside an active event**, where `@all` means the event roster — an intentional subset, so there is nothing to warn about. (Implemented in `bot/handlers/addexpense.py`.)

### The interactive `/addexpense` wizard

A **bare `/addexpense`** (no args) launches a guided, button-driven flow instead of the one-liner grammar above — for users who'd rather tap than type. It is purely **additive**: the one-liner still serves power users unchanged, and the wizard is a bot-layer **entry path** only, collecting the same fields and calling the same `add_expense` service path (no new service code). It runs **in the group chat** (not a DM deep-link) and is implemented in the `bot/handlers/addexpense_wizard/` package (`states` / `render` / `steps` / `actions`). The steps:

1. **Amount + optional description** — prompted with a `ForceReply`; the reply is `<amount> [description]` on one line (e.g. `50.25 1 night at Domino's`, quotes optional). A currency prefix overrides (`$50`, `USD50`).
2. **Participant roster** — a tap-to-toggle list of known members (paged, with *Everyone* / *Clear*), opening with everyone selected (matching the one-liner's "no mentions = all"); pending placeholders are marked ⏳. ✏️ / 📝 buttons re-prompt for the amount or description mid-flow (a mistyped amount never forces a restart), and — when an event is active — a 📂 button in plain words ("Don't tag to *\<event\>*" / "Tag to *\<event\>*") flips the draft to `#general` scope. The primary **✅ Add — split equally** button **commits right here**: the anchor already previews amount/description/scope/selection, and `/void` is the undo — mirroring the one-liner's record-then-void model, so the everyday equal split is one tap. **Uneven split ▶** continues instead:
3. **Split mode** — *Exact* / *Percent* / *Weight* buttons (Equal lives on the roster screen as the fast path; confirmation friction is reserved for the high-cost uneven splits).
4. **Per-person shares** — each participant's share is typed via a `ForceReply`; *Confirm* appears only once the shares reconcile (percent = 100, exact = the amount, weighted = any positive total) and writes the expense.
5. **Receipt** — the same receipt as the one-liner (equal splits collapse to one "*X* each" line; the same coverage-gap / payer-excluded / `#general` nudges; the `/void` hint), plus — for equal splits that reconstruct faithfully — a "💡 Faster next time: `/addexpense …`" footer teaching the equivalent one-liner, so wizard users graduate to the one-message path.

Design notes that fall out of the platform:

- **The draft lives in aiogram FSM state (`MemoryStorage`)**, keyed by `(chat, user)` — `callback_data`'s 64-byte cap is too small for an expense, and the per-key state isolates two members' concurrent wizards for free. Buttons reference roster members by **index** into the stored roster, never by UUID.
- **Free-text steps are reply-only.** This bot runs with **group privacy off**, so it receives all group messages; matching any in-state text would let ordinary chatter hijack the flow. Each free-text step therefore fires only on a *direct reply to the bot's current prompt* (`callback_data` aside, the answer's `reply_to_message` must point at the live prompt). `/cancel` aborts at any step.
- **One anchor message.** Button steps edit a single anchor in place (like `/statements` paging); reply steps re-send it fresh at the bottom so the update is unmissable. Taps are **anchor-bound to the initiator** — another member tapping your draft is rejected, not silently dropped.
- **Confirm is guarded against a double-submit** so a double-tap can't record the expense twice in the append-only ledger.

## Debt simplification (must-have, per-group toggle)

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

**A per-group toggle, admin-only.** Simplification is controlled by `groups.simplify_debts`, flipped with `/simplify on|off`. Only a group **admin** may change it: on a set request the bot calls `getChatMember(chat_id, caller_id)` and proceeds only if the caller's status is `creator` or `administrator`; otherwise it refuses and leaves the setting untouched. (This is independent of the bot's *own* required admin rights — see Onboarding.) Reading the setting (`/simplify` with no argument, or `/group`) is open to any member. The setting defaults to **on**: simplification is purely presentational (the per-member balances are byte-for-byte identical either way — see below), so the default carries **no ledger risk** and immediately delivers the feature's value — the fewest transfers to settle up. The one cosmetic cost is that a suggested transfer may name someone you never directly transacted with; since that is advisory only and reversible with zero ledger impact, an admin who prefers raw pairwise debts can `/simplify off`.

**The toggle is presentation-only; balances never move.** This is the design rule that makes flipping it safe any number of times. The single source of truth is the append-only ledger (expenses, expense_shares, settlements); every net balance is **derived** from that ledger on read (see "Deriving balances") and `simplify()` is a *pure function of those balances* that returns suggested transfers. Simplification therefore:

- **writes nothing** — it never inserts settlements, never voids or mutates anything, never persists its output. It runs at read time for `/balance all` and is thrown away.
- **changes only the suggested-transfer view** — `simplify_debts = true` renders the reduced transfer set; `false` renders the raw pairwise debts. The per-member net balances `/balance all` prints are computed straight from the ledger and are byte-for-byte identical under either setting.

Because the toggle touches no rows that feed the balance formula, toggling on → off → on → … leaves balances exactly where they were. There is **no "apply simplification" step** that nets the ledger down; that would be the one implementation that *could* corrupt balances on toggle, and it is deliberately excluded. The invariant to assert in tests: for any ledger and any sequence of `simplify_debts` flips, each member's derived balance equals what the toggle-free ledger derives — i.e. **equality against the never-toggled baseline** is the load-bearing assertion. Do **not** assert only "balances sum to zero": every settlement moves `+x`/`−x`, so the sum stays zero even when individual balances are wrong, which means a materialize-on-toggle bug can corrupt balances while still passing a sum-zero check. Sum-zero is a sanity check, never the proof of accuracy.

**Acting on a suggestion is a normal settlement.** When a user runs `/settleup` after seeing a simplified suggestion (e.g. "A pays C 30"), that records an ordinary settlement event between the actual `from`/`to` users — a real transfer of obligation, no different from any other settlement. It is a genuine ledger fact, not a materialization of the simplification, so later toggling the setting off (and showing raw pairwise debts again) still yields correct, consistent balances. Suggestions are advisory; only `/settleup` moves money in the ledger.

## Events (ad-hoc expense scopes)

An **event** is an ad-hoc sub-scope within a group for tracking a bounded set of expenses separately — a weekend trip, a dinner series, a shared project — without spinning up a new Telegram group. It is the same pattern as the simplify toggle: **a scope dimension on the one append-only ledger, never a second ledger.** The whole schema change is the `events`/`event_members` tables, a nullable `event_id` on `expenses`/`settlements` (NULL = regular/general tracking), and `groups.active_event_id`. Everything else — `apportion`, `compute_shares`, the balance formula, `simplify()`, placeholders/claiming, voiding — works unchanged, just **parameterized by scope**.

**Isolated scopes (not a filtered view).** Scopes do not net against each other. The general `/balance` derives over `event_id IS NULL` only and **excludes** event-tagged rows; each event derives over its own slice; each is settled independently. There is **no** automatic combined/grand-total balance across scopes in v1 (a deliberate Won't — see below). This is what "track the trip separately" means: the trip has its own tab you can settle and forget, without it touching the regular running tally.

**Active-event mode (how expenses get tagged).** Tagging is implicit via a shared, durable **active event**, not a per-command token:

- `/event new "<name>"` begins an event (create + open + make active), setting `groups.active_event_id`. While set, **`/addexpense` and `/settleup` auto-tag to it** (unless a single write opts out with `#general`, below). `/event pause` clears the pointer without closing (the event stays `open`); `/event resume` re-points at it.
- **Writes default to the active scope, with a one-off `#general` override.** Auto-tagging makes the active event "sticky," so **every scoped reply echoes the scope** ("✅ Added to *Bali Trip*: …") and `/group` surfaces the active event prominently — otherwise people mis-file expenses. To record a *single* general (non-event) expense or settlement **without leaving event mode**, add the reserved **`#general`** flag (`/addexpense 12 "taxi" #general`, `/settleup @bob #general`): it forces just that one write to the general scope, behaves exactly like "no active event" for that command (currency, coverage check, reply wording), and is confirmed in the reply. For a *run* of general writes, `/event pause` (admin-only) instead. The trade vs. the original no-override design: `#general` needs no admin and — being per-command — can't leave a forgotten `/event resume` silently mis-filing later trip expenses. `#general` is a bot-grammar keyword in its own `#`-namespace (one matcher, both commands — see the keyword note in CLAUDE.md); the service core still only ever sees `event_id` (a value or NULL).
- **Reads may cross scopes; writes may not.** `/balance` defaults to the active event's scope, but a scope can be *named* read-only (`/balance general`, `/balance "<event>"`) without ending the active one — reading another scope is harmless, mis-tagging a write is not.
- **At most one event is open per group at a time** (a partial unique index, `uq_events_one_open_per_group`, enforces it). To begin a new event you must `/event close` the current one first — there is no switching between several open events. The active pointer therefore only ever references that single open event or is NULL (general / paused).
- The active event lives in the **DB** (`groups.active_event_id`), **not aiogram FSM** — it is shared across all members and must survive restarts, whereas the FSM (e.g. the `/addexpense` wizard's in-flight draft) holds only per-conversation state.

**Explicit roster (who `@all` means).** An event carries its own `event_members` roster — a deliberate opt-in **subset** of the group (the trip attendees):

- `@all` inside an active event splits across the **roster**, not the whole group.
- **The group-level coverage check does not apply.** The `getChatMemberCount` coverage warning exists because the bot can't enumerate the group; a roster is an intentional subset, so there is nothing to warn about. (Unclaimed placeholders on the roster are valid participants.)
- The roster grows **implicitly** (the creator on `/event new`; anyone named as a participant in an event expense joins) and **explicitly** (`/event add|remove @user`). It references `users.id`, so claiming a placeholder binds their event shares automatically, exactly as in the general scope.

**Lifecycle is state-only — no materialization.** An event is `open` → `closed` (`/event close`), reversibly via `/event reopen` (allowed only when no other event is open, per the one-open rule); a closed event rejects new tagging. A group runs many events across its lifetime, but strictly **sequentially** — close the current one before opening the next. **Closing never rolls a trip's net debts into the general balance.** That would be a materialization — the same trap the simplify section forbids: it would have to be done as real settlement/transfer events, and silently doing so on close would corrupt the "balances are derived" contract. Settling a trip is just normal `/settleup`s tagged to that event; folding a trip into the general tab, if ever wanted, is future work via *explicit recorded transfers*, never a mutate-on-close.

**Currency.** `events.default_currency` is nullable and falls back to `groups.default_currency`. Per-`(scope, currency)` balances still hold, so a trip can default to a foreign currency while the group stays on its own.

**Simplify & `/group`.** `/balance` within a scope honors the group's `simplify_debts` setting against that scope's balances (a per-event toggle is a Could-have, not v1). `/group` gains the active event (if any), the list of open events, and per-event activity in its summary.

**Invariants to assert in tests (extends "Deriving balances" and "Debt simplification"):**

- **Per-scope sum-zero:** every `(event, currency)` and the general scope each independently sum to zero.
- **Isolation:** the general balance equals the balance computed while ignoring all event-tagged rows; an event's balance equals the balance over only its rows. Toggling/closing an event never moves a balance in another scope.
- **Equality vs. the never-toggled baseline** for simplify holds *per scope*.

**Service-core & DTO impact.** `AddExpenseCommand` and `SettleUpCommand` gain an optional `event_id: UUID | None`; the **bot** resolves the active event from group state and populates it (or leaves it NULL when the caller adds `#general`) — the core stays Telegram-agnostic and just records the tag. New command DTOs `CreateEventCommand` (new), `SetActiveEventCommand` (pause/resume — points the pointer at the open event or NULL), `SetEventStatusCommand` (close/reopen), and `EditEventRosterCommand`, a result `EventCreatedResult`, and a read-side `EventSummary` follow the existing conventions (frozen, money as int cents, IDs UUID7). All SQL stays in the service core; one transaction per command, unchanged.

**Command-grammar note (open).** `/balance all` already means *all members*; adding a scope axis needs disambiguation so `all` (the member axis) and a scope name don't collide — e.g. `/balance [<scope>] [all]`. The token syntax is a parsing task, deferred to implementation.

**MoSCoW for events.**

- *Must-have*: create/open an event (**one open at a time** — sequential lifecycle); active-event mode + auto-tag + the per-command `#general` override; `/event pause` / `resume` (log a run of general expenses mid-event); `/event close` (finish, freeing the slot for the next event); explicit roster + event `@all`; event-scoped `/balance` and `/settleup`; per-`(scope, currency)` reconciliation; scope echoed in every reply.
- *Should-have*: `/event reopen` a closed event; per-event default currency; `/event list` and `/event` info; `/group` surfacing the active/open event.
- *Could-have*: per-event `simplify` toggle (else inherit the group); closed-event summary/export; a combined "all scopes" balance view; `event_members` leave/rejoin history.
- *Won't-have (v1)*: cross-event netting / roll-up of a closed event into general (the materialization trap — settling a trip is just normal scoped `/settleup`s); events spanning multiple Telegram groups.
