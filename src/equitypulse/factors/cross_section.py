"""From one security's daily frame to inputs, and from a date's inputs to factor percentiles.

Factor = within-sector percentile of the mean of its inputs' within-sector percentiles, so each
input counts equally and the factor is again 0-100. Momentum needs at least
MIN_MOMENTUM_INPUTS of its inputs; with fewer (or across a demerger) it is missing, never 0.
"""

import datetime as dt
from collections.abc import Iterable

import pandas as pd

from equitypulse.factors import momentum, volume
from equitypulse.factors.demerger import ATR_WINDOW_DAYS, spans_demerger
from equitypulse.factors.ranking import sector_percentiles

MIN_MOMENTUM_INPUTS = 4
MIN_VOLUME_INPUTS = 1


def security_inputs(df: pd.DataFrame, demerger_ex_dates: Iterable[dt.date] = ()) -> pd.DataFrame:
    """Momentum and volume inputs for every bar, plus the two demerger flags."""
    ex = list(demerger_ex_dates)
    out = momentum.momentum_inputs(df).join(volume.volume_inputs(df))
    out["momentum_blocked"] = spans_demerger(out.index, ex, momentum.LOOKBACK_DAYS)
    out["plan_blocked"] = spans_demerger(out.index, ex, ATR_WINDOW_DAYS)
    return out


def _factor(rows: pd.DataFrame, inputs: list[str], sectors: pd.Series, min_inputs: int
            ) -> tuple[pd.Series, pd.DataFrame]:
    pcts = pd.DataFrame({c: sector_percentiles(rows[c], sectors) for c in inputs})
    enough = pcts.notna().sum(axis=1) >= min_inputs
    composite = pcts.mean(axis=1, skipna=True).where(enough)
    return sector_percentiles(composite, sectors), pcts


def cross_section(rows: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """`rows`: one row per security (index ISIN) of `security_inputs` on one date."""
    mom_rows = rows[momentum.SCORE_INPUTS].copy()
    mom_rows[rows["momentum_blocked"].astype(bool)] = float("nan")
    pct_momentum, mom_pcts = _factor(mom_rows, momentum.SCORE_INPUTS, sectors,
                                     MIN_MOMENTUM_INPUTS)
    pct_volume, vol_pcts = _factor(rows, volume.SCORE_INPUTS, sectors, MIN_VOLUME_INPUTS)
    out = pd.DataFrame({"pct_momentum": pct_momentum, "pct_volume_delivery": pct_volume})
    out = out.join(mom_pcts.add_prefix("pct_")).join(vol_pcts.add_prefix("pct_"))
    out["plan_blocked"] = rows["plan_blocked"].astype(bool)
    out["momentum_blocked"] = rows["momentum_blocked"].astype(bool)
    return out
