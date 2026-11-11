"""Import smallcase validation dataset xlsx into DB and map raw names to ISINs.

Expected sheets (identical structure in every version of the workbook):
  Constituent_Periods — one row per (smallcase, period, stock)
  Rebalance_Events    — one row per (smallcase, date, stock, action)
  Index_Values        — one row per (smallcase, date)

Name mapping strategy:
  1. Exact match against securities.name (case-insensitive).
  2. Normalised fuzzy match via rapidfuzz (strip Ltd/Limited/Inc/Corp suffixes,
     lowercase, collapse whitespace).
  3. Names below `fuzzy_threshold` → NameMap row with isin=NULL.

The caller writes low-confidence names to data/private/name_map_review.csv.
"""

import csv
import datetime as dt
import io
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

import openpyxl
from pydantic import BaseModel
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import NameMap, Security, SmallcaseEvent, SmallcaseIndex, SmallcasePeriod

_DEFAULT_WORKBOOK_PATH = "data/private/EquityPulse_smallcase_validation_dataset.xlsx"
_DEFAULT_REVIEW_PATH = Path("data/private/name_map_review.csv")

# Columns expected in each sheet.
_CP_COLS = ["Smallcase", "Period start", "Period end", "Stock", "Weight", "Cash or ETF"]
_RE_COLS = ["Smallcase", "Rebalance date", "Stock", "Action", "Weight", "Cash or ETF"]
_IV_COLS = ["Smallcase", "Date", "Index value", "Rebalance on this date"]


class SmallcaseImportResult(BaseModel):
    periods_inserted: int = 0
    periods_already_present: int = 0
    events_inserted: int = 0
    events_already_present: int = 0
    index_rows_inserted: int = 0
    index_rows_already_present: int = 0


class MapNamesResult(BaseModel):
    exact: int = 0
    fuzzy: int = 0
    unmapped: int = 0


def import_smallcase(
    session: Session,
    *,
    workbook_path: str = _DEFAULT_WORKBOOK_PATH,
    load_workbook: Callable[[str], bytes] | None = None,
    now: dt.datetime | None = None,
) -> SmallcaseImportResult:
    """Parse xlsx and upsert all three smallcase tables.

    Does not commit; the caller owns the transaction.
    Injectable `load_workbook` enables tests without touching the real file.
    """
    if load_workbook is not None:
        raw = load_workbook(workbook_path)
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    else:
        wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)

    result = SmallcaseImportResult()
    _import_periods(session, wb, result)
    _import_events(session, wb, result)
    _import_index(session, wb, result)
    return result


def _import_periods(session: Session, wb: openpyxl.Workbook, result: SmallcaseImportResult) -> None:
    ws = wb["Constituent_Periods"]
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        smallcase, period_start, period_end, raw_name, weight, _cash = row
        if not smallcase or not raw_name:
            continue
        period_start = _to_date(period_start)
        period_end = _to_date(period_end)

        existing = session.scalar(
            select(SmallcasePeriod).where(
                SmallcasePeriod.smallcase == str(smallcase),
                SmallcasePeriod.period_start == period_start,
                SmallcasePeriod.raw_name == str(raw_name),
            )
        )
        if existing is not None:
            result.periods_already_present += 1
            continue

        session.add(
            SmallcasePeriod(
                smallcase=str(smallcase),
                period_start=period_start,
                period_end=period_end,
                raw_name=str(raw_name),
                isin=None,
                weight=float(weight) if weight is not None else None,
            )
        )
        result.periods_inserted += 1

    session.flush()


