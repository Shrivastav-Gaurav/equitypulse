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
- **Horizons (decided 23 Nov 2026).** 3 months is the primary horizon and the "best fit" default;
  6 months (extendable to 9) is the second. 1 month is offered but only reported at the gate.
  10 days is research-only: computed in T27/T29, not offered in scans.
- **Phase 1a scores five factors**: Momentum, Volume and delivery, Reported growth, Quality,
  Valuation. Earnings momentum is split: *Reported growth* (sales and EPS growth from
  filing-date-lagged financials) has point-in-time history and is scored now; analyst revisions
  and result surprise arrive in Phase 1b, forward-only, with Flows and sentiment. In 1a, Reported
  growth carries half the PRD Earnings-momentum weight; the other half and the Flows weight are
  spread proportionally over the scored factors (table below). Every card says "analyst and flows
  factors not yet scored".
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
| Price history 2019 → today; point-in-time Nifty 500 membership | Analyst revisions, result surprise and flows factors (1b, forward-only) |
| Index levels: Nifty 500, Nifty200 Momentum 30 TRI, sector indices, India VIX | Outcome labelling, calibration jobs (1b) |
| Sector taxonomy; fundamentals and lender metrics, lagged to filing date | Filing-sentiment model, Performance screen (2) |
| Universe hard filters, operator-pattern flags, soft filters, data-quality gate | LightGBM ranker, Kronos (3) |
| Momentum, Volume/delivery, Reported growth, Quality, Valuation factors; financials model for lenders | Core-bucket holding review (UC4); long-term conversion at time stop |
| Frameworks: Piotroski, Beneish, Altman Z, Mohanram, Minervini, Weinstein | Comparator UC3 (1b) |
| Composite, regime gate, missing-data handling, params registry | Cloud deployment of anything |
| Trade plan engine, sizing, costs and tax, expected net return | |
| Factor IC study (gate stage 2) and backtest (gate stage 3) | |
| Scan service, ranking, presets, append-only recommendations ledger | |
| Grounded rationale; prompt parser; Streamlit Scan + Trade card screens | |

## Folder layout (new and changed)

