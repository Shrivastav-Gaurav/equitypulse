# Phase 0 — Data foundation (weeks 1–2)

**Goal:** a reliable, point-in-time data store that every later phase reads from. No scoring,
no UI, no LLM yet. Phase 0 is done when the gate at the bottom passes.

## Scope

| In scope | Out of scope (later phases) |
| --- | --- |
| Security master (ISIN-keyed), symbol history | Signal engine, trade plans |
| NSE trading calendar | Streamlit UI |
| Daily prices + delivery % (NSE bhavcopy), 2-year backfill | LLM parser, research agent |
| Surveillance lists (ASM / GSM / ESM) | Kite holdings sync, alerts |
| Nifty 500 membership (current; history via Tapetide as-of later) | Backtests |
| Tapetide MCP client with budget guard + snapshots | |
| Smallcase validation dataset import + name→ISIN mapping | |
| Data audit log + `status` command | |
| Nightly job entry point; CI on GitHub Actions | |

## Folder layout

```
src/equitypulse/
  config.py            # Settings from .env (pydantic-settings)
  db/models.py         # SQLAlchemy models (schema below)
  db/session.py        # engine (SQLite WAL), session factory
  calendar.py          # trading-day helpers
  sources/nse.py       # bhavcopy, equity master, surveillance, holidays
  sources/tapetide.py  # MCP client + budget guard + snapshot writer
  sources/indices.py   # index constituents
  imports/smallcase.py # smallcase workbook import + name mapping
  audit.py             # data_audit helpers
  jobs/nightly.py      # orchestrates nightly ingest
  cli.py               # `ep status`, `ep backfill`, `ep nightly`
tests/ , tests/fixtures/
data/private/          # gitignored: smallcase workbook, local DB
```

## Database schema (v1)

| Table | Key columns | Notes |
| --- | --- | --- |
| `securities` | isin (PK), symbol, name, series, sector, industry, listing_date, status | From NSE equity master; sector/industry from Tapetide profile later |
| `symbol_history` | isin, symbol, valid_from, valid_to | Renames and recycled symbols |
| `trading_calendar` | date (PK), is_trading_day, note | NSE holidays + weekends; special sessions noted |
| `prices_daily` | isin, date (PK pair), open, high, low, close, prev_close, volume, traded_value, delivery_qty, delivery_pct, series, source | Raw, unadjusted |
| `corporate_actions` | isin, ex_date, action_type, details, adj_factor, source | Splits/bonus factor; rights/demergers flagged without a factor |
| `surveillance` | as_of, isin, list_type, stage, source | One row per stock per list per date |
| `index_membership` | index_name, isin, date_from, date_to, source | Current constituents now; history later |
| `snapshots` | id, source, endpoint, entity, as_of, fetched_at, payload_json, payload_hash | Append-only; dedupe on hash |
| `data_audit` | run_id, job, source, started_at, finished_at, status, rows_in, rows_written, stale_count, anomaly_count, error | status ∈ OK / DEGRADED / FAILED |
| `api_usage` | date, source, calls | Budget guard counter |
| `smallcase_periods` | smallcase, period_start, period_end, raw_name, isin, weight | From the validation workbook |
| `smallcase_events` | smallcase, date, raw_name, isin, action, weight | ADD / REMOVE / INITIAL |
| `smallcase_index` | smallcase, date, index_value | Daily, base 100 |
| `name_map` | raw_name (PK), isin, method, confidence, verified | exact / fuzzy / manual |

Migrations with Alembic from the first table, so moving to PostgreSQL later is a config change.

## Data sources

| Data | Source | Notes |
| --- | --- | --- |
| Equity master | NSE `EQUITY_L.csv` | Verify current URL on nseindia.com; send browser-like headers; retry with backoff |
| Daily prices + delivery | NSE full bhavcopy (`sec_bhavdata_full_DDMMYYYY.csv` in NSE archives) | Verify URL; if NSE blocks, fall back to Tapetide `get_price_history` and mark source |
| Holidays | NSE holiday list | Cache yearly; also a manual YAML override |
| Surveillance | NSE ASM / GSM / ESM reports | Nightly |
| Nifty 500 constituents | niftyindices.com constituent CSV | Monthly |
| Fundamentals, forecasts, filings, as-of tools | Tapetide MCP (`https://mcp.tapetide.com/mcp`, bearer token) | REST is Enterprise-only, so use the Python `mcp` client |

## Tapetide client requirements

- One function per tool used; typed return models (pydantic).
- Budget guard: counts calls per day in `api_usage`; refuses past `TAPETIDE_DAILY_BUDGET`
  (default 45 on the free plan) with a clear error.
- Every response written to `snapshots` (source, tool name, entity, as_of, payload, hash).
- Retries with backoff on network errors; no retry on 4xx.
- Results cached in the DB; a cache hit costs no call.

## Phase 0 gate (all must pass)

1. `uv run pytest` and `uv run ruff check .` pass in CI.
2. 2 years of daily prices for the current Nifty 500 loaded; < 0.1% missing trading-day rows
   (each gap explained in `data_audit`).
3. Golden check: closes for 10 hand-picked stocks on 5 dates match NSE's published bhavcopy exactly.
4. Re-running `ep nightly` for the same date writes zero new rows.
5. Surveillance lists load nightly; at least one known ASM stock is present.
6. Smallcase import: 2,905 period rows, 1,208 events, 7,560 index rows; ≥ 95% of the 345 names
   mapped to an ISIN, the rest listed in `data/private/name_map_review.csv` for manual review.
7. `ep status` prints last successful run, rows and freshness per source, and Tapetide calls used today.
