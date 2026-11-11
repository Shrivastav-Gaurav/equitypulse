"""Trading calendar tests. NSE data comes from recorded fixtures (2024-2026); overrides from the
real config/calendar_overrides.yaml or a temp file. Every special session asserted here was
checked against NSE's published bhavcopy for that date."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from equitypulse.calendar import (
    IST,
    CalendarRangeError,
    NseCalendar,
    OverrideError,
    load_overrides,
    refresh_year,
    today_ist,
)
from equitypulse.db.models import Snapshot, TradingCalendar
from equitypulse.sources.nse import parse_trading_holidays
from tests.conftest import REPO, load_json_fixture

OVERRIDES = REPO / "config" / "calendar_overrides.yaml"
D = dt.date


def fixture_fetch(year: int) -> dict:
    return load_json_fixture(f"nse/holiday_master_trading_{year}.json")


def load_years(session: Session, years: list[int], overrides: Path = OVERRIDES) -> NseCalendar:
    for year in years:
        refresh_year(session, year, fetch=fixture_fetch, overrides_path=overrides)
    session.commit()
    return NseCalendar.from_session(session)


@pytest.fixture
def cal(db_session: Session) -> NseCalendar:
    return load_years(db_session, [2024, 2025, 2026])


def write_overrides(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "overrides.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- today -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_now", "expected"),
    [
        (dt.datetime(2026, 11, 10, 18, 29, tzinfo=dt.UTC), D(2026, 11, 10)),  # 23:59 IST
        (dt.datetime(2026, 11, 10, 18, 30, tzinfo=dt.UTC), D(2026, 11, 11)),  # 00:00 IST
        (dt.datetime(2026, 11, 11, 3, 0, tzinfo=dt.UTC), D(2026, 11, 11)),
    ],
)
def test_today_is_the_india_date(utc_now: dt.datetime, expected: dt.date) -> None:
    assert today_ist(utc_now) == expected


def test_today_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        today_ist(dt.datetime(2026, 11, 11, 3, 0))


def test_today_default_uses_real_clock() -> None:
    assert today_ist() == dt.datetime.now(IST).date()


# --- known days --------------------------------------------------------------------------


def test_republic_day_is_closed(cal: NseCalendar) -> None:
    assert not cal.is_trading_day(D(2026, 1, 26))  # Monday
    assert not cal.is_trading_day(D(2024, 1, 26))  # Friday


def test_diwali_closures(cal: NseCalendar) -> None:
    assert not cal.is_trading_day(D(2025, 10, 22))  # Balipratipada
    assert not cal.is_trading_day(D(2026, 11, 10))  # Diwali-Balipratipada


def test_muhurat_sessions_are_trading_days(cal: NseCalendar) -> None:
    assert cal.is_trading_day(D(2025, 10, 21))  # NSE lists it as a holiday; override wins
    assert cal.is_trading_day(D(2026, 11, 8))  # a Sunday


def test_long_weekend_good_friday_2026(cal: NseCalendar) -> None:
    for d in (D(2026, 4, 3), D(2026, 4, 4), D(2026, 4, 5)):  # Fri holiday, Sat, Sun
        assert not cal.is_trading_day(d)
    assert cal.prev_trading_day(D(2026, 4, 6)) == D(2026, 4, 2)
    assert cal.next_trading_day(D(2026, 4, 2)) == D(2026, 4, 6)


def test_budget_day_special_weekend_sessions_from_override_file(cal: NseCalendar) -> None:
    assert cal.is_trading_day(D(2025, 2, 1))  # Saturday
    assert cal.is_trading_day(D(2026, 2, 1))  # Sunday
    assert not cal.is_trading_day(D(2025, 2, 8))  # an ordinary Saturday


def test_special_live_trading_listed_by_nse_is_a_trading_day(cal: NseCalendar) -> None:
    assert cal.is_trading_day(D(2024, 3, 2))


def test_sessions_on_weekends_and_holidays_match_nse_bhavcopy_archive(cal: NseCalendar) -> None:
    # Verified 2026-11-11 by probing every weekend day and NSE-listed holiday from 2024-01-01
    # to 2026-11-10 (342 dates) for a bhavcopy carrying that date. Exactly these traded.
    verified = {
        D(2024, 1, 20),
        D(2024, 3, 2),
        D(2024, 5, 18),
        D(2025, 2, 1),
        D(2025, 10, 21),
        D(2026, 2, 1),
        D(2026, 11, 8),
    }
    nse_holidays = {
        e.date
        for y in (2024, 2025, 2026)
        for e in parse_trading_holidays(fixture_fetch(y), y)
        if not e.is_session
    }
    start, end = D(2024, 1, 1), D(2026, 11, 10)
    days = [start + dt.timedelta(n) for n in range((end - start).days + 1)]
    unusual = {d for d in days if (d.weekday() >= 5 or d in nse_holidays) and cal.is_trading_day(d)}
    assert unusual == verified


def test_unlisted_2024_saturday_sessions_from_override_file(cal: NseCalendar) -> None:
    assert cal.is_trading_day(D(2024, 1, 20))
    assert cal.is_trading_day(D(2024, 5, 18))


def test_ordinary_weekday_and_weekend(cal: NseCalendar) -> None:
    assert cal.is_trading_day(D(2026, 11, 11))  # Wednesday
    assert not cal.is_trading_day(D(2026, 11, 14))  # Saturday


# --- arithmetic --------------------------------------------------------------------------


def test_add_trading_days_across_republic_day(cal: NseCalendar) -> None:
    assert cal.add_trading_days(D(2026, 1, 23), 1) == D(2026, 1, 27)  # skips Sat, Sun, Mon
    assert cal.add_trading_days(D(2026, 1, 27), -1) == D(2026, 1, 23)
    assert cal.add_trading_days(D(2026, 1, 23), 5) == D(2026, 2, 1)  # Sun 1 Feb: Budget session
    assert cal.add_trading_days(D(2026, 1, 23), 6) == D(2026, 2, 2)
    assert cal.add_trading_days(D(2026, 1, 23), 0) == D(2026, 1, 23)


def test_add_trading_days_from_a_non_trading_day(cal: NseCalendar) -> None:
    assert cal.add_trading_days(D(2026, 1, 26), 1) == D(2026, 1, 27)
    assert cal.add_trading_days(D(2026, 1, 26), -1) == D(2026, 1, 23)
    with pytest.raises(ValueError, match="not a trading day"):
        cal.add_trading_days(D(2026, 1, 26), 0)


def test_trading_days_between(cal: NseCalendar) -> None:
    assert cal.trading_days_between(D(2026, 1, 23), D(2026, 1, 27)) == 1
    assert cal.trading_days_between(D(2026, 1, 27), D(2026, 1, 23)) == -1
    assert cal.trading_days_between(D(2026, 1, 23), D(2026, 1, 23)) == 0


def test_between_is_the_inverse_of_add(cal: NseCalendar) -> None:
    start = D(2025, 10, 15)
    for n in range(-30, 31):
        assert cal.trading_days_between(start, cal.add_trading_days(start, n)) == n


def test_trading_days_in_2025_count(cal: NseCalendar) -> None:
    # 261 weekdays - 14 weekday holidays + Budget Saturday + Muhurat (on a listed holiday) = 249
    n = cal.trading_days_between(D(2024, 12, 31), D(2025, 12, 31))
    assert n == 249


# --- range errors ------------------------------------------------------------------------


def test_dates_outside_loaded_range_raise(cal: NseCalendar) -> None:
    with pytest.raises(CalendarRangeError, match="2023-12-29"):
        cal.is_trading_day(D(2023, 12, 29))
    with pytest.raises(CalendarRangeError, match="2027"):
        cal.is_trading_day(D(2027, 1, 4))


def test_stepping_past_the_loaded_range_raises(cal: NseCalendar) -> None:
    with pytest.raises(CalendarRangeError):
        cal.next_trading_day(D(2026, 12, 31))
    with pytest.raises(CalendarRangeError):
        cal.prev_trading_day(D(2024, 1, 1))
    with pytest.raises(CalendarRangeError):
        cal.add_trading_days(D(2026, 12, 20), 10)


def test_empty_calendar_raises(db_session: Session) -> None:
    with pytest.raises(CalendarRangeError, match="empty"):
        NseCalendar.from_session(db_session)


def test_calendar_with_a_missing_year_raises(db_session: Session) -> None:
    with pytest.raises(CalendarRangeError, match="gap"):
        load_years(db_session, [2024, 2026])


# --- refresh / storage -------------------------------------------------------------------


def test_refresh_writes_every_day_of_the_year(db_session: Session) -> None:
    result = refresh_year(db_session, 2024, fetch=fixture_fetch, overrides_path=OVERRIDES)
    db_session.commit()
    assert result.inserted == 366  # leap year
    assert db_session.scalar(select(func.count()).select_from(TradingCalendar)) == 366
    row = db_session.get(TradingCalendar, D(2024, 1, 26))
    assert row is not None
    assert (row.is_trading_day, row.note, row.source) == (
        False,
        "Republic Day",
        "NSE_HOLIDAY_MASTER",
    )


def test_refresh_is_idempotent(db_session: Session) -> None:
    refresh_year(db_session, 2026, fetch=fixture_fetch, overrides_path=OVERRIDES)
    db_session.commit()
    again = refresh_year(db_session, 2026, fetch=fixture_fetch, overrides_path=OVERRIDES)
    db_session.commit()
    assert (again.inserted, again.updated) == (0, 0)
    assert db_session.scalar(select(func.count()).select_from(TradingCalendar)) == 365


def test_refresh_stores_raw_payload_once_per_day(db_session: Session) -> None:
    now = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)
    for _ in range(2):
        refresh_year(db_session, 2026, fetch=fixture_fetch, overrides_path=OVERRIDES, now=now)
    db_session.commit()
    snaps = db_session.scalars(select(Snapshot)).all()
    assert len(snaps) == 1
    snap = snaps[0]
    assert (snap.source, snap.endpoint, snap.entity) == (
        "NSE",
        "holiday-master?type=trading",
        "2026",
    )
    assert snap.as_of == D(2026, 11, 11)
    assert '"Republic Day"' in snap.payload_json


def test_refresh_fails_loudly_and_writes_nothing_on_bad_payload(db_session: Session) -> None:
    def wrong_year(_year: int) -> dict:
        return load_json_fixture("nse/holiday_master_trading_2025.json")

    with pytest.raises(ValueError, match="2026"):
        refresh_year(db_session, 2026, fetch=wrong_year, overrides_path=OVERRIDES)
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(TradingCalendar)) == 0


def test_override_change_updates_existing_rows(db_session: Session, tmp_path: Path) -> None:
    refresh_year(db_session, 2026, fetch=fixture_fetch, overrides_path=OVERRIDES)
    db_session.commit()
    closure = write_overrides(
        tmp_path,
        "overrides:\n"
        "  - date: 2026-11-12\n    trading: false\n    note: Unexpected closure (test)\n",
    )
    result = refresh_year(db_session, 2026, fetch=fixture_fetch, overrides_path=closure)
    db_session.commit()
    assert result.updated >= 1
    row = db_session.get(TradingCalendar, D(2026, 11, 12))
    assert row is not None
    assert (row.is_trading_day, row.source) == (False, "OVERRIDE")
    # The Muhurat override is gone from this file, so 8 Nov falls back to NSE (a Sunday).
    assert not NseCalendar.from_session(db_session).is_trading_day(D(2026, 11, 8))


def test_override_wins_over_nse_holiday(db_session: Session, tmp_path: Path) -> None:
    path = write_overrides(
        tmp_path,
        "overrides:\n  - date: 2026-01-26\n    trading: true\n    note: Hypothetical session\n",
    )
    cal = load_years(db_session, [2026], overrides=path)
    assert cal.is_trading_day(D(2026, 1, 26))


# --- override file -----------------------------------------------------------------------


def test_repo_override_file_is_valid() -> None:
    overrides = {o.date: o for o in load_overrides(OVERRIDES)}
    for d in (D(2025, 2, 1), D(2025, 10, 21), D(2026, 2, 1), D(2026, 11, 8)):
        assert overrides[d].trading, d
        assert overrides[d].note


def test_missing_override_file_fails_loudly(tmp_path: Path) -> None:
    # Silently running without it would drop Budget-day and Muhurat sessions.
    with pytest.raises(OverrideError, match="not found"):
        load_overrides(tmp_path / "absent.yaml")


def test_empty_override_file_means_no_overrides(tmp_path: Path) -> None:
    assert load_overrides(write_overrides(tmp_path, "overrides: []\n")) == []


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "overrides:\n  - {date: 2026-01-26, trading: true, note: a}\n"
            "  - {date: 2026-01-26, trading: false, note: b}\n",
            "duplicate",
        ),
        ("overrides:\n  - {date: 2026-01-26, trading: true}\n", "note"),
        ("overrides:\n  - {date: 2026-01-26, trading: true, note: a, extra: 1}\n", "extra"),
        ("overrides:\n  - {date: not-a-date, trading: true, note: a}\n", "date"),
        ("overrides: 5\n", "list"),
    ],
    ids=["duplicate", "no-note", "unknown-key", "bad-date", "not-a-list"],
)
def test_invalid_override_file_raises(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(OverrideError, match=match):
        load_overrides(write_overrides(tmp_path, body))
