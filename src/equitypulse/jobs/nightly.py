"""Nightly ingest orchestrator.

Order: calendar check → equity master → bhavcopy → surveillance (ASM/GSM/ESM).
Each step is wrapped in AuditContext so failures are recorded without crashing
subsequent steps.

Injectable stubs (`fetch_master`, `fetch_bhavcopy`, `fetch_surveillance`) allow
tests to run without touching any network or external DB.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from equitypulse.audit import AuditContext, new_run_id
from equitypulse.calendar import NseCalendar, today_ist
from equitypulse.master import load_equity_master
from equitypulse.prices import load_bhavcopy
from equitypulse.surveillance_load import load_surveillance


class NightlyResult(BaseModel):
    date: dt.date
    skipped: bool = False
    master_rows: int = 0
    bhavcopy_rows: int = 0
    surveillance_rows: int = 0
    degraded: bool = False


def run_nightly(
    session: Session,
    *,
    date: dt.date | None = None,
    now: dt.datetime | None = None,
    fetch_master: Callable[..., Any] | None = None,
    fetch_bhavcopy: Callable[..., Any] | None = None,
    fetch_surveillance: Callable[..., Any] | None = None,
) -> NightlyResult:
    """Run all nightly ingest steps for `date`.

    Returns immediately with `skipped=True` for non-trading days.
    Does not commit; the caller owns the transaction.
    """
    if date is None:
        date = today_ist(now)

    # Calendar check: skip non-trading days.
    cal = NseCalendar.from_session(session)
    if not cal.is_trading_day(date):
        return NightlyResult(date=date, skipped=True)

    result = NightlyResult(date=date)
    run_id = new_run_id()

    # ── equity master ────────────────────────────────────────────────────────
    with AuditContext(
        session, run_id=run_id, job="equity_master", source="NSE_EQUITY", as_of=date, now=now
    ) as ctx:
        master_fn = fetch_master if fetch_master is not None else load_equity_master
        master_result = master_fn(session, as_of=date, now=now)
        inserted = getattr(master_result, "inserted", 0)
        result.master_rows = inserted
        ctx.rows_written = inserted

    # ── bhavcopy ─────────────────────────────────────────────────────────────
    with AuditContext(
        session, run_id=run_id, job="prices", source="NSE_BHAVCOPY", as_of=date, now=now
    ) as ctx:
        bhav_fn = fetch_bhavcopy if fetch_bhavcopy is not None else load_bhavcopy
        bhav_result = bhav_fn(session, date, now=now)
        inserted = getattr(bhav_result, "inserted", 0)
        degraded = getattr(bhav_result, "degraded", False)
        result.bhavcopy_rows = inserted
        ctx.rows_written = inserted
        ctx.degraded = degraded
        if degraded:
            result.degraded = True

    # ── surveillance ─────────────────────────────────────────────────────────
    surv_fn = fetch_surveillance if fetch_surveillance is not None else load_surveillance
    for list_type in ("ASM", "GSM", "ESM"):
        with AuditContext(
            session, run_id=run_id, job=f"surveillance_{list_type.lower()}",
            source=f"NSE_{list_type}", as_of=date, now=now,
        ) as ctx:
            surv_result = surv_fn(session, list_type, as_of=date, now=now)
            inserted = getattr(surv_result, "inserted", 0)
            result.surveillance_rows += inserted
            ctx.rows_written = inserted

    session.flush()
    return result
