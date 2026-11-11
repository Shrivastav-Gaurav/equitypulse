"""Nightly job: orchestration, skip on non-trading days, idempotency."""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import DataAudit, TradingCalendar
from equitypulse.jobs.nightly import NightlyResult, run_nightly

DATE = dt.date(2026, 11, 11)  # Tuesday — trading day
NON_TRADING = dt.date(2026, 11, 14)  # Saturday — non-trading
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)


def _seed_calendar(session: Session) -> None:
    """Insert a minimal calendar so NseCalendar.from_session() doesn't raise."""
    for delta in range(-5, 10):
        d = DATE + dt.timedelta(days=delta)
        # Weekends are non-trading.
        is_trading = d.weekday() < 5
        session.add(TradingCalendar(date=d, is_trading_day=is_trading, source="TEST"))
    session.flush()


def _passthrough_job(*_args, **_kwargs):
    return None


def _make_stubs(*, bhavcopy_rows=5, master_rows=10):
    """Return stub callables for each job."""

    def stub_master(session, **kw):
        from equitypulse.master import LoadResult
        return LoadResult(inserted=master_rows)

    def stub_bhavcopy(session, date, **kw):
        from equitypulse.prices import LoadPricesResult
        return LoadPricesResult(inserted=bhavcopy_rows)

    def stub_surveillance(session, list_type, **kw):
        from equitypulse.surveillance_load import LoadSurveillanceResult
        return LoadSurveillanceResult(list_type=list_type, inserted=1)

    return dict(
        fetch_master=stub_master,
        fetch_bhavcopy=stub_bhavcopy,
        fetch_surveillance=stub_surveillance,
    )


# ── nightly runs ─────────────────────────────────────────────────────────────


def test_nightly_returns_result(db_session: Session) -> None:
    _seed_calendar(db_session)
    stubs = _make_stubs()
    result = run_nightly(db_session, date=DATE, now=_NOW, **stubs)
    db_session.commit()
    assert isinstance(result, NightlyResult)
    assert result.skipped is False


def test_nightly_skips_non_trading_day(db_session: Session) -> None:
    _seed_calendar(db_session)
    stubs = _make_stubs()
    result = run_nightly(db_session, date=NON_TRADING, now=_NOW, **stubs)
    db_session.commit()
    assert result.skipped is True
    # No audit rows should be written for a skipped day.
    count = db_session.query(DataAudit).count()
    assert count == 0


def test_nightly_writes_audit_rows(db_session: Session) -> None:
    _seed_calendar(db_session)
    stubs = _make_stubs()
    run_nightly(db_session, date=DATE, now=_NOW, **stubs)
    db_session.commit()
    # Expect at least STARTED + final OK rows per job.
    rows = db_session.scalars(select(DataAudit)).all()
    assert len(rows) >= 2
    statuses = {r.status for r in rows}
    assert "STARTED" in statuses
    assert "OK" in statuses or "DEGRADED" in statuses


def test_nightly_idempotent_rerun_writes_zero(db_session: Session) -> None:
    _seed_calendar(db_session)
    stubs = _make_stubs()
    run_nightly(db_session, date=DATE, now=_NOW, **stubs)
    db_session.commit()
    # Second run: stubs return 0 new rows since everything already loaded.
    zero_stubs = _make_stubs(bhavcopy_rows=0, master_rows=0)
    result2 = run_nightly(db_session, date=DATE, now=_NOW, **zero_stubs)
    db_session.commit()
    assert result2.skipped is False
    assert result2.bhavcopy_rows == 0


def test_nightly_records_bhavcopy_rows(db_session: Session) -> None:
    _seed_calendar(db_session)
    stubs = _make_stubs(bhavcopy_rows=42)
    result = run_nightly(db_session, date=DATE, now=_NOW, **stubs)
    db_session.commit()
    assert result.bhavcopy_rows == 42
