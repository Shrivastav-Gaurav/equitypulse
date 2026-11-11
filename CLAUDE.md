# EquityPulse — rules for Claude Code

EquityPulse is a personal Indian-equity trade planner: an on-demand scan returns up to 5
short-term trade plans (buy price, target, stop, quantity, horizon), and a monitor emails
sell alerts for stocks I actually hold. Real money depends on it.

- PRD (source of truth): https://claude.ai/artifact/5cMzfzS9JsqWjkSShr8PpU
- Current phase spec: `docs/phase0_spec.md`
- Task list: `docs/phase0_tasks.md` — work on ONE task at a time, in order.

## How to work

1. Read this file, the phase spec and the task before writing code.
2. Write the tests first (`tests/`), then the code, then run `uv run pytest` and `uv run ruff check .`.
3. Change only what the task asks. If something outside the task needs changing, stop and say so.
4. End every task with a short summary: what changed, how it was tested, anything left open.
5. Never commit secrets. Never print API keys. Keys live in `.env` (gitignored) and are read
   through `equitypulse.config.Settings`.

## Hard rules (do not break these)

- **No LLM in money decisions.** Ingestion, scoring, pricing, sizing, risk checks and the
  monitor are plain, deterministic Python. LLMs are used only for: parsing a scan prompt into
  filters, reading filings, writing rationales, and comparator write-ups — always behind
  validation.
- **Point-in-time data.** Every fetched value is stored with `source`, `as_of` and `fetched_at`.
  Raw payloads go to the `snapshots` table and are never overwritten or deleted.
- **No look-ahead.** Code that computes a signal for date D may only read data with
  `as_of <= D` (and fundamentals only after their filing date).
- **ISIN is the primary key** for securities. Symbols change; ISINs rarely do.
- **Trading calendar, not calendar days.** Staleness and horizons use NSE trading days.
- **Jobs are idempotent.** Re-running a job for the same date must not duplicate rows.
- **Tapetide call budget.** All Tapetide calls go through `equitypulse.sources.tapetide`,
  which enforces the daily budget in `.env` (`TAPETIDE_DAILY_BUDGET`). Never call it in a loop
  without the budget guard.
- **Fail loudly, degrade safely.** A failed source marks the run `DEGRADED` in `data_audit`
  and stale data is flagged; it is never silently used.
- **Personal data stays private.** Files in `data/private/` are gitignored. Never move them.

## Stack

Python 3.11+, uv, SQLAlchemy 2.x (SQLite in WAL mode now, PostgreSQL later), Alembic,
pydantic v2 + pydantic-settings, httpx, pandas, pytest, ruff. Streamlit for UI (later phase).
LangGraph + OpenRouter only in later phases.

## Conventions

- Package code in `src/equitypulse/`; one module per source in `src/equitypulse/sources/`.
- Money in INR as `Decimal` at the edges, floats in pandas; percentages stored as fractions.
- Dates are `datetime.date` in IST market terms; timestamps are timezone-aware UTC.
- Type hints everywhere; small functions; docstrings say *why*, not *what*.
- Tests must not hit the network: use recorded fixtures in `tests/fixtures/`.
