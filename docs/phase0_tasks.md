# Phase 0 tasks — run one at a time in Claude Code

For each task, open the Claude Code panel, pick the model shown, and paste the prompt:

> Read CLAUDE.md, docs/phase0_spec.md and task **T<n>** in docs/phase0_tasks.md.
> Implement only T<n>. Write the tests first. Run pytest and ruff. Then summarise what changed
> and anything left open. Do not start the next task.

Review the diff, then commit with message `T<n>: <title>` and push. Tick the box below.

| Done | Task | Model | Acceptance check |
| --- | --- | --- | --- |
| [x] | **T1 Project skeleton.** uv project, src layout, ruff + pytest config, `cli.py` with an `ep` entry point that prints the version | Sonnet | `uv run ep --version` works; CI not yet |
| [x] | **T2 Settings and secrets.** `config.py` with pydantic-settings reading `.env`; `.env.example` complete; keys never logged | Sonnet | Test proves missing required key raises a clear error; `repr(settings)` hides secrets |
| [x] | **T3 Database and migrations.** SQLAlchemy models for every table in the spec; SQLite WAL; Alembic initial migration | **Opus** (design + review) | `alembic upgrade head` creates all tables; a test round-trips one row per table |
| [x] | **T4 Trading calendar.** NSE holidays + weekends, YAML override, helpers `is_trading_day`, `prev_trading_day`, `add_trading_days` | Sonnet | Tests on known holidays and long weekends |
| [x] | **T5 Equity master.** Download NSE equity list, upsert `securities`, record renames in `symbol_history`. When an ISIN not in securities appears, insert a minimal securities row with status UNKNOWN_OR_DELISTED and source; prefer NSE's newer bhavcopy format that includes ISIN (verify the current file name and columns) | Sonnet | Fixture-based test; second run writes 0 rows |
| [x] | **T6 Daily prices (bhavcopy).** Download full bhavcopy for a date, parse EQ series, upsert `prices_daily` incl. delivery %; `ep backfill --from --to` with polite rate limit. When an ISIN not in securities appears, insert a minimal securities row with status UNKNOWN_OR_DELISTED and source; prefer NSE's newer bhavcopy format that includes ISIN (verify the current file name and columns) | Sonnet | Fixture test; golden check on 10 stocks × 5 dates; idempotent rerun |
| [x] | **T7 Tapetide client.** MCP client, budget guard, snapshot writer, cache; wrappers for `search_stocks`, `get_price_history`, `get_company_profile`, `get_forecasts`, `screen_stocks`, `get_index_membership_asof`, `get_adjustment_factors` | **Opus** (review) | Tests with recorded responses; budget refusal test; no network in tests |
| [x] | **T8 Bhavcopy fallback.** If NSE download fails for a date, fetch via Tapetide `get_price_history` (within budget), mark `source`, mark run DEGRADED | Sonnet | Test simulates NSE failure |
| [x] | **T9 Surveillance lists.** Load ASM / GSM / ESM nightly into `surveillance` | Sonnet | Fixture test; a known ASM stock appears |
| [ ] | **T10 Index membership.** Load current Nifty 500 constituents; monthly refresh closes old rows with `date_to` | Sonnet | Test add/remove handling |
| [ ] | **T11 Corporate actions.** Split/bonus factors via Tapetide `get_adjustment_factors` for the universe (budget-aware, cached); rights/demergers flagged without a factor | Sonnet | Test on a known split |
| [ ] | **T12 Smallcase import + name mapping.** Read `data/private/EquityPulse_smallcase_validation_dataset.xlsx` into the smallcase tables; map names to ISIN (exact, then normalised fuzzy match against `securities`); low-confidence names to `data/private/name_map_review.csv` | **Opus** (review the matching) | Row counts match the spec; ≥ 95% mapped; review file produced |
| [ ] | **T13 Audit and status.** `data_audit` written by every job, append-only: a STARTED row when the job begins and a final OK / DEGRADED / FAILED row (same run_id, with `finished_at`) when it ends; a STARTED row with no final row is reported as a crashed run; `ep status` shows freshness per source and Tapetide calls today | Sonnet | Test on a seeded DB |
| [ ] | **T14 Nightly job.** `ep nightly [--date]` runs calendar check → master → bhavcopy → surveillance → audit; skips non-trading days | Sonnet | Rerun writes 0 rows; non-trading day exits cleanly |
| [ ] | **T15 CI.** GitHub Actions: uv sync, ruff, pytest on push and PR | Sonnet | Green check on GitHub |

After T15, run the Phase 0 gate in `docs/phase0_spec.md`, then ask for the Phase 1a spec.
