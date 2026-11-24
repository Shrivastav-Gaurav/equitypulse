"""Markdown report for an IC study. Every heading and table names the universe label, so a
survivorship-biased result cannot be quoted without its caveat."""

import datetime as dt
from pathlib import Path

from equitypulse.research.ic import (
    BAR_MEAN_IC,
    BAR_POSITIVE,
    FACTORS,
    MIN_STOCKS,
    ICStudy,
    Summary,
    by_year,
    summarise,
)

NAMES = {"pct_momentum": "Momentum", "pct_volume_delivery": "Volume and delivery"}
HORIZON_ORDER = ["1m", "3m", "6m", "10d"]


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_report(study: ICStudy, path: Path, *, generated: dt.date) -> list[Summary]:
    summaries = sorted(summarise(study), key=lambda s: (FACTORS.index(s.factor),
                                                        HORIZON_ORDER.index(s.horizon)))
    label = study.universe
    tag = f" [{'SURVIVORSHIP-BIASED' if study.survivorship_biased else 'point-in-time'}]"
    lines = [
        f"# Smoke test: do price factors predict returns?{tag}",
        "",
        f"Generated {generated}. Universe: **{label}**. Window {study.start} to {study.end}; "
        f"the locked holdout starts {study.holdout} and was not read.",
        "",
    ]
    if study.survivorship_biased:
        lines += [
            "> **SURVIVORSHIP-BIASED.** The universe is today's index members applied to every "
            "past date, so stocks that fell out of the index (often after falling) are missing. "
            "This flatters momentum. A pass here is only encouraging; a fail is a stop signal.",
            "",
        ]
    lines += [_verdict(summaries, tag), "", f"## Summary by factor and horizon{tag}", "",
              "| Factor | Horizon | Evaluated on | Periods | Mean IC | % positive | t | "
              "Top-bottom decile spread | Top decile minus EW | Passes bar |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        horizon = f"{s.horizon} (research)" if s.research_only else s.horizon
        lines.append(
            f"| {NAMES[s.factor]} | {horizon} | {s.evaluation} | {s.periods} | {s.mean_ic:.3f} | "
            f"{_pct(s.pct_positive)} | {s.t_stat:.2f} | {_pct(s.mean_spread)} | "
            f"{_pct(s.mean_top_minus_ew)} | {'yes' if s.passes else 'no'} |"
        )
    lines += ["", f"Bar: mean IC > {BAR_MEAN_IC} and positive in at least "
              f"{BAR_POSITIVE:.0%} of evaluated periods. Spreads and excess returns are mean "
              "forward returns per period over the horizon, before costs and tax.", ""]

    lines += [f"## Top-decile momentum vs the equal-weight universe{tag}", "",
              "| Horizon | Periods | Top decile, compounded | Equal weight, compounded | "
              "Mean excess per period |", "| --- | --- | --- | --- | --- |"]
    for s in summaries:
        if s.factor == "pct_momentum":
            lines.append(f"| {s.horizon} | {s.periods} | {_pct(s.top_growth)} | "
                         f"{_pct(s.ew_growth)} | {_pct(s.mean_top_minus_ew)} |")
    lines += ["", "Compounded over the evaluated (non-overlapping for 3m/6m) periods; 1m and 10d "
              "compound monthly, so 10d leaves cash idle between formation dates.", ""]

    for factor in FACTORS:
        lines += [f"## {NAMES[factor]}: results by year{tag}", "",
                  "Evaluation dates as above (3m and 6m: non-overlapping, so 4 and 2 per year).",
                  "",
                  "| Year | " + " | ".join(f"{h} mean IC (n)" for h in HORIZON_ORDER) + " |",
                  "| --- | " + " | ".join("---" for _ in HORIZON_ORDER) + " |"]
        tables = {h: by_year(study, factor, h) for h in HORIZON_ORDER}
        years = sorted({y for t in tables.values() for y in t.index})
        for y in years:
            cells = []
            for h in HORIZON_ORDER:
                t = tables[h]
                cells.append(f"{t.at[y, 'mean_ic']:.3f} ({int(t.at[y, 'periods'])})"
                             if y in t.index else "")
            lines.append(f"| {y} | " + " | ".join(cells) + " |")
        lines.append("")

    counts = study.eligible_counts
    counts = counts[counts >= MIN_STOCKS]  # the formation dates actually scored
    lines += [
        f"## Setup{tag}", "",
        f"- Gates: series {', '.join(study.gates.series)}; raw price >= "
        f"{study.gates.min_price:.0f}; {study.gates.traded_value_days}-day median traded value "
        f">= Rs {study.gates.min_median_traded_value / 1e7:.0f} Cr; >= "
        f"{study.gates.min_history_days} trading days of history. Eligible stocks on the "
        f"{len(counts)} scored formation dates ({min(counts.index)} to {max(counts.index)}): "
        f"median {counts.median():.0f} (min {counts.min():.0f}, max {counts.max():.0f}).",
        "- Formation on each month-end trading day; forward return from that close to the close "
        "1m = 21, 3m = 63, 6m = 126, 10d = 10 trading days later (a stock that stops trading "
        "keeps its last price). Split/bonus-adjusted, ISIN-stitched prices.",
        "- IC: Spearman correlation of the factor's within-sector percentile with the forward "
        "return's within-sector percentile. Deciles on the factor percentile (top >= 90, "
        "bottom <= 10).",
        "- Momentum: equal-weight mean of within-sector percentiles of 1m/3m/6m return, distance "
        "from the 52-week high, trend template (price > 50 > 150 > 200-day average, 200-day "
        "rising) and 20-day breakout on 1.5x volume; masked for 252 bars after a demerger. "
        "Volume and delivery: 5-day vs 20-day volume and 5-day vs 60-day delivery %.",
        "- Not yet in place: point-in-time index membership (T19), the full T23 gates, golden "
        "files, costs and tax. The first ~12 months of the window build history, so formation "
        "starts once 200+ bars exist.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return summaries


def _verdict(summaries: list[Summary], tag: str) -> str:
    mom = {s.horizon: s for s in summaries if s.factor == "pct_momentum"}
    gated = [mom[h] for h in ("3m", "6m") if h in mom]
    if gated and all(s.passes for s in gated):
        head = "**Verdict: PASS (encouraging only)**: momentum clears the bar at 3m and 6m"
    elif gated and any(s.passes for s in gated):
        passed = [s.horizon for s in gated if s.passes]
        head = f"**Verdict: MIXED**: momentum clears the bar only at {', '.join(passed)}"
    else:
        head = "**Verdict: FAIL (stop signal)**: momentum does not clear the bar at 3m or 6m"
    detail = "; ".join(
        f"{h}: mean IC {s.mean_ic:.3f}, {s.pct_positive:.0%} positive, t {s.t_stat:.2f} "
        f"over {s.periods} periods" for h, s in mom.items()
    )
    weak = [s.horizon for s in gated if s.passes and abs(s.t_stat) < 2]
    caution = (
        f" The bar is met, but at {', '.join(weak)} the t-statistic is below 2 on few "
        "independent periods: not statistically significant at the 5% level."
        if weak else ""
    )
    return f"{head}{tag}. {detail}.{caution}"
