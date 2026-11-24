"""Volume and delivery inputs (spec: volume vs its 20-day average; delivery % vs its 60-day
average). Each compares the last 5 sessions with the longer average, so a single print does
not decide it. Bulk/block deals are not loaded yet and are not scored.

Delivery is missing for BE/BZ rows (NSE publishes none); the ratio is then NaN for that date
and the factor is scored on volume alone.
"""

import pandas as pd

RECENT_DAYS = 5
VOLUME_BASE_DAYS = 20
DELIVERY_BASE_DAYS = 60
SCORE_INPUTS = ["volume_ratio", "delivery_ratio"]


def volume_inputs(df: pd.DataFrame) -> pd.DataFrame:
    d = df.set_index("date").sort_index()
    volume = d["volume"].astype(float)
    delivery = d["delivery_pct"].astype(float)
    out = pd.DataFrame(index=d.index)
    out["volume_ratio"] = (
        volume.rolling(RECENT_DAYS, min_periods=RECENT_DAYS).mean()
        / volume.rolling(VOLUME_BASE_DAYS, min_periods=VOLUME_BASE_DAYS).mean()
    )
    out["delivery_ratio"] = (
        delivery.rolling(RECENT_DAYS, min_periods=RECENT_DAYS).mean()
        / delivery.rolling(DELIVERY_BASE_DAYS, min_periods=DELIVERY_BASE_DAYS).mean()
    )
    return out
