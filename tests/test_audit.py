"""Data audit helpers: STARTED + final row pattern, ep status."""
# ruff: noqa: E501

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.audit import AuditContext, begin_audit, finish_audit
from equitypulse.db.models import DataAudit

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)
_RUN_ID = "test-run-00000000"


def _count_rows(session: Session) -> int:
    return session.scalar(select(DataAudit).with_only_columns(  # type: ignore[call-overload]
        *[]
    ).count()) or session.query(DataAudit).count()


# ── begin/finish ─────────────────────────────────────────────────────────────


def test_begin_audit_writes_started_row(db_session: Session) -> None:
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    db_session.commit()
    row = db_session.scalar(
        select(DataAudit).where(
            DataAudit.run_id == _RUN_ID, DataAudit.status == "STARTED"
        )
    )
    assert row is not None
    assert row.finished_at is None


def test_finish_audit_writes_ok_row(db_session: Session) -> None:
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    db_session.commit()
    finish_audit(
        db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY",
        status="OK", rows_written=100, now=_NOW,
    )
    db_session.commit()
    row = db_session.scalar(
        select(DataAudit).where(
            DataAudit.run_id == _RUN_ID, DataAudit.status == "OK"
        )
    )
    assert row is not None
    assert row.finished_at is not None
    assert row.rows_written == 100


def test_finish_audit_accepts_degraded(db_session: Session) -> None:
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    db_session.commit()
    finish_audit(
        db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY",
        status="DEGRADED", rows_written=50, now=_NOW,
    )
    db_session.commit()
    row = db_session.scalar(
        select(DataAudit).where(DataAudit.status == "DEGRADED")
    )
    assert row is not None


def test_finish_audit_accepts_failed_with_error(db_session: Session) -> None:
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    db_session.commit()
    finish_audit(
        db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY",
        status="FAILED", error="Connection refused", now=_NOW,
    )
    db_session.commit()
    row = db_session.scalar(
        select(DataAudit).where(DataAudit.status == "FAILED")
    )
    assert row is not None
    assert row.error == "Connection refused"


def test_audit_context_manager_ok(db_session: Session) -> None:
    with AuditContext(db_session, run_id=_RUN_ID, job="master", source="NSE_EQUITY", as_of=DATE, now=_NOW):
        pass
    db_session.commit()
    ok_row = db_session.scalar(select(DataAudit).where(DataAudit.status == "OK"))
    assert ok_row is not None


def test_audit_context_manager_failed_on_exception(db_session: Session) -> None:
    with pytest.raises(RuntimeError):
        with AuditContext(db_session, run_id=_RUN_ID, job="master", source="NSE_EQUITY", as_of=DATE, now=_NOW):
            raise RuntimeError("boom")
    db_session.commit()
    fail_row = db_session.scalar(select(DataAudit).where(DataAudit.status == "FAILED"))
    assert fail_row is not None
    assert "boom" in (fail_row.error or "")


def test_audit_rows_are_append_only(db_session: Session) -> None:
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    db_session.commit()
    row = db_session.scalar(select(DataAudit).where(DataAudit.status == "STARTED"))
    assert row is not None
    # Directly modifying an audit row should raise.
    with pytest.raises(Exception):  # noqa: B017
        row.status = "OK"
        db_session.flush()


# ── ep status ─────────────────────────────────────────────────────────────────


def test_status_summary_returns_list(db_session: Session) -> None:
    """status_summary() returns a list (empty or not) without crashing."""
    from equitypulse.audit import status_summary

    rows = status_summary(db_session)
    assert isinstance(rows, list)
    # Last entry is always the Tapetide calls today sentinel.
    assert rows[-1]["source"] == "TAPETIDE_CALLS_TODAY"


def test_status_summary_includes_finished_jobs(db_session: Session) -> None:
    from equitypulse.audit import status_summary

    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=_NOW)
    finish_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY",
                 status="OK", rows_written=200, now=_NOW)
    db_session.commit()
    rows = status_summary(db_session)
    sources = [r["source"] for r in rows]
    assert "NSE_BHAVCOPY" in sources


def test_final_row_inherits_as_of_and_started_at(db_session: Session) -> None:
    started_at = dt.datetime(2026, 11, 11, 5, 0, tzinfo=dt.UTC)
    begin_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", as_of=DATE, now=started_at)
    finish_audit(db_session, run_id=_RUN_ID, job="prices", source="NSE_BHAVCOPY", status="OK", now=_NOW)
    final = db_session.scalar(select(DataAudit).where(DataAudit.status == "OK"))
    assert final.as_of == DATE
    assert final.started_at.replace(tzinfo=None) == started_at.replace(tzinfo=None)
    assert final.finished_at.replace(tzinfo=None) == _NOW.replace(tzinfo=None)
