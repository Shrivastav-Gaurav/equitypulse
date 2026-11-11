"""NSE trading calendar: which dates are trading days, and trading-day arithmetic.

Staleness and horizons count NSE trading days, not calendar days, so every date question goes
through `NseCalendar`. It answers only for dates inside the loaded range and raises
`CalendarRangeError` outside it: guessing (e.g. "weekdays are trading days") would quietly
mis-count around unlisted holidays.

Precedence when building a year: override file > NSE holiday list > weekday rule.
"Today" is always the Asia/Kolkata date: use `today_ist()`, never `date.today()`.
"""

import bisect
import datetime as dt
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import TradingCalendar
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse import NseHoliday, fetch_holiday_master, parse_trading_holidays

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_OVERRIDES_PATH = Path("config/calendar_overrides.yaml")
SOURCE_NSE = "NSE_HOLIDAY_MASTER"
SOURCE_OVERRIDE = "OVERRIDE"


def today_ist(now: dt.datetime | None = None) -> dt.date:
    """The current Indian calendar date. `now` (timezone-aware) is for tests and replays."""
    if now is None:
        now = dt.datetime.now(dt.UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(IST).date()


# --- overrides ---------------------------------------------------------------------------


class OverrideError(ValueError):
    """The override file is missing or invalid."""


class CalendarOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: dt.date
    trading: bool
    note: str = Field(min_length=1)


class _OverrideFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: list[CalendarOverride] = []


def load_overrides(path: Path = DEFAULT_OVERRIDES_PATH) -> list[CalendarOverride]:
    if not path.is_file():
        raise OverrideError(f"calendar override file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        parsed = _OverrideFile.model_validate(raw)
    except yaml.YAMLError as exc:
        raise OverrideError(f"{path}: not valid YAML ({exc})") from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise OverrideError(f"{path}: {problems}") from None
    seen: set[dt.date] = set()
    for item in parsed.overrides:
        if item.date in seen:
            raise OverrideError(f"{path}: duplicate override for {item.date}")
        seen.add(item.date)
    return parsed.overrides


# --- building and storing a year ---------------------------------------------------------


class CalendarDay(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    is_trading_day: bool
    note: str | None
    source: str


class RefreshResult(BaseModel):
    year: int
    inserted: int
    updated: int


def build_year(
    year: int, holidays: list[NseHoliday], overrides: list[CalendarOverride]
) -> list[CalendarDay]:
    nse = {h.date: h for h in holidays}
    manual = {o.date: o for o in overrides if o.date.year == year}
    first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    days = []
    for offset in range((last - first).days + 1):
        d = first + dt.timedelta(days=offset)
        if d in manual:
            o = manual[d]
            days.append(
                CalendarDay(date=d, is_trading_day=o.trading, note=o.note, source=SOURCE_OVERRIDE)
            )
        elif d in nse:
            h = nse[d]
            days.append(
                CalendarDay(
                    date=d, is_trading_day=h.is_session, note=h.description, source=SOURCE_NSE
                )
            )
        else:
            weekday = d.weekday() < 5
            days.append(
                CalendarDay(
                    date=d,
                    is_trading_day=weekday,
                    note=None if weekday else "Weekend",
                    source=SOURCE_NSE,
                )
            )
    return days


def refresh_year(
    session: Session,
    year: int,
    *,
    fetch: Callable[[int], dict[str, Any]] = fetch_holiday_master,
    overrides_path: Path = DEFAULT_OVERRIDES_PATH,
    now: dt.datetime | None = None,
) -> RefreshResult:
    """Fetch NSE's list for `year`, keep the raw payload, and upsert all of the year's days.

    Everything is validated before anything is written. Idempotent: an unchanged rerun
    inserts and updates nothing. Does not commit; the caller owns the transaction.
    """
    payload = fetch(year)
    holidays = parse_trading_holidays(payload, year)
    overrides = load_overrides(overrides_path)
    days = build_year(year, holidays, overrides)

    store_snapshot(
        session,
        source="NSE",
        endpoint="holiday-master?type=trading",
        entity=str(year),
        as_of=today_ist(now),
        payload=payload,
        fetched_at=now,
    )

    existing = {
        row.date: row
        for row in session.scalars(
            select(TradingCalendar).where(
                TradingCalendar.date >= dt.date(year, 1, 1),
                TradingCalendar.date <= dt.date(year, 12, 31),
            )
        )
    }
    inserted = updated = 0
    for day in days:
        row = existing.get(day.date)
        if row is None:
            session.add(TradingCalendar(**day.model_dump()))
            inserted += 1
        elif (row.is_trading_day, row.note, row.source) != (
            day.is_trading_day,
            day.note,
            day.source,
        ):
            row.is_trading_day, row.note, row.source = day.is_trading_day, day.note, day.source
            updated += 1
    session.flush()
    return RefreshResult(year=year, inserted=inserted, updated=updated)


# --- queries -----------------------------------------------------------------------------


class CalendarRangeError(LookupError):
    """A date (or a step from it) falls outside the loaded trading calendar."""


class NseCalendar:
    """Trading-day lookups over a contiguous, fully loaded range of dates."""

    def __init__(self, days: Mapping[dt.date, bool]) -> None:
        if not days:
            raise CalendarRangeError("trading calendar is empty; refresh it for the years needed")
        self._days = dict(days)
        self.start, self.end = min(self._days), max(self._days)
        expected = (self.end - self.start).days + 1
        if len(self._days) != expected:
            missing = sorted(
                d
                for d in (self.start + dt.timedelta(n) for n in range(expected))
                if d not in self._days
            )
            raise CalendarRangeError(
                f"trading calendar has a gap: {len(missing)} missing dates from {missing[0]} "
                f"to {missing[-1]}; refresh those years"
            )
        self._trading = sorted(d for d, is_trading in self._days.items() if is_trading)

    @classmethod
    def from_session(cls, session: Session) -> "NseCalendar":
        rows = session.execute(select(TradingCalendar.date, TradingCalendar.is_trading_day))
        return cls({d: bool(t) for d, t in rows})

    def _check(self, d: dt.date) -> None:
        if not self.start <= d <= self.end:
            raise CalendarRangeError(
                f"{d.isoformat()} is outside the loaded trading calendar "
                f"({self.start}..{self.end}); refresh the calendar for {d.year} first"
            )

    def _at(self, index: int, d: dt.date, n: int) -> dt.date:
        if not 0 <= index < len(self._trading):
            raise CalendarRangeError(
                f"{n:+d} trading days from {d} is outside the loaded trading calendar "
                f"({self.start}..{self.end})"
            )
        return self._trading[index]

    def is_trading_day(self, d: dt.date) -> bool:
        self._check(d)
        return self._days[d]

    def next_trading_day(self, d: dt.date) -> dt.date:
        """First trading day strictly after `d`."""
        return self.add_trading_days(d, 1)

    def prev_trading_day(self, d: dt.date) -> dt.date:
        """Last trading day strictly before `d`."""
        return self.add_trading_days(d, -1)

    def add_trading_days(self, d: dt.date, n: int) -> dt.date:
        """The n-th trading day after `d` (before it if n < 0). `d` itself need not be a
        trading day; n == 0 requires that it is, since there is no honest answer otherwise."""
        self._check(d)
        if n == 0:
            if not self._days[d]:
                raise ValueError(f"{d} is not a trading day; use n=1 or n=-1 to step to one")
            return d
        if n > 0:
            return self._at(bisect.bisect_right(self._trading, d) + n - 1, d, n)
        return self._at(bisect.bisect_left(self._trading, d) + n, d, n)

    def trading_days_between(self, a: dt.date, b: dt.date) -> int:
        """Trading days in (a, b] — so `trading_days_between(a, add_trading_days(a, n)) == n`.
        Negative when b is before a."""
        self._check(a)
        self._check(b)
        if b < a:
            return -self.trading_days_between(b, a)
        return bisect.bisect_right(self._trading, b) - bisect.bisect_right(self._trading, a)
