"""Bulk backfill: audit per date, resume, stop-on-block, no Tapetide fallback."""

import datetime as dt

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
