"""T27: the IC study — holdout guard, no look-ahead, a factor that should predict, labels."""

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Security, TradingCalendar
from equitypulse.research.ic import (
    HoldoutError,
    Universe,
    guard_window,
    holdout_start,
    run_ic_study,
    summarise,
)
from equitypulse.research.report import write_report

N_STOCKS = 40
FIRST, LAST_DATA = dt.date(2019, 1, 1), dt.date(2021, 6, 30)
WINDOW_END = dt.date(2021, 3, 31)


def _days(a: dt.date, b: dt.date) -> list[dt.date]:
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)
            if (a + dt.timedelta(days=i)).weekday() < 5]


def _seed(session: Session, *, reverse_after: dt.date | None = None) -> frozenset[str]:
    """Stock i drifts at a rate that grows with i and persists, so past winners keep winning."""
    days = _days(FIRST, LAST_DATA)
    session.execute(insert(TradingCalendar), [
        {"date": d, "is_trading_day": True, "note": None, "source": "T"} for d in days
    ])
    rng = np.random.default_rng(1)
    isins, rows = [], []
    for i in range(N_STOCKS):
        isin = f"INE{i:03d}A01011"
        isins.append(isin)
        session.add(Security(isin=isin, symbol=f"S{i}", name=f"S{i}", status="ACTIVE",
                             source="T", as_of=FIRST, sector="ALL",
                             listing_date=dt.date(2010, 1, 1)))
        drift = (i - N_STOCKS / 2) * 0.00015
        price = 200.0
        for d in days:
            step = -drift if reverse_after and d > reverse_after else drift
            price *= 1 + step + rng.normal(0, 0.004)
            rows.append({"isin": isin, "date": d, "open": price, "high": price * 1.002,
                         "low": price * 0.998, "close": price, "prev_close": None,
                         "volume": 1_000_000, "traded_value": 2e8, "delivery_qty": None,
                         "delivery_pct": 0.4, "series": "EQ", "source": "T"})
    session.flush()
    session.execute(insert(PriceDaily), rows)
    session.flush()
    return frozenset(isins)


def _universe(members: frozenset[str], biased: bool = True) -> Universe:
    return Universe(name="TEST", members=lambda _d: members, survivorship_biased=biased)


def test_holdout_starts_on_the_first_of_the_month_twelve_months_back() -> None:
    assert holdout_start(dt.date(2026, 11, 24)) == dt.date(2025, 11, 1)
    assert holdout_start(dt.date(2026, 1, 15)) == dt.date(2025, 1, 1)


def test_a_window_past_the_holdout_is_refused_before_any_data_is_read(db_session: Session) -> None:
    with pytest.raises(HoldoutError, match="locked holdout"):
        run_ic_study(db_session, _universe(frozenset()), dt.date(2019, 1, 1),
                     dt.date(2025, 11, 2), holdout=dt.date(2025, 11, 1))
    guard_window(dt.date(2025, 11, 1), dt.date(2025, 11, 1))  # the boundary itself is allowed


def test_persistent_trends_give_momentum_a_strong_positive_ic(db_session: Session) -> None:
    members = _seed(db_session)
    study = run_ic_study(db_session, _universe(members), dt.date(2019, 1, 1), WINDOW_END,
                         holdout=WINDOW_END)
    mom = {s.horizon: s for s in summarise(study) if s.factor == "pct_momentum"}
    assert mom["1m"].mean_ic > 0.3 and mom["1m"].pct_positive > 0.9 and mom["1m"].passes
    assert mom["3m"].evaluation.startswith("non-overlapping")
    assert mom["1m"].mean_spread > 0
    assert study.records["date"].min() > dt.date(2019, 9, 1)  # needs 200 bars of history


def test_prices_after_the_window_end_change_nothing(db_session: Session, tmp_path: Path) -> None:
    members = _seed(db_session, reverse_after=WINDOW_END)  # trends flip after the window
    flipped = run_ic_study(db_session, _universe(members), dt.date(2019, 1, 1), WINDOW_END,
                           holdout=WINDOW_END)
    assert flipped.records["date"].max() <= WINDOW_END
    mom = {s.horizon: s for s in summarise(flipped) if s.factor == "pct_momentum"}
    assert mom["1m"].mean_ic > 0.3  # the reversal after the end is invisible


def test_every_section_of_a_biased_report_carries_the_label(
    db_session: Session, tmp_path: Path
) -> None:
    members = _seed(db_session)
    study = run_ic_study(db_session, _universe(members), dt.date(2019, 1, 1), WINDOW_END,
                         holdout=WINDOW_END)
    path = tmp_path / "report.md"
    write_report(study, path, generated=dt.date(2026, 11, 24))
    text = path.read_text(encoding="utf-8")
    headings = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert headings and all("SURVIVORSHIP-BIASED" in h for h in headings)
    assert "Verdict" in text and "TEST [SURVIVORSHIP-BIASED]" in text