```
config/params/v1.yaml        # horizon weights, ATR multiples, thresholds, caps (versioned)
config/exclusions.yaml       # SEBI orders with order dates (applied from the order date), by hand
src/equitypulse/
  adjust.py                  # read-time split/bonus adjustment
  sources/nse_legacy.py      # pre-UDiFF bhavcopy (before 2024-07-08)
  sources/indices.py         # + index level and VIX history
  sources/tapetide.py        # + financials, ratios, lender metrics, shareholding wrappers
  fundamentals.py            # fundamentals loader (filing-date lagged)
  universe.py                # gates, hard/soft filters, operator flags, data-quality gate
  factors/                   # momentum.py, volume.py, growth.py, quality.py, valuation.py, frameworks.py
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
tests/fixtures/known_bad.yaml # known-bad test cases; never read by the scan or any filter
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
| Fundamentals, ratios | Tapetide `get_batch_financials`, `get_screener_ratios` | Verify parameters, batch size, quarterly history depth (2018-Q1 onward, for YoY growth in 2019) and whether filing dates are returned, with ≤ 3 live calls before coding. No filing date → lag to quarter end + 45 days (Q4: + 60), the SEBI results deadlines |
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
   **Forensic red flags** (added 23 Nov 2026) count with the operator flags. Non-financials only:
   cash conversion (3-year cumulative operating cash flow below 50% of cumulative net profit);
   working-capital stretch (receivable days or inventory days up more than 30% over 2 years);
   distress (Altman Z below 1.8 and interest cover below 1.5). All stocks: retail-holding surge
   (non-institutional public holding up more than 8 points in 12 months). Thresholds are priors;
   T27 reports each flag's hit rate on the known-bad cases and its false-positive rate on smallcase
   holdings. Beneish M stays a Quality input, not a flag: its sales-growth term penalises the
   growth stocks this system is looking for.
5. **Regime gate** (scan level): Nifty 500 below its 200-day average **and** India VIX above 20 →
   no new 1-month trades; 3-month and 6-month risk per trade halved.

## Factors (sector-relative percentiles, 0–100)

| Factor | Inputs (all computed for date D from data with as_of ≤ D) |
| --- | --- |
| Momentum | 1m/3m/6m adjusted return vs sector and vs Nifty 500; Minervini trend template (price > 50 > 150 > 200-day averages, 200-day rising); distance from 52-week high; 20-day breakout on volume |
| Volume and delivery | Volume vs 20-day average; delivery % vs its 60-day average |
| Quality | 3–5 year ROCE/ROE, Piotroski F, Beneish M (non-financials), D/E, pledge, cash conversion. **Lenders** use ROA, NIM, GNPA/NNPA trend, CASA (banks), capital adequacy, credit cost; promoter holding not scored |
| Valuation | P/E and EV/EBITDA vs own 5-year median and sector median; penalises only extreme expensiveness |
| Reported growth | Latest-quarter YoY and QoQ sales and EPS growth; 3-year sales CAGR. Lenders use loan-book and net-interest-income growth. Filing-date-lagged financials only |

Percentiles are taken within sector across the eligible universe on that date; sectors with fewer
than 8 eligible stocks use the parent group or the whole universe, and the card says which.

**Phase 1a horizon weights (%)** — PRD weights, with half of Earnings momentum on Reported growth
and the rest of Earnings momentum plus Flows spread proportionally over the scored factors:

| Factor | 1 month | 3 months (primary) | 6 months | 10 days (research) |
| --- | --- | --- | --- | --- |
| Momentum | 46.7 | 34.5 | 27.6 | 58.0 |
| Volume and delivery | 20.0 | 6.9 | 0.0 | 25.8 |
| Reported growth | 13.3 | 17.2 | 17.2 | 9.7 |
| Quality | 13.3 | 27.6 | 34.5 | 6.5 |
| Valuation | 6.7 | 13.8 | 20.7 | 0.0 |

If Reported growth fails the IC bar (gate check 4), its weight is spread the same way; for 3 months
that gives Momentum 41.7, Volume 8.3, Quality 33.3, Valuation 16.7.

A factor missing for one stock has its weight spread over that stock's remaining factors; it is
never scored as zero. Every backtest also runs an equal-weight version; if equal weights do as
well, the params file switches to equal weights.

## Trade plan (PRD parameters, in `config/params/v1.yaml`)

| Rule | 1 month | 3 months (primary) | 6 months (to 9) | 10 days (research) |
| --- | --- | --- | --- | --- |
| Entry (limit) | Last close or 20-day average pullback | Last close or 50-day average pullback | Last close or 50-day average pullback | Last close, or breakout-retest level if within 1×ATR below |
| Stop | Entry − 2×ATR, or below 20-day swing low | Entry − 2.5×ATR, or below 50-day average | Entry − 3.5×ATR, or below 100-day average | Entry − 1.5×ATR, or below 10-day swing low |
| Target | Entry + 4.5×ATR | Entry + 7×ATR | Entry + 10×ATR | Entry + 3×ATR |
| Target cap | Nearest resistance (prior swing high / 52-week high) | Same | Same | Same |
| Time stop | 21 trading days | 63 trading days | 126 trading days; extends to 189 if price is above its 50-day average and the trail is not hit | 10 trading days |
| Trailing stop | Breakeven at +2×ATR, then trail 2×ATR | Trail below 20-day average after +3×ATR | Breakeven at +3×ATR, then below the 50-day average on weekly closes | Breakeven at +1.5×ATR |

ATR is the 14-day average true range on adjusted prices. The wider of the ATR stop and the
structure stop is used. Reward:risk below 2.0 after the target cap is **rejected**, not squeezed.
Entry window is 2 trading days; skip if the open gaps more than 1×ATR above entry. Trailing-stop
rules are stored on the card for the 1b monitor.

**Quantity**

```latex
\text{qty} = \min\left(\left\lfloor \frac{\text{capital} \times r}{\text{entry} - \text{stop}} \right\rfloor,\ \left\lfloor \frac{\text{capital} \times 10\%}{\text{entry}} \right\rfloor,\ 1\% \times \text{ADV}_{20}\right)
```

r = 1% of Satellite capital (halved by the regime gate for 3m/6m); ADV₂₀ = 20-day average daily
volume in shares. If free cash is lower, quantity scales down and the card says so.

**Expected net return**

```latex
E = p \times G - (1 - p) \times L - c
```

p = calibrated hit rate from backtested similar setups (horizon × composite band × regime, built from every eligible setup in the backtest, not only
the top-5 picks), shown
with its range and n; "insufficient history" when n < 100. c = round-trip costs (STT, stamp duty,
exchange and DP charges, slippage; about 0.3% for liquid stocks, more for mid caps), then 20% STCG;
long-term candidates also show 12.5% LTCG above ₹1.25 lakh. "Best fit" picks, among 1 month, 3 months and 6 months, the horizon with the
highest E after tax; ties and "no horizon chosen" go to 3 months. Every hold under 12 months,
including 6–9 months, pays STCG.

**Ranking.** Opportunity score = composite × p × reward:risk; ties go to higher liquidity.
Cross-market scans take at most 2 per sector; per-sector cap 25% of Satellite capital and 4
stocks; at most 5 new positions per scan. Until holdings sync exists (1b), open positions come
from an optional manual list and default to none.

**Long-term flag** (PRD quality gate): Piotroski ≥ 7; 5-year average ROCE ≥ 15% (lenders: ROA ≥
1.2% and GNPA falling); Beneish M < −1.78; D/E below sector median; no red flags; price above the
200-day average. Until the holding review (UC4) exists the flag is informational: a trade is never
converted to a long-term holding at its time stop; it exits by its own rules.

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

## Backtest fill and exit rules

End-of-day data cannot show the order of moves inside a day, so the backtest is conservative:

- If a day's high reaches the target and its low reaches the stop, the stop is taken.
- A gap down through the stop fills at that day's open, not at the stop; a gap up through the
  target fills at the target.
- Entry fills only if the limit trades inside the entry window; an open more than 1×ATR above
  entry is a skip.
- Trailing-stop changes are computed after the close and apply from the next session, as the GTT
  would be modified live.
- An early-exit variant (exit at the next open when the composite falls below its 40th
  percentile) runs against the plain rules and is adopted only if it does better net of costs
  and tax.

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
   match my hand-scored files exactly (100%). Stocks: NTPC, COALINDIA, HINDALCO, KEC, DATAPATTNS,
   TITAGARH, INDIANB, CANFINHOME, MFSL, REDINGTON; dates 2024-09-27 and 2025-06-30. A fixture
   test shows LENSKART excluded by the 200-day history rule while it has fewer than 200 days.
4. **Factors (PRD stage 2), price-based factors:** for Momentum and Volume/delivery, walk-forward
   within-sector rank IC vs forward returns at each horizon has mean > 0.02 and is positive in at
   least 60% of months; a factor that fails is dropped from the params file. Quality and Valuation
   are reported; they pass the same bar only if filing-date-lagged history covers the window,
   otherwise they ship labelled "untested". Reported growth and Mohanram G are tested on the same
   bar; Reported growth keeps its weight only if it passes. For 3-month and 6-month forward returns,
   significance uses non-overlapping windows (or Newey-West errors).
5. **Backtest (PRD stage 3):** full scan + trade plan, 2019-01 to the start of the locked holdout,
   point-in-time membership, one signal per stock per window, costs, tax and the fill rules above:
   for **3 months and 6 months each**, net of tax beats the Nifty200 Momentum 30 TRI; max drawdown
   < 25%; Deflated Sharpe > 0. 1 month and 10 days are reported, not gated; a horizon that fails is
   not offered in scans. Equal-weight and early-exit variants reported.
5b. **Robustness (PRD stage 4), 3 and 6 months:** the top 5 beat at least 90% of 1,000 random
   5-stock portfolios drawn from the same filtered universe on the same dates; results reported
   separately for the 2020 crash, the 2021 bull run and the 2024–25 mid-cap correction; the
   composite beats a naive top 5 by 6-month return; each threshold ±20% leaves pass/fail unchanged.
6. **Personal validation:** the long-term quality gate passes ≥ 80% of current smallcase holdings
   that are Nifty 500 members (37 of 81 on 2026-11-09; the out-of-universe count is reported
   separately), mapped via `name_map` with every match verified; known-bad cases (`tests/fixtures/known_bad.yaml`) are run with
   `config/exclusions.yaml` switched off: no Tier A case (warning signs in the data) appears as a card in
   the 90 trading days before its event, and the report names the rule that stopped it; no Tier B case
   (sudden shock) opened in that window loses more than 3% of Satellite capital. Cases outside the
   point-in-time Nifty 500 are reported, not scored. Each red flag's false-positive rate on in-universe
   smallcase holdings is reported; a flag excluding more than 20% of them is reviewed before the gate.
7. **No look-ahead:** the future-row tests in every loader and factor pass; the holdout guard test passes.
8. **Scan end-to-end:** a cross-market 3-month scan on stored data returns ≤ 5 cards, each with
   reward:risk ≥ 2.0, quantity within all caps, a rationale that passes the number check, and a
   ledger row; re-running the same request on the same data returns identical cards.
9. **Prompt parser:** 50 sample prompts match their expected filter sets exactly; unmapped words flagged.

## Open questions

1. Tapetide plan: free access has ended, and T19 (point-in-time membership) and T22
   (fundamentals) depend on it. Buy Starter (₹499/month per the PRD, 400 calls/day), or replace
   those sources (NSE index-change notices; company XBRL filings), which is much more work?
2. Satellite capital and risk per trade (PRD open question) — needed for sizing defaults.
3. Known-bad cases: a 16-stock starter set is in `tests/fixtures/known_bad.yaml`; T18 refines event
   dates from prices and T19 verifies Nifty 500 membership. The 10 golden stocks are fixed (gate check 3);
   Claude builds the hand-scored workbook with every formula visible and you spot-check it.
4. Sector taxonomy: NSE's industry labels from the Nifty 500 CSV (default), or a coarser grouping?
