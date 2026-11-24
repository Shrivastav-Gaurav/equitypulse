"""Factor IC study (T27, Phase 1a gate check 4).

For each month-end formation date D in the window, every eligible stock in the universe gets its
factor percentiles from data up to D (factors.cross_section), and its forward return over each
horizon from D's close to the close h trading days later. Per date and horizon:
  - rank IC: Spearman correlation between the factor's within-sector percentile and the forward
    return's within-sector percentile (a within-sector IC);
  - decile spread: mean forward return of the top decile minus the bottom decile;
  - top decile minus the equal-weight universe.
1m and 10d are evaluated on every month-end; 3m and 6m on non-overlapping formation dates
(every 3rd / 6th month-end), so their months are independent.

The universe is a parameter. A universe built from today's index members is survivorship-
biased: every output then carries that label. The study refuses any window that ends after the
start of the locked holdout, and reads no price dated after the window end.
"""

import datetime as dt
import math
from collections.abc import Callable

import pandas as pd
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.adjust import adjusted_prices, chain_as_of
from equitypulse.db.models import CorporateAction, Security, TradingCalendar
from equitypulse.factors.cross_section import cross_section, security_inputs
from equitypulse.factors.ranking import sector_percentiles

HORIZON_DAYS = {"1m": 21, "3m": 63, "6m": 126, "10d": 10}
RESEARCH_ONLY = ("10d",)
NON_OVERLAP_STEP = {"1m": 1, "3m": 3, "6m": 6, "10d": 1}  # in month-ends
FACTORS = ("pct_momentum", "pct_volume_delivery")
BAR_MEAN_IC = 0.02
BAR_POSITIVE = 0.60
MIN_STOCKS = 30
LOOKBACK = dt.timedelta(days=400)  # enough calendar days for 252 trading bars before `start`
SURVIVORSHIP_LABEL = "SURVIVORSHIP-BIASED"


class HoldoutError(RuntimeError):
    """A research window reached into the locked holdout."""


