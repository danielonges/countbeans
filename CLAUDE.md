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

- **Telegram bot** (`src/countbeans/main.py`): Built with `python-telegram-bot`. Handles group chat commands and is the primary user-facing interface. Run via `uv run countbeans`.
- **FastAPI server** (`src/countbeans/apis/`): HTTP API layer, intended for AI agent interactions via `deepagents`.

Both share `src/countbeans/config/core.py` for settings (see below).

**Planned data layer**: PostgreSQL via SQLAlchemy + asyncpg, with Alembic for migrations. The schema (users, groups, expenses, expense_participants, debts) is defined in `SPEC.adoc` but not yet implemented.

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
