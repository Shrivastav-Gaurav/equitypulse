# Phase 1a — Core signal and trade-plan engine (weeks 3–5)

**Goal:** a scan that turns the Nifty 500 into up to 5 executable trade cards (buy, target, stop,
quantity, exit date) using deterministic, point-in-time factors, with a backtest that shows the
price-based factors and the trade plan actually earn their keep. Phase 1a is done when the gate at
the bottom passes.

Source of truth: PRD v2 (sections *Signal engine v2*, *Trade plan engine*, *Portfolio and risk*,
*Investable universe and filters*, *Validation and go-live gates*). Where this spec and the PRD
differ, the PRD wins; raise the difference instead of coding around it.

## Design decisions (fixed for this phase)

- **Six sector-relative factors, not fixed points.** Each factor is a percentile rank (0–100)
  within the stock's sector; the composite is a weighted mean, so it is 0–100 by construction.
- **Phase 1a scores four factors**: Momentum, Volume and delivery, Quality, Valuation.
  Earnings momentum and Flows and sentiment need analyst/news history that does not exist
  point-in-time yet; they arrive in Phase 1b, forward-only. Their PRD weights are spread over the
  four factors (table below), and every card says "scored on 4 of 6 factors".
- **No model in the score.** No LLM, FinBERT or other learned model contributes to any gate,
  factor, composite, price, quantity or ranking (CLAUDE.md hard rule). Filing sentiment is a
  Phase 2 candidate behind an IC test. The only LLM in 1a writes the rationale, behind a
  number check, and parses scan prompts (T33), which I confirm before they run.
- **Macro is a gate, not a score.** The regime gate (Nifty 500 vs its 200-day average, India VIX)
  changes what may be traded; it never adds points.
- **Parameters are data.** Horizon weights, ATR multiples, thresholds and caps live in a versioned
  YAML (`config/params/v1.yaml`); every scan and backtest stores the params version and hash.

## Scope

| In scope | Out of scope (later phases) |
| --- | --- |
| Split/bonus-adjusted price series (read-time) | Kite holdings sync, GTT helper, monitor, alerts (1b) |
| Price history 2019 → today; point-in-time Nifty 500 membership | Earnings momentum, analyst and flows factors (1b, forward-only) |
| Index levels: Nifty 500, Nifty200 Momentum 30 TRI, sector indices, India VIX | Outcome labelling, calibration jobs (1b) |
| Sector taxonomy; fundamentals and lender metrics, lagged to filing date | Filing-sentiment model, Performance screen (2) |
| Universe hard filters, operator-pattern flags, soft filters, data-quality gate | LightGBM ranker, Kronos (3) |
| Momentum, Volume/delivery, Quality, Valuation factors; financials model for lenders | Core-bucket holding review (UC4) |
| Frameworks: Piotroski, Beneish, Altman Z, Mohanram, Minervini, Weinstein | Comparator UC3 (1b) |
| Composite, regime gate, missing-data handling, params registry | Cloud deployment of anything |
| Trade plan engine, sizing, costs and tax, expected net return | |
| Factor IC study (gate stage 2) and backtest (gate stage 3) | |
| Scan service, ranking, presets, append-only recommendations ledger | |
| Grounded rationale; prompt parser; Streamlit Scan + Trade card screens | |

## Folder layout (new and changed)

```
config/params/v1.yaml        # horizon weights, ATR multiples, thresholds, caps (versioned)
config/exclusions.yaml       # SEBI-order and known-bad lists, maintained by hand
src/equitypulse/
  adjust.py                  # read-time split/bonus adjustment
  sources/nse_legacy.py      # pre-UDiFF bhavcopy (before 2024-07-08)
  sources/indices.py         # + index level and VIX history
  sources/tapetide.py        # + financials, ratios, lender metrics, shareholding wrappers
  fundamentals.py            # fundamentals loader (filing-date lagged)
  universe.py                # gates, hard/soft filters, operator flags, data-quality gate
  factors/                   # momentum.py, volume.py, quality.py, valuation.py, frameworks.py
  signal.py                  # percentiles, composite, regime gate, missing-data weights
  params.py                  # load + hash the params YAML
  plan.py                    # trade plan engine, sizing, costs/tax, expected net return
  scan.py                    # scan request → ranked cards, ledger write
  rationale/                 # LangGraph graph, number validator, template fallback
  parser.py                  # prompt → scan request (catalogue-bound)
  research/ic.py             # factor IC study
  research/backtest.py       # walk-forward backtest
  ui/                        # Streamlit app: scan, trade card
tests/golden/                # hand-scored factor and plan files (from me)
```

