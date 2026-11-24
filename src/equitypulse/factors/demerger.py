"""The T24 demerger rule.

A demerger carries no price factor, so any window that spans its ex-date compares prices of two
different businesses. If the momentum window (the longest momentum look-back) spans a demerger,
momentum is missing for that date and its weight spreads; if the ATR window spans one, no trade
plan is made. Both clear once the window no longer reaches back to the ex-date.
"""

import datetime as dt
from collections.abc import Iterable

import pandas as pd

ATR_WINDOW_DAYS = 15  # 14-day ATR needs 15 bars (true range uses the previous close)


def spans_demerger(
    dates: pd.Index, ex_dates: Iterable[dt.date], window_bars: int
) -> pd.Series:
    """True for date D when some ex-date E has window-start(D) < E <= D, where window-start is
    the bar `window_bars` rows before D (or the first bar)."""
    dates = pd.Index(dates)
    out = pd.Series(False, index=dates)
    if len(dates) == 0:
        return out
    starts = pd.Series(dates, index=dates).shift(window_bars)
    starts = starts.fillna(dates[0] - dt.timedelta(days=1))
    for ex in ex_dates:
        out |= (starts < ex) & (dates >= ex)
    return out