def holdout_start(today: dt.date, months: int = 12) -> dt.date:
    """First day of the month `months` before today: 2026-11-24 -> 2025-11-01."""
    month_index = today.year * 12 + (today.month - 1) - months
    return dt.date(month_index // 12, month_index % 12 + 1, 1)


def guard_window(end: dt.date, holdout: dt.date) -> None:
    if end > holdout:
        raise HoldoutError(f"window ends {end}, after the locked holdout starts on {holdout}")


class Universe(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    members: Callable[[dt.date], frozenset[str]]
    survivorship_biased: bool

    @property
    def label(self) -> str:
        return f"{self.name} [{SURVIVORSHIP_LABEL}]" if self.survivorship_biased else self.name


class Gates(BaseModel):
    """The smoke test's minimal gates (the full T23 universe comes later)."""

    model_config = ConfigDict(frozen=True)

    series: tuple[str, ...] = ("EQ",)
    min_price: float = 50.0
    min_median_traded_value: float = 1e8  # INR, = 10 Cr
    traded_value_days: int = 20
    min_history_days: int = 200


class ICStudy(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    universe: str
    survivorship_biased: bool
    start: dt.date
    end: dt.date
    holdout: dt.date
    gates: Gates
    records: pd.DataFrame  # one row per formation date x horizon x factor
    eligible_counts: pd.Series  # formation date -> eligible stocks


def run_ic_study(
    session: Session,
    universe: Universe,
    start: dt.date,
    end: dt.date,
    *,
    holdout: dt.date,
    gates: Gates = Gates(),  # noqa: B008
    horizons: dict[str, int] = HORIZON_DAYS,
) -> ICStudy:
    guard_window(end, holdout)
    calendar = sorted(session.scalars(select(TradingCalendar.date).where(
        TradingCalendar.is_trading_day.is_(True), TradingCalendar.date <= end,
    )))
    members = sorted(universe.members(end))
    sectors = pd.Series({
        isin: (sec.sector if (sec := session.get(Security, isin)) and sec.sector else "UNKNOWN")
        for isin in members
    })
    panel = _load_panel(session, members, start, end, gates)

    in_window = [d for d in calendar if start <= d <= end]
    month_ends = [d for d, nxt in zip(in_window, [*in_window[1:], None], strict=True)
                  if nxt is None or nxt.month != d.month]
    position = {d: i for i, d in enumerate(calendar)}
    records, counts = [], {}
    for d in month_ends:
        rows = {isin: frame.loc[d] for isin, frame in panel.items()
                if d in frame.index and bool(frame.at[d, "eligible"])}
        counts[d] = len(rows)
        if len(rows) < MIN_STOCKS:
            continue
        today = pd.DataFrame(rows).T
        flags = ["momentum_blocked", "plan_blocked", "eligible"]
        numeric = [c for c in today.columns if c not in flags]
        today[numeric] = today[numeric].astype(float)
        today[flags] = today[flags].astype(bool)
        factors = cross_section(today, sectors.reindex(today.index))
        for horizon, days in horizons.items():
            i = position[d] + days
            if i >= len(calendar):
                continue  # the forward window would pass the window end
            exit_day = calendar[i]
            fwd = pd.Series({isin: _forward_return(panel[isin], d, exit_day) for isin in rows})
            for factor in FACTORS:
                records.append(_score(d, horizon, factor, factors[factor], fwd, sectors))
    return ICStudy(
        universe=universe.label, survivorship_biased=universe.survivorship_biased,
        start=start, end=end, holdout=holdout, gates=gates,
        records=pd.DataFrame(records), eligible_counts=pd.Series(counts, dtype=float),
    )


def _load_panel(session: Session, isins: list[str], start: dt.date, end: dt.date,
                gates: Gates) -> dict[str, pd.DataFrame]:
    panel = {}
    for isin in isins:
        df = adjusted_prices(session, isin, end, start=start - LOOKBACK)  # nothing after `end`
        if df.empty:
            continue
        chain = chain_as_of(session, isin, end)
        demergers = session.scalars(select(CorporateAction.ex_date).where(
            CorporateAction.isin.in_(chain), CorporateAction.action_type == "DEMERGER",
            CorporateAction.ex_date <= end,
        )).all()
        inputs = security_inputs(df, demergers)
        d = df.set_index("date")
        raw_close = d["close"] / d["factor"]
        median_value = d["traded_value"].rolling(
            gates.traded_value_days, min_periods=gates.traded_value_days
        ).median()
        history = pd.Series(range(1, len(d) + 1), index=d.index)
        inputs["eligible"] = (
            d["series"].isin(gates.series) & (raw_close >= gates.min_price)
            & (median_value >= gates.min_median_traded_value)
            & (history >= gates.min_history_days)
        )
        inputs["close"] = d["close"]
        panel[isin] = inputs
    return panel


def _forward_return(frame: pd.DataFrame, entry: dt.date, exit_day: dt.date) -> float:
    """Close-to-close, to the last close on or before `exit_day` (a stock that stops trading
    keeps its last price); NaN if it never trades after entry."""
    closes = frame["close"]
    after = closes[(closes.index > entry) & (closes.index <= exit_day)]
    if after.empty:
        return float("nan")
    return float(after.iloc[-1] / closes.loc[entry] - 1)


def _score(d: dt.date, horizon: str, factor: str, values: pd.Series, fwd: pd.Series,
           sectors: pd.Series) -> dict:
    frame = pd.DataFrame({"f": values, "r": fwd}).dropna()
    rec = {"date": d, "horizon": horizon, "factor": factor, "n": len(frame), "ic": float("nan"),
           "spread": float("nan"), "top_ret": float("nan"), "ew_ret": float("nan"),
           "top_minus_ew": float("nan")}
    if len(frame) < MIN_STOCKS:
        return rec
    r_pct = sector_percentiles(frame["r"], sectors.reindex(frame.index))
    rec["ic"] = float(frame["f"].rank().corr(r_pct.rank()))  # Spearman = Pearson on ranks
    top, bottom = frame[frame["f"] >= 90], frame[frame["f"] <= 10]
    rec["top_ret"], rec["ew_ret"] = float(top["r"].mean()), float(frame["r"].mean())
    rec["spread"] = float(top["r"].mean() - bottom["r"].mean())
    rec["top_minus_ew"] = rec["top_ret"] - rec["ew_ret"]
    return rec


# ── summaries ─────────────────────────────────────────────────────────────────


class Summary(BaseModel):
    factor: str
    horizon: str
    evaluation: str  # "monthly" or "non-overlapping (every N months)"
    periods: int
    mean_ic: float
    pct_positive: float
    t_stat: float
    mean_spread: float
    mean_top_minus_ew: float
    top_growth: float  # compounded top-decile return over the evaluation periods
    ew_growth: float
    passes: bool
    research_only: bool


def evaluation_rows(records: pd.DataFrame, factor: str, horizon: str) -> pd.DataFrame:
    rows = records[(records["factor"] == factor) & (records["horizon"] == horizon)]
    rows = rows.dropna(subset=["ic"]).sort_values("date")
    step = NON_OVERLAP_STEP[horizon]
    return rows.iloc[::step]


def summarise(study: ICStudy) -> list[Summary]:
    out = []
    for factor in FACTORS:
        for horizon in study.records["horizon"].unique():
            rows = evaluation_rows(study.records, factor, horizon)
            if rows.empty:
                continue
            ic = rows["ic"]
            sd = ic.std(ddof=1)
            mean = float(ic.mean())
            pos = float((ic > 0).mean())
            step = NON_OVERLAP_STEP[horizon]
            out.append(Summary(
                factor=factor, horizon=horizon,
                evaluation="monthly" if step == 1 else f"non-overlapping (every {step} months)",
                periods=len(rows), mean_ic=mean, pct_positive=pos,
                t_stat=float(mean / sd * math.sqrt(len(rows))) if sd and len(rows) > 1 else 0.0,
                mean_spread=float(rows["spread"].mean()),
                mean_top_minus_ew=float(rows["top_minus_ew"].mean()),
                top_growth=float((1 + rows["top_ret"].fillna(0)).prod() - 1),
                ew_growth=float((1 + rows["ew_ret"].fillna(0)).prod() - 1),
                passes=mean > BAR_MEAN_IC and pos >= BAR_POSITIVE,
                research_only=horizon in RESEARCH_ONLY,
            ))
    return out


def by_year(study: ICStudy, factor: str, horizon: str) -> pd.DataFrame:
    """Mean IC by calendar year of formation, on the evaluation dates."""
    rows = evaluation_rows(study.records, factor, horizon)
    years = pd.Series([d.year for d in rows["date"]], index=rows.index)
    return rows.groupby(years).agg(periods=("ic", "size"), mean_ic=("ic", "mean"),
                                   pct_positive=("ic", lambda s: (s > 0).mean()),
                                   mean_spread=("spread", "mean"))