## Database changes (one new migration, T16)

Phase 0 tables stay as they are; T3's schema is not altered, only extended. `isin_links` already
exists (added at the Phase 0 gate, migration `664b9d237a67`) and is listed here because Phase 1a
reads it.

| Table | Key columns | Notes |
| --- | --- | --- |
| `fundamentals` | isin, period_end, statement, field, filing_date (PK), value, source, fetched_at | One row per reported number per filing, so a restatement is a new row and the original stays. A factor for date D reads the latest row with `filing_date <= D` |
| `index_levels` | index_name, date (PK pair), close, tri_close, source | Nifty 500, Nifty200 Momentum 30, sector indices, India VIX |
| `features` | isin, as_of, params_version (PK), factor inputs, factor percentiles, composite per horizon, frameworks, flags, data_ok | Idempotent per (isin, as_of, params_version) |
| `param_sets` | version (PK), hash, yaml, created_at | Append-only |
| `scans` | id, created_at, request_json, request_hash, params_version, data_snapshot_ids, regime | Every scan, including ones that return no cards |
| `recommendations` | id, scan_id, isin, horizon, entry, target, stop, qty, exit_by, composite, factor scores, hit-rate estimate (p, range, n), expected_net, flags, rationale, prompt_version, row_hash, created_at | Append-only prediction ledger (ORM + trigger, like `snapshots`); outcomes are added in 1b in a separate table |
| `decisions` | id, recommendation_id, action (TAKE / SKIP), reason, decided_at | Append-only; my Take/Skip lives here because ledger rows are never edited. The latest row per recommendation wins |
| `securities` | + `sector`, `industry` filled | From the Nifty 500 constituent CSV `Industry` column; one fixed taxonomy |
| `isin_links` (exists) | old_isin, new_isin (PK pair), effective_date, reason, source | A face-value split or similar issues a new ISIN for the same security (e.g. KOTAKBANK INE237A01028 → INE237A01036). Factors and backtests read the stitched series across the chain, adjusted by the split factor |

## Data sources (additions)

| Data | Source | Notes |
| --- | --- | --- |
| Prices before 2024-07-08 | NSE legacy bhavcopy (`cm{DD}{MON}{YYYY}bhav.csv.zip`) + `sec_bhavdata_full` | Verify URL and columns on 3 dates before coding; same series rules as Phase 0 (EQ/BE/BZ) |
| Corporate actions (split/bonus) | NSE corporate-actions data | Free; ratios parsed to factors. Tapetide `get_adjustment_factors` only to cross-check a sample |
| ISIN changes | Bhavcopy: same symbol, same issuer code (ISIN characters 3–7), consecutive dates | Rule exists (Phase 0 gate); 17 links found. A symbol match alone is never enough (symbols are recycled) |
| Symbol changes | NSE symbol-change list (verify URL and columns) | Fills `symbol_history` with past symbols, so link detection also catches an ISIN change made before a rename (ACUTAAS: ISIN changed 2025-04-25 while still AMIORG) |
| Historical Nifty 500 membership | Tapetide `get_index_membership_asof`, month-ends 2019 → today | About 95 calls, spread over days within the budget; cached forever |
| Index levels, TRI, India VIX | niftyindices.com / NSE archives | Verify the endpoint; Tapetide `get_index_history` / `get_india_vix` as fallback |
| Fundamentals, ratios | Tapetide `get_batch_financials`, `get_screener_ratios` | Verify parameters and batch size with ≤ 3 live calls before coding |
| Lender metrics | Tapetide `get_lender_metrics` | Banks, NBFCs, insurers only (about 60 names) |
| Shareholding, pledge | Tapetide `get_shareholding`, `get_promoter_pledge` | For operator flags; quarterly |

