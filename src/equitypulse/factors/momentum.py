"""Momentum inputs for one security, for every date in its history.

Inputs (spec, "Factors"): 1m/3m/6m returns; trend template (price > 50 > 150 > 200-day
averages, 200-day rising); distance from the 52-week high; 20-day breakout on volume. Returns
are ranked within sector, so "vs sector" and "vs Nifty 500" give the same ranking as the raw
return: subtracting one benchmark return from every stock on a date does not change the order.
The Weinstein stage is computed and stored as a framework; it does not enter the score.

Thresholds here (breakout volume multiple, slope look-back) are not yet in the params file:
they move to params v2 together with the golden-file definitions.
"""

import pandas as pd

RETURN_DAYS = {"ret_1m": 21, "ret_3m": 63, "ret_6m": 126}
HIGH_52W_DAYS = 252
LOOKBACK_DAYS = 252  # longest momentum window: the demerger mask spans this
BREAKOUT_DAYS = 20
BREAKOUT_VOLUME_MULTIPLE = 1.5
MA200_RISING_DAYS = 21
WEINSTEIN_SLOPE_DAYS = 20

SCORE_INPUTS = ["ret_1m", "ret_3m", "ret_6m", "dist_52w_high", "trend_template", "breakout"]


def momentum_inputs(df: pd.DataFrame) -> pd.DataFrame:
    """One row per bar of `df` (columns date, high, close, volume), indexed by date."""
    d = df.set_index("date").sort_index()
    close, high, volume = d["close"], d["high"], d["volume"]
    out = pd.DataFrame(index=d.index)
    for name, days in RETURN_DAYS.items():
        out[name] = close / close.shift(days) - 1
    out["dist_52w_high"] = close / high.rolling(HIGH_52W_DAYS, min_periods=HIGH_52W_DAYS).max() - 1

    ma50 = close.rolling(50, min_periods=50).mean()
    ma150 = close.rolling(150, min_periods=150).mean()
    ma200 = close.rolling(200, min_periods=200).mean()
    rising = ma200 > ma200.shift(MA200_RISING_DAYS)
    checks = pd.concat([close > ma50, ma50 > ma150, ma150 > ma200, rising], axis=1)
    out["trend_template"] = checks.sum(axis=1).astype(float).where(
        ma200.shift(MA200_RISING_DAYS).notna()
    )

    prior_high = high.rolling(BREAKOUT_DAYS, min_periods=BREAKOUT_DAYS).max().shift(1)
    prior_volume = volume.rolling(BREAKOUT_DAYS, min_periods=BREAKOUT_DAYS).mean().shift(1)
    breakout = (close > prior_high) & (volume > BREAKOUT_VOLUME_MULTIPLE * prior_volume)
    out["breakout"] = breakout.astype(float).where(prior_high.notna() & prior_volume.notna())

    slope = ma150 / ma150.shift(WEINSTEIN_SLOPE_DAYS) - 1
    above = close > ma150
    stage = pd.Series(float("nan"), index=d.index)
    stage[above & (slope > 0)] = 2
    stage[above & (slope <= 0)] = 3
    stage[~above & (slope < 0)] = 4
    stage[~above & (slope >= 0)] = 1
    out["weinstein_stage"] = stage.where(slope.notna())
    return out
