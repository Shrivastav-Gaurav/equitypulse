"""Price coverage for a set of securities: the measure behind gate check "< 0.1% missing rows,
each gap explained".

For each security the expected days are the NSE trading days from the later of `start` and its
NSE listing date, to `end`. Prices are read across its whole ISIN chain. A missing day is
explained when a committed GapNote (config/manual_data.yaml) covers it. A security whose first
row comes after its expected start is reported as a late start, and those days count as missing.
Securities with no equity rows at all (e.g. REITs, which NSE prices in another segment) are
listed separately and left out of the percentage.
"""

import datetime as dt
from collections.abc import Iterable, Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Security, TradingCalendar
from equitypulse.isin_links import chain_of
from equitypulse.manual_data import GapNote


class StockGap(BaseModel):
    symbol: str
    isin: str
    expected_start: dt.date
    first_row: dt.date
    missing: int
    explained: int
    first_missing: dt.date
    last_missing: dt.date


class CoverageReport(BaseModel):
    start: dt.date
    end: dt.date
    securities: int
    expected: int = 0
    missing: int = 0
    explained: int = 0
    no_rows: list[str] = []
    late_starts: list[StockGap] = []
    gaps: list[StockGap] = []

    @property
    def unexplained(self) -> int:
        return self.missing - self.explained

    @property
    def missing_pct(self) -> float:
        return self.missing / self.expected if self.expected else 0.0

    @property
    def unexplained_pct(self) -> float:
        return self.unexplained / self.expected if self.expected else 0.0


def price_coverage(
    session: Session,
    isins: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    gap_notes: Sequence[GapNote] = (),
) -> CoverageReport:
    isins = sorted(set(isins))
    days = sorted(session.scalars(select(TradingCalendar.date).where(
        TradingCalendar.is_trading_day.is_(True), TradingCalendar.date.between(start, end)
    )))
    report = CoverageReport(start=start, end=end, securities=len(isins))
    for isin in isins:
        sec = session.get(Security, isin)
        chain = chain_of(session, isin)
        have = set(session.scalars(select(PriceDaily.date).where(
            PriceDaily.isin.in_(chain), PriceDaily.date.between(start, end)
        )))
        symbol = sec.symbol if sec else isin
        if not have:
            report.no_rows.append(symbol)
            continue
        expected_start = max(start, sec.listing_date) if sec and sec.listing_date else start
        expected = [d for d in days if d >= expected_start]
        missing = [d for d in expected if d not in have]
        explained = [d for d in missing if any(g.covers(i, d) for g in gap_notes for i in chain)]
        report.expected += len(expected)
        report.missing += len(missing)
        report.explained += len(explained)
        if missing:
            gap = StockGap(
                symbol=symbol, isin=isin, expected_start=expected_start, first_row=min(have),
                missing=len(missing), explained=len(explained), first_missing=missing[0],
                last_missing=missing[-1],
            )
            report.gaps.append(gap)
            if min(have) > expected_start:
                report.late_starts.append(gap)
    report.gaps.sort(key=lambda g: -g.missing)
    return report
