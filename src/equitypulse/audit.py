"""Data audit helpers: STARTED + final row pattern for every job.

Usage::

    with AuditContext(session, run_id=run_id, job="prices",
                      source="NSE_BHAVCOPY", as_of=date):
        result = load_bhavcopy(session, date)
        ctx.rows_written = result.inserted

Or the lower-level::

    begin_audit(session, run_id=..., job=..., source=..., as_of=...)
    # ... do work ...
    finish_audit(session, run_id=..., job=..., source=...,
                 status="OK", rows_written=n)
"""

import datetime as dt
import uuid
from types import TracebackType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import DataAudit


def _utcnow(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else dt.datetime.now(tz=dt.UTC)


def new_run_id() -> str:
    return str(uuid.uuid4())


def begin_audit(
    session: Session,
    *,
    run_id: str,
    job: str,
    source: str,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
) -> DataAudit:
    """Write a STARTED row. Does not commit."""
    row = DataAudit(
        run_id=run_id,
        job=job,
        source=source,
        as_of=as_of,
        started_at=_utcnow(now),
        finished_at=None,
        status="STARTED",
    )
    session.add(row)
    session.flush()
    return row


def finish_audit(
    session: Session,
    *,
    run_id: str,
    job: str,
    source: str,
    status: str,
    rows_in: int = 0,
    rows_written: int = 0,
    stale_count: int = 0,
    anomaly_count: int = 0,
    error: str | None = None,
    now: dt.datetime | None = None,
) -> DataAudit:
    """Write a final OK/DEGRADED/FAILED row. Does not commit.

    The row inherits `started_at` and `as_of` from the run's STARTED row, so the final row
    alone says which market date it was for and how long the run took.
    """
    started = session.scalar(
        select(DataAudit).where(
            DataAudit.run_id == run_id,
            DataAudit.job == job,
            DataAudit.source == source,
            DataAudit.status == "STARTED",
        )
    )
    row = DataAudit(
        run_id=run_id,
        job=job,
        source=source,
        as_of=started.as_of if started is not None else None,
        started_at=started.started_at if started is not None else _utcnow(now),
        finished_at=_utcnow(now),
        status=status,
        rows_in=rows_in,
        rows_written=rows_written,
        stale_count=stale_count,
        anomaly_count=anomaly_count,
        error=error,
    )
    session.add(row)
    session.flush()
    return row


class AuditContext:
    """Context manager that writes STARTED on enter, final row on exit.

    Sets status=OK on clean exit, FAILED with the exception message on error.
    Sets status=DEGRADED when `ctx.degraded = True` is set before exit.
    Callers may update `ctx.rows_written` etc. during the block.
    """

    def __init__(
        self,
        session: Session,
        *,
        run_id: str | None = None,
        job: str,
        source: str,
        as_of: dt.date | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self.session = session
        self.run_id = run_id or new_run_id()
        self.job = job
        self.source = source
        self.as_of = as_of
        self._now = now
        self.rows_in = 0
        self.rows_written = 0
        self.stale_count = 0
        self.anomaly_count = 0
        self.degraded = False

    def __enter__(self) -> "AuditContext":
        begin_audit(
            self.session,
            run_id=self.run_id,
            job=self.job,
            source=self.source,
            as_of=self.as_of,
            now=self._now,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            finish_audit(
                self.session,
                run_id=self.run_id,
                job=self.job,
                source=self.source,
                status="FAILED",
                error=str(exc_val),
                now=self._now,
            )
            # Don't suppress the exception.
            return False

        status = "DEGRADED" if self.degraded else "OK"
        finish_audit(
            self.session,
            run_id=self.run_id,
            job=self.job,
            source=self.source,
            status=status,
            rows_in=self.rows_in,
            rows_written=self.rows_written,
            stale_count=self.stale_count,
            anomaly_count=self.anomaly_count,
            now=self._now,
        )
        return False


def status_summary(session: Session) -> list[dict[str, Any]]:
    """Return freshness info per source for `ep status`.

    Returns rows sorted by source, with the most recent OK/DEGRADED run per source.
    A STARTED row with no final row is included as a 'crashed' run.
    """
    from sqlalchemy import func, select

    from equitypulse.db.models import ApiUsage

    # Latest successful (OK/DEGRADED) run per source.
    sub = (
        select(
            DataAudit.source,
            func.max(DataAudit.finished_at).label("last_ok"),
            func.sum(DataAudit.rows_written).label("total_rows"),
        )
        .where(DataAudit.status.in_(["OK", "DEGRADED"]))
        .group_by(DataAudit.source)
        .subquery()
    )
    rows = session.execute(select(sub)).all()

    # Today's Tapetide call count.
    from zoneinfo import ZoneInfo

    today_ist = dt.datetime.now(tz=ZoneInfo("Asia/Kolkata")).date()
    usage = session.get(ApiUsage, (today_ist, "TAPETIDE"))
    tapetide_calls = usage.calls if usage else 0

    summary = [
        {"source": r.source, "last_ok": r.last_ok, "total_rows": r.total_rows}
        for r in rows
    ]
    summary.sort(key=lambda x: x["source"])
    summary.append({"source": "TAPETIDE_CALLS_TODAY", "calls": tapetide_calls})
    return summary
