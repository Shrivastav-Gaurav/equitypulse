"""Bulk backfill: audit per date, resume, stop-on-block, no Tapetide fallback."""

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import NseCalendar
from equitypulse.db.models import DataAudit, PriceDaily, Security
from equitypulse.jobs.backfill import run_backfill
from equitypulse.prices import LoadPricesResult
from equitypulse.sources.nse import NseFetchError, NseFormatError

MON = dt.date(2026, 11, 2)
# Mon-Fri trading, weekend closed.
_DAYS = {MON + dt.timedelta(days=i): (MON + dt.timedelta(days=i)).weekday() < 5 for i in range(14)}
CAL = NseCalendar(_DAYS)
ISIN = "INE002A01018"


def _ok_loader(calls: list):
    def load(session, date, **kw):
        calls.append((date, kw))
        session.add(PriceDaily(isin=ISIN, date=date, open=1, high=1, low=1, close=1,
                               volume=1, series="EQ", source="NSE_BHAVCOPY"))
        return LoadPricesResult(inserted=1)

    return load


def _seed_security(session: Session) -> None:
    session.add(Security(isin=ISIN, symbol="RELIANCE", name="Reliance", status="ACTIVE",
                         source="TEST", as_of=MON))
    session.commit()


def test_loads_only_trading_days_and_audits_each(db_session: Session) -> None:
    _seed_security(db_session)
    calls: list = []
    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=6), load=_ok_loader(calls))
    assert result.loaded == 5
    assert result.skipped_non_trading == 2
    ok = db_session.scalars(select(DataAudit).where(DataAudit.status == "OK")).all()
    assert sorted(a.as_of for a in ok) == [MON + dt.timedelta(days=i) for i in range(5)]


def test_never_allows_tapetide_fallback(db_session: Session) -> None:
    _seed_security(db_session)
    calls: list = []
    run_backfill(db_session, CAL, MON, MON, load=_ok_loader(calls), universe={ISIN})
    assert calls[0][1] == {"universe": {ISIN}, "allow_fallback": False}


def test_resumes_by_skipping_dates_already_loaded(db_session: Session) -> None:
    _seed_security(db_session)
    first: list = []
    run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=1), load=_ok_loader(first))
    second: list = []
    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=2), load=_ok_loader(second))
    assert [c[0] for c in second] == [MON + dt.timedelta(days=2)]
    assert result.already_loaded == 2


def test_failed_date_is_recorded_and_run_continues(db_session: Session) -> None:
    _seed_security(db_session)
    bad = MON + dt.timedelta(days=1)

    def load(session, date, **kw):
        if date == bad:
            raise NseFormatError("garbled file")
        return _ok_loader([])(session, date, **kw)

    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=2), load=load)
    assert result.failed == [bad]
    assert result.loaded == 2
    failed = db_session.scalar(select(DataAudit).where(DataAudit.status == "FAILED"))
    assert failed.as_of == bad
    assert "garbled file" in failed.error
    # The failed date wrote no price rows, and the loaded dates are still there.
    dates = set(db_session.scalars(select(PriceDaily.date)))
    assert bad not in dates and len(dates) == 2


def test_stops_after_consecutive_nse_fetch_failures(db_session: Session) -> None:
    attempted: list = []

    def load(session, date, **kw):
        attempted.append(date)
        raise NseFetchError("HTTP 403")

    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=4), load=load)
    assert result.blocked is True
    assert len(attempted) == 3
    assert len(result.failed) == 3


def test_a_success_resets_the_block_counter(db_session: Session) -> None:
    _seed_security(db_session)
    fail_on = {MON + dt.timedelta(days=i) for i in (0, 1, 3, 4)}

    def load(session, date, **kw):
        if date in fail_on:
            raise NseFetchError("HTTP 503")
        return _ok_loader([])(session, date, **kw)

    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=4), load=load)
    assert result.blocked is False
    assert len(result.failed) == 4


def test_delay_is_applied_between_dates(db_session: Session) -> None:
    _seed_security(db_session)
    slept: list = []
    run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=1), load=_ok_loader([]),
                 delay_seconds=1.5, sleep=slept.append)
    assert slept == [1.5, 1.5]


def test_refresh_reloads_dates_already_loaded(db_session: Session) -> None:
    _seed_security(db_session)
    run_backfill(db_session, CAL, MON, MON, load=_ok_loader([]))
    calls: list = []

    def reload(session, date, **kw):
        calls.append(date)
        return LoadPricesResult(updated=0)

    result = run_backfill(db_session, CAL, MON, MON, load=reload, refresh=True)
    assert calls == [MON]
    assert result.already_loaded == 0 and result.loaded == 1


# ── calendar cross-check ──────────────────────────────────────────────────────

from equitypulse.sources.nse import NseFileMissing  # noqa: E402

HOLIDAY = MON + dt.timedelta(days=2)  # a Wednesday the calendar closes
_HOL_DAYS = dict(_DAYS) | {HOLIDAY: False}
CAL_HOL = NseCalendar(_HOL_DAYS)


def _checks(session: Session) -> list[DataAudit]:
    return session.scalars(
        select(DataAudit).where(DataAudit.job == "calendar_check", DataAudit.status != "STARTED")
    ).all()


def test_price_file_on_a_calendar_holiday_is_a_mismatch(db_session: Session) -> None:
    _seed_security(db_session)
    probed: list = []

    def exists(d: dt.date) -> bool:
        probed.append(d)
        return True

    result = run_backfill(db_session, CAL_HOL, MON, MON + dt.timedelta(days=6),
                          load=_ok_loader([]), file_exists=exists)
    assert probed == [HOLIDAY]  # weekends are not probed
    assert result.calendar_mismatches == [HOLIDAY]
    row = _checks(db_session)[0]
    assert (row.status, row.as_of, row.anomaly_count) == ("DEGRADED", HOLIDAY, 1)
    assert "holiday" in row.error


def test_holiday_without_a_file_is_fine(db_session: Session) -> None:
    _seed_security(db_session)
    result = run_backfill(db_session, CAL_HOL, MON, MON + dt.timedelta(days=4),
                          load=_ok_loader([]), file_exists=lambda _d: False)
    assert result.calendar_mismatches == [] and _checks(db_session) == []


def test_missing_file_on_a_trading_day_is_a_mismatch_not_a_block(db_session: Session) -> None:
    def load(session, date, **kw):
        raise NseFileMissing(f"NSE legacy bhavcopy {date} returned HTTP 404")

    result = run_backfill(db_session, CAL, MON, MON + dt.timedelta(days=4), load=load,
                          file_exists=lambda _d: False)
    assert result.blocked is False  # 404s are not NSE refusing us
    assert len(result.calendar_mismatches) == 5
    assert len(_checks(db_session)) == 5
    assert "trading day" in _checks(db_session)[0].error


def test_cross_check_can_be_switched_off(db_session: Session) -> None:
    _seed_security(db_session)
    result = run_backfill(db_session, CAL_HOL, MON, MON + dt.timedelta(days=4),
                          load=_ok_loader([]), check_calendar=False,
                          file_exists=lambda _d: pytest.fail("must not probe"))
    assert result.calendar_mismatches == []
