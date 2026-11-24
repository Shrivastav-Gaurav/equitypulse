"""Bulk bhavcopy backfill over a date range.

Differs from the nightly job on purpose: it never falls back to Tapetide (a bulk run would
burn the daily call budget on the first outage), records one audit run per date so every gap
is explained in `data_audit`, resumes by skipping dates already loaded from NSE, and stops
when NSE looks like it is blocking us.

It also cross-checks the trading calendar against NSE's files: a calendar trading day with no
price file (HTTP 404), or a calendar weekday holiday with one, is a `calendar_check` row in
`data_audit` (DEGRADED). A missing file is never counted as NSE blocking us.
"""

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.audit import AuditContext, begin_audit, finish_audit, new_run_id
from equitypulse.calendar import NseCalendar
from equitypulse.db.models import PriceDaily
from equitypulse.isin_links import ChainUniverse, detect_links
from equitypulse.prices import SOURCE_NSE, load_bhavcopy
from equitypulse.sources.nse import NseFetchError, NseFileMissing
from equitypulse.sources.nse_legacy import price_file_exists

JOB = "prices"
CHECK_JOB = "calendar_check"


class BackfillResult(BaseModel):
    loaded: int = 0
    rows_written: int = 0
    already_loaded: int = 0
    skipped_non_trading: int = 0
    failed: list[dt.date] = Field(default_factory=list)
    blocked: bool = False
    isin_links_added: int = 0
    calendar_mismatches: list[dt.date] = Field(default_factory=list)


def run_backfill(
    session: Session,
    calendar: NseCalendar,
    from_date: dt.date,
    to_date: dt.date,
    *,
    universe: set[str] | ChainUniverse | None = None,
    load: Callable[..., Any] = load_bhavcopy,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    max_consecutive_fetch_failures: int = 3,
    refresh: bool = False,
    check_calendar: bool = True,
    file_exists: Callable[[dt.date], bool] = price_file_exists,
    progress: Callable[[str], None] = lambda _msg: None,
) -> BackfillResult:
    """Load every trading day in [from_date, to_date], committing after each date.

    `refresh=True` re-downloads dates already loaded (the load upserts, so unchanged rows
    write nothing); use it after the loader starts keeping more rows per file.
    """
    result = BackfillResult()
    consecutive_fetch_failures = 0
    date = from_date
    while date <= to_date:
        if not calendar.is_trading_day(date):
            result.skipped_non_trading += 1
            if check_calendar and date.weekday() < 5 and file_exists(date):
                _mismatch(session, result, date, "price file exists on a calendar holiday")
                progress(f"{date} CALENDAR MISMATCH: price file on a holiday")
            date += dt.timedelta(days=1)
            continue

        loaded = session.scalar(
            select(PriceDaily.isin)
            .where(PriceDaily.date == date, PriceDaily.source == SOURCE_NSE)
            .limit(1)
        )
        if loaded is not None and not refresh:
            result.already_loaded += 1
            date += dt.timedelta(days=1)
            continue

        try:
            with AuditContext(session, job=JOB, source=SOURCE_NSE, as_of=date) as ctx:
                loaded_result = load(session, date, universe=universe, allow_fallback=False)
                ctx.rows_written = loaded_result.inserted + loaded_result.updated
                ctx.rows_in = ctx.rows_written
            session.commit()
            result.loaded += 1
            result.rows_written += loaded_result.inserted + loaded_result.updated
            consecutive_fetch_failures = 0
            progress(f"{date} +{loaded_result.inserted} rows")
        except Exception as exc:
            session.rollback()
            run_id = new_run_id()
            begin_audit(session, run_id=run_id, job=JOB, source=SOURCE_NSE, as_of=date)
            finish_audit(
                session, run_id=run_id, job=JOB, source=SOURCE_NSE, status="FAILED",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            session.commit()
            result.failed.append(date)
            progress(f"{date} FAILED {type(exc).__name__}: {str(exc)[:120]}")
            if isinstance(exc, NseFileMissing):
                _mismatch(session, result, date, f"no file on a calendar trading day: {exc}")
            elif isinstance(exc, NseFetchError):
                consecutive_fetch_failures += 1
                if consecutive_fetch_failures >= max_consecutive_fetch_failures:
                    result.blocked = True
                    break
            else:
                consecutive_fetch_failures = 0

        sleep(delay_seconds)
        date += dt.timedelta(days=1)

    result.isin_links_added = detect_links(session)
    session.commit()
    return result


def _mismatch(session: Session, result: BackfillResult, date: dt.date, why: str) -> None:
    run_id = new_run_id()
    begin_audit(session, run_id=run_id, job=CHECK_JOB, source=SOURCE_NSE, as_of=date)
    finish_audit(
        session, run_id=run_id, job=CHECK_JOB, source=SOURCE_NSE, status="DEGRADED",
        anomaly_count=1, error=why[:500],
    )
    session.commit()
    result.calendar_mismatches.append(date)
