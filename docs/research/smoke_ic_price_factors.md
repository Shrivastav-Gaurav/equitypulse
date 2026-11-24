# Smoke test: do price factors predict returns? [SURVIVORSHIP-BIASED]

Generated 2026-11-24. Universe: **NIFTY500 members as of 2026-11-24 [SURVIVORSHIP-BIASED]**. Window 2019-01-01 to 2025-11-01; the locked holdout starts 2025-11-01 and was not read.

> **SURVIVORSHIP-BIASED.** The universe is today's index members applied to every past date, so stocks that fell out of the index (often after falling) are missing. This flatters momentum. A pass here is only encouraging; a fail is a stop signal.

**Verdict: PASS (encouraging only)**: momentum clears the bar at 3m and 6m [SURVIVORSHIP-BIASED]. 1m: mean IC -0.010, 51% positive, t -0.63 over 72 periods; 3m: mean IC 0.044, 70% positive, t 1.78 over 23 periods; 6m: mean IC 0.056, 75% positive, t 1.85 over 12 periods; 10d: mean IC -0.018, 49% positive, t -1.11 over 72 periods. The bar is met, but at 3m, 6m the t-statistic is below 2 on few independent periods: not statistically significant at the 5% level.

## Summary by factor and horizon [SURVIVORSHIP-BIASED]

| Factor | Horizon | Evaluated on | Periods | Mean IC | % positive | t | Top-bottom decile spread | Top decile minus EW | Passes bar |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Momentum | 1m | monthly | 72 | -0.010 | 51.4% | -0.63 | 0.4% | 0.5% | no |
| Momentum | 3m | non-overlapping (every 3 months) | 23 | 0.044 | 69.6% | 1.78 | 5.1% | 3.3% | yes |
| Momentum | 6m | non-overlapping (every 6 months) | 12 | 0.056 | 75.0% | 1.85 | 7.3% | 5.5% | yes |
| Momentum | 10d (research) | monthly | 72 | -0.018 | 48.6% | -1.11 | -0.1% | 0.2% | no |
| Volume and delivery | 1m | monthly | 72 | 0.001 | 62.5% | 0.08 | -0.6% | -0.3% | no |
| Volume and delivery | 3m | non-overlapping (every 3 months) | 23 | 0.005 | 43.5% | 0.33 | -1.2% | -0.3% | no |
| Volume and delivery | 6m | non-overlapping (every 6 months) | 12 | 0.005 | 58.3% | 0.17 | -1.1% | -1.3% | no |
| Volume and delivery | 10d (research) | monthly | 72 | -0.000 | 48.6% | -0.00 | -0.4% | -0.2% | no |

Bar: mean IC > 0.02 and positive in at least 60% of evaluated periods. Spreads and excess returns are mean forward returns per period over the horizon, before costs and tax.

## Top-decile momentum vs the equal-weight universe [SURVIVORSHIP-BIASED]

| Horizon | Periods | Top decile, compounded | Equal weight, compounded | Mean excess per period |
| --- | --- | --- | --- | --- |
| 1m | 72 | 626.7% | 398.6% | 0.5% |
| 3m | 23 | 648.3% | 279.1% | 3.3% |
| 6m | 12 | 546.4% | 282.8% | 5.5% |
| 10d | 72 | 230.7% | 193.5% | 0.2% |

Compounded over the evaluated (non-overlapping for 3m/6m) periods; 1m and 10d compound monthly, so 10d leaves cash idle between formation dates.

## Momentum: results by year [SURVIVORSHIP-BIASED]

Evaluation dates as above (3m and 6m: non-overlapping, so 4 and 2 per year).

| Year | 1m mean IC (n) | 3m mean IC (n) | 6m mean IC (n) | 10d mean IC (n) |
| --- | --- | --- | --- | --- |
| 2019 | 0.055 (3) | 0.095 (1) | 0.285 (1) | 0.060 (3) |
| 2020 | -0.061 (12) | -0.055 (4) | -0.083 (2) | -0.061 (12) |
| 2021 | -0.008 (12) | 0.101 (4) | 0.065 (2) | -0.018 (12) |
| 2022 | 0.041 (12) | 0.099 (4) | 0.120 (2) | -0.005 (12) |
| 2023 | 0.010 (12) | 0.110 (4) | 0.073 (2) | -0.009 (12) |
| 2024 | -0.018 (12) | -0.008 (4) | 0.026 (2) | 0.003 (12) |
| 2025 | -0.053 (9) | -0.034 (2) | -0.016 (1) | -0.047 (9) |

## Volume and delivery: results by year [SURVIVORSHIP-BIASED]

Evaluation dates as above (3m and 6m: non-overlapping, so 4 and 2 per year).

| Year | 1m mean IC (n) | 3m mean IC (n) | 6m mean IC (n) | 10d mean IC (n) |
| --- | --- | --- | --- | --- |
| 2019 | 0.007 (3) | 0.080 (1) | 0.062 (1) | -0.023 (3) |
| 2020 | -0.014 (12) | 0.003 (4) | 0.036 (2) | -0.001 (12) |
| 2021 | -0.003 (12) | 0.008 (4) | 0.038 (2) | -0.014 (12) |
| 2022 | -0.008 (12) | -0.046 (4) | -0.072 (2) | 0.006 (12) |
| 2023 | 0.027 (12) | -0.003 (4) | -0.013 (2) | 0.011 (12) |
| 2024 | -0.010 (12) | 0.064 (4) | 0.043 (2) | -0.011 (12) |
| 2025 | 0.014 (9) | -0.031 (2) | -0.070 (1) | 0.019 (9) |

## Setup [SURVIVORSHIP-BIASED]

- Gates: series EQ; raw price >= 50; 20-day median traded value >= Rs 10 Cr; >= 200 trading days of history. Eligible stocks on the 73 scored formation dates (2019-10-31 to 2025-10-31): median 291 (min 166, max 419).
- Formation on each month-end trading day; forward return from that close to the close 1m = 21, 3m = 63, 6m = 126, 10d = 10 trading days later (a stock that stops trading keeps its last price). Split/bonus-adjusted, ISIN-stitched prices.
- IC: Spearman correlation of the factor's within-sector percentile with the forward return's within-sector percentile. Deciles on the factor percentile (top >= 90, bottom <= 10).
- Momentum: equal-weight mean of within-sector percentiles of 1m/3m/6m return, distance from the 52-week high, trend template (price > 50 > 150 > 200-day average, 200-day rising) and 20-day breakout on 1.5x volume; masked for 252 bars after a demerger. Volume and delivery: 5-day vs 20-day volume and 5-day vs 60-day delivery %.
- Not yet in place: point-in-time index membership (T19), the full T23 gates, golden files, costs and tax. The first ~12 months of the window build history, so formation starts once 200+ bars exist.