def _import_events(session: Session, wb: openpyxl.Workbook, result: SmallcaseImportResult) -> None:
    ws = wb["Rebalance_Events"]
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        smallcase, rebalance_date, raw_name, action, weight, _cash = row
        if not smallcase or not raw_name or not action:
            continue
        date = _to_date(rebalance_date)
        action = str(action).upper()

        existing = session.scalar(
            select(SmallcaseEvent).where(
                SmallcaseEvent.smallcase == str(smallcase),
                SmallcaseEvent.date == date,
                SmallcaseEvent.raw_name == str(raw_name),
                SmallcaseEvent.action == action,
            )
        )
        if existing is not None:
            result.events_already_present += 1
            continue

        session.add(
            SmallcaseEvent(
                smallcase=str(smallcase),
                date=date,
                raw_name=str(raw_name),
                isin=None,
                action=action,
                weight=float(weight) if weight is not None else None,
            )
        )
        result.events_inserted += 1

    session.flush()


def _import_index(session: Session, wb: openpyxl.Workbook, result: SmallcaseImportResult) -> None:
    ws = wb["Index_Values"]
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        smallcase, date_val, index_value, _rebalance = row
        if not smallcase or index_value is None:
            continue
        date = _to_date(date_val)

        existing = session.get(SmallcaseIndex, (str(smallcase), date))
        if existing is not None:
            result.index_rows_already_present += 1
            continue

        session.add(
            SmallcaseIndex(
                smallcase=str(smallcase),
                date=date,
                index_value=float(index_value),
            )
        )
        result.index_rows_inserted += 1

    session.flush()


def _to_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise ValueError(f"Cannot convert {value!r} to date")


# ── name mapping ─────────────────────────────────────────────────────────────

_STRIP_SUFFIX = re.compile(
    r"\b(limited|ltd|pvt|private|inc|corp|corporation|co|company|llp|llc)\b\.?",
    re.IGNORECASE,
)


def _normalise(name: str) -> str:
    """Lowercase, strip legal suffixes, collapse whitespace, strip accents."""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode()
    name = _STRIP_SUFFIX.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def map_names(
    session: Session,
    raw_names: list[str],
    *,
    fuzzy_threshold: float = 0.80,
    review_path: Path | None = None,
) -> MapNamesResult:
    """Map raw stock names to ISINs and write to name_map.

    Writes low-confidence entries to `review_path` (default:
    data/private/name_map_review.csv) for manual review.
    Does not commit; the caller owns the transaction.
    """
    if review_path is None:
        review_path = _DEFAULT_REVIEW_PATH

    # Build a lookup from securities: name → isin (exact, case-insensitive).
    securities = session.execute(select(Security.name, Security.isin)).all()
    sec_by_name: dict[str, str] = {s.name.strip().lower(): s.isin for s in securities if s.name}
    sec_names: list[str] = list(sec_by_name.keys())
    sec_norm: dict[str, str] = {_normalise(n): sec_by_name[n] for n in sec_names}

    result = MapNamesResult()
    unmapped: list[str] = []

    for raw in raw_names:
        # Skip if already in name_map.
        existing = session.get(NameMap, raw)
        if existing is not None:
            continue

        # 1. Exact match (case-insensitive).
        lower = raw.strip().lower()
        if lower in sec_by_name:
            session.add(
                NameMap(raw_name=raw, isin=sec_by_name[lower], method="exact", confidence=1.0)
            )
            result.exact += 1
            continue

        # 2. Fuzzy match on normalised names.
        norm = _normalise(raw)
        match = process.extractOne(norm, list(sec_norm.keys()), scorer=fuzz.token_sort_ratio)
        if match is not None:
            matched_norm, score, _ = match
            confidence = score / 100.0
            if confidence >= fuzzy_threshold:
                isin = sec_norm[matched_norm]
                session.add(NameMap(raw_name=raw, isin=isin, method="fuzzy", confidence=confidence))
                result.fuzzy += 1
                continue

        # 3. No match — store as unmapped.
        session.add(NameMap(raw_name=raw, isin=None, method=None, confidence=None))
        result.unmapped += 1
        unmapped.append(raw)

    session.flush()

    # Write review file for manual mapping (append-safe, creates if missing).
    if unmapped:
        review_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not review_path.exists() or review_path.stat().st_size == 0
        with review_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(["raw_name", "isin", "notes"])
            for name in unmapped:
                writer.writerow([name, "", ""])

    return result