Every Tapetide call goes through `sources.tapetide` and its budget guard. **Tapetide's free access
has ended** (22 Nov 2026: "The free plan covers 100 MCP tool calls a day for the first 7 days after
signup"), so T17's cross-check, T19 and T22 need a paid plan; set `TAPETIDE_DAILY_BUDGET` to the
plan's limit. Decide before T17 runs (open question 1).

## Universe and gates

Order of evaluation for every stock and date; a stock that fails any gate gets no score and a
reason the Scan screen can show.

1. **Point-in-time membership**: Nifty 500 on that date (from `index_membership` or the as-of history).
2. **Hard filters** (PRD defaults; a scan may tighten, never loosen): not on ASM/GSM/ESM/IBC that
   day; series EQ only (BE/BZ and SME excluded); market cap ≥ ₹2,000 Cr (setting ₹5,000 Cr);
   20-day median traded value ≥ ₹10 Cr; price ≥ ₹50; not in `config/exclusions.yaml`.
3. **Data quality**: price not stale (TTL 1 trading day), at least 200 trading days of history,
   fundamentals present for Quality/Valuation, fewer than 2 red flags.
4. **Operator-pattern flags**: computed from stored data; 2 or more excludes. A flag whose input is
   missing is reported as *not checked*, never as passed.
5. **Regime gate** (scan level): Nifty 500 below its 200-day average **and** India VIX above 20 →
   no new 10-day trades; 1-month and 3-month risk per trade halved.

## Factors (sector-relative percentiles, 0–100)

| Factor | Inputs (all computed for date D from data with as_of ≤ D) |
| --- | --- |
| Momentum | 1m/3m/6m adjusted return vs sector and vs Nifty 500; Minervini trend template (price > 50 > 150 > 200-day averages, 200-day rising); distance from 52-week high; 20-day breakout on volume |
| Volume and delivery | Volume vs 20-day average; delivery % vs its 60-day average |
| Quality | 3–5 year ROCE/ROE, Piotroski F, Beneish M, D/E, pledge. **Lenders** use ROA, NIM, GNPA/NNPA trend, CASA (banks), capital adequacy, credit cost; promoter holding not scored |
| Valuation | P/E and EV/EBITDA vs own 5-year median and sector median; penalises only extreme expensiveness |

Percentiles are taken within sector across the eligible universe on that date; sectors with fewer
than 8 eligible stocks use the parent group or the whole universe, and the card says which.

**Phase 1a horizon weights (%)** — PRD weights with Earnings momentum and Flows spread
proportionally over the four scored factors:

| Factor | 10 days | 1 month | 3 months |
| --- | --- | --- | --- |
| Momentum | 64.3 | 53.8 | 41.7 |
| Volume and delivery | 28.6 | 23.1 | 8.3 |
| Quality | 7.1 | 15.4 | 33.3 |
| Valuation | 0.0 | 7.7 | 16.7 |

A factor missing for one stock has its weight spread over that stock's remaining factors; it is
never scored as zero. Every backtest also runs an equal-weight version; if equal weights do as
well, the params file switches to equal weights.

## Trade plan (PRD parameters, in `config/params/v1.yaml`)

| Rule | 10 days | 1 month | 3 months |
| --- | --- | --- | --- |
| Entry (limit) | Last close, or breakout-retest level if within 1×ATR below | Last close or 20-day average pullback | Last close or 50-day average pullback |
| Stop | Entry − 1.5×ATR, or below 10-day swing low | Entry − 2×ATR, or below 20-day swing low | Entry − 2.5×ATR, or below 50-day average |
| Target | Entry + 3×ATR | Entry + 4.5×ATR | Entry + 7×ATR |
| Target cap | Nearest resistance (prior swing high / 52-week high) | Same | Same |
| Time stop | 10 trading days | 21 trading days | 63 trading days |

ATR is the 14-day average true range on adjusted prices. The wider of the ATR stop and the
structure stop is used. Reward:risk below 2.0 after the target cap is **rejected**, not squeezed.
Entry window is 2 trading days; skip if the open gaps more than 1×ATR above entry. Trailing-stop
rules are stored on the card for the 1b monitor.

**Quantity**

```latex
\text{qty} = \min\left(\left\lfloor \frac{\text{capital} \times r}{\text{entry} - \text{stop}} \right\rfloor,\ \left\lfloor \frac{\text{capital} \times 10\%}{\text{entry}} \right\rfloor,\ 1\% \times \text{ADV}_{20}\right)
```

r = 1% of Satellite capital (halved by the regime gate for 1m/3m); ADV₂₀ = 20-day average daily
volume in shares. If free cash is lower, quantity scales down and the card says so.

**Expected net return**

```latex
E = p \times G - (1 - p) \times L - c
```

p = calibrated hit rate from backtested similar setups (horizon × composite band × regime), shown
with its range and n; "insufficient history" when n < 100. c = round-trip costs (STT, stamp duty,
exchange and DP charges, slippage; about 0.3% for liquid stocks, more for mid caps), then 20% STCG;
long-term candidates also show 12.5% LTCG above ₹1.25 lakh. "Best fit" picks the horizon with the
highest E after tax.

**Ranking.** Opportunity score = composite × p × reward:risk; ties go to higher liquidity.
Cross-market scans take at most 2 per sector; per-sector cap 25% of Satellite capital and 4
stocks; at most 5 new positions per scan. Until holdings sync exists (1b), open positions come
from an optional manual list and default to none.

**Long-term flag** (PRD quality gate): Piotroski ≥ 7; 5-year average ROCE ≥ 15% (lenders: ROA ≥
1.2% and GNPA falling); Beneish M < −1.78; D/E below sector median; no red flags; price above the
200-day average.

## Scan, ledger and rationale

- Every input (filters, preset, named stocks, sector) becomes one scan request (filters + scope +
  horizon + capital + number of picks), stored in `scans` with its hash before anything runs.
- Output: regime line, up to 5 cards, 5 watch-list names, and a rejected-and-why list (gate,
  filter or rule that removed each stock).
- Every card is written to `recommendations` before it is shown, with params version, data
  snapshot ids and prompt version; rows are never edited.
- Rationale: LangGraph + OpenRouter (Haiku) writes 3–4 sentences from the card's fields only. A
  validator rejects any number not on the card and requires the stop as the invalidation level;
  two failures fall back to a template sentence. The rationale never changes a field.
- Prompt parser (Haiku) may only choose catalogue fields and values; words it cannot map are shown
  as "not understood". The structured request is shown and confirmed before the scan runs.

## Point-in-time and honesty rules

- A feature for date D reads prices with `date <= D`, fundamentals with `filing_date <= D`, and
  membership, surveillance and corporate actions as known on D. A test seeds a future row for each
  source and proves it is ignored.
- Backtests use the point-in-time Nifty 500 and include delisted members.
- The **locked holdout** (the last 12 months before the gate run) is not read by any 1a research
  code; a guard raises if a backtest window touches it. It is run once, in Phase 1b.
- LLM output is never part of a backtested signal. Factors without point-in-time history are
  labelled "untested" on the card.

## Phase 1a gate (all must pass)

1. `uv run pytest` and `uv run ruff check .` pass in CI; no network in tests.
2. **Data:** prices 2019-01-01 → today for every point-in-time Nifty 500 member, < 0.1% missing
   trading-day rows, each gap explained in `data_audit`; adjusted closes match Tapetide's
   split/bonus-adjusted closes on a 20-stock sample to within 0.01%.
3. **Golden files:** factor inputs, percentiles, composite and trade plan for 10 stocks on 2 dates
   match my hand-scored files exactly (100%).
4. **Factors (PRD stage 2), price-based factors:** for Momentum and Volume/delivery, walk-forward
   within-sector rank IC vs forward returns at each horizon has mean > 0.02 and is positive in at
   least 60% of months; a factor that fails is dropped from the params file. Quality and Valuation
   are reported; they pass the same bar only if filing-date-lagged history covers the window,
   otherwise they ship labelled "untested".
5. **Backtest (PRD stage 3):** full scan + trade plan, 2019-01 to the start of the locked holdout,
   point-in-time membership, one signal per stock per window, costs and tax: net of tax beats the
   Nifty200 Momentum 30 TRI; max drawdown < 25%; Deflated Sharpe > 0. Equal-weight variant reported.
6. **Personal validation:** the long-term quality gate passes ≥ 80% of current smallcase holdings
   (mapped via `name_map`); every stock in the known-bad list is excluded on the dates before its fall.
7. **No look-ahead:** the future-row tests in every loader and factor pass; the holdout guard test passes.
8. **Scan end-to-end:** a cross-market 1-month scan on stored data returns ≤ 5 cards, each with
   reward:risk ≥ 2.0, quantity within all caps, a rationale that passes the number check, and a
   ledger row; re-running the same request on the same data returns identical cards.
9. **Prompt parser:** 50 sample prompts match their expected filter sets exactly; unmapped words flagged.

## Open questions

1. Tapetide plan: free access has ended, and T19 (point-in-time membership) and T22
   (fundamentals) depend on it. Buy Starter (₹499/month per the PRD, 400 calls/day), or replace
   those sources (NSE index-change notices; company XBRL filings), which is much more work?
2. Satellite capital and risk per trade (PRD open question) — needed for sizing defaults.
3. Known-bad cases list and the 10 golden stocks: you provide them (T23, T24).
4. Sector taxonomy: NSE's industry labels from the Nifty 500 CSV (default), or a coarser grouping?
