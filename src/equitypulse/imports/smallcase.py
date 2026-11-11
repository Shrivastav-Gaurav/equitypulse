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
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process
from sqlalchemy import select, update
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
    # Distinct non-cash/ETF stock names: the population name mapping is measured on.
    stock_names: list[str] = Field(default_factory=list)


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
    names: set[str] = set()
    _import_periods(session, wb, result, names)
    _import_events(session, wb, result, names)
    _import_index(session, wb, result)
    result.stock_names = sorted(names)
    return result


def _import_periods(
    session: Session, wb: openpyxl.Workbook, result: SmallcaseImportResult, names: set[str]
) -> None:
    ws = wb["Constituent_Periods"]
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        smallcase, period_start, period_end, raw_name, weight, is_cash = row
        if not smallcase or not raw_name:
            continue
        if not is_cash:
            names.add(str(raw_name))
        period_start = _to_date(period_start)
        period_end = _to_date(period_end) if period_end is not None else None

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


def _import_events(
    session: Session, wb: openpyxl.Workbook, result: SmallcaseImportResult, names: set[str]
) -> None:
    ws = wb["Rebalance_Events"]
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        smallcase, rebalance_date, raw_name, action, weight, is_cash = row
        if not smallcase or not raw_name or not action:
            continue
        if not is_cash:
            names.add(str(raw_name))
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

# Legal-form words carry no identity; "&" and "and" are the same word.
_LEGAL_WORDS = frozenset(
    {
        "limited", "ltd", "pvt", "private", "inc", "corp", "corporation",
        "co", "company", "llp", "plc",
    }
)

# A fuzzy match is only auto-accepted at or above this score AND when it beats the runner-up
# by MIN_MARGIN; everything else goes to the review file. Callers can raise the bar, not lower it.
MIN_AUTO_CONFIDENCE = 0.92
MIN_MARGIN = 0.05
NORMALISED_EQUAL_CONFIDENCE = 0.99


def _normalise(name: str) -> str:
    """Casefold, drop accents/punctuation/legal suffixes, and read '&' as 'and'."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = name.replace("&", " and ").replace(".", "")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(t for t in name.split() if t not in _LEGAL_WORDS)


def _group(pairs: list[tuple[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, isin in pairs:
        out.setdefault(key, set()).add(isin)
    return out


def _apply_manual_overrides(session: Session, review_path: Path) -> None:
    """Rows of the review file where the user filled in `isin` become verified manual mappings.

    This is the only route for renamed and merged companies, whose old names are in no source.
    """
    if not review_path.exists():
        return
    with review_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("raw_name") or "").strip()
            isin = (row.get("isin") or "").strip()
            if not raw or not isin or session.get(Security, isin) is None:
                continue
            nm = session.get(NameMap, raw)
            if nm is None:
                session.add(
                    NameMap(raw_name=raw, isin=isin, method="manual", confidence=1.0, verified=True)
                )
            else:
                nm.isin, nm.method, nm.confidence, nm.verified = isin, "manual", 1.0, True
    session.flush()


def link_isins(session: Session) -> None:
    """Copy name_map ISINs onto smallcase_periods / smallcase_events rows."""
    mapped = select(NameMap.raw_name).where(NameMap.isin.is_not(None))
    for model in (SmallcasePeriod, SmallcaseEvent):
        isin_for = select(NameMap.isin).where(NameMap.raw_name == model.raw_name).scalar_subquery()
        session.execute(
            update(model)
            .where(model.raw_name.in_(mapped))
            .values(isin=isin_for)
            .execution_options(synchronize_session=False)
        )
    session.flush()


def map_names(
    session: Session,
    raw_names: list[str],
    *,
    fuzzy_threshold: float = MIN_AUTO_CONFIDENCE,
    review_path: Path | None = None,
) -> MapNamesResult:
    """Map raw stock names to ISINs, write name_map, link ISINs, rewrite the review file.

    Order: manual overrides from the review file, exact name, normalised-equal name, then fuzzy
    with a score floor and a margin over the runner-up. Ambiguous or low-confidence names are
    never auto-accepted: they stay `isin=NULL` and appear in the review file with the best
    candidate. Previously unmapped names are retried (the master may have grown); verified or
    already-mapped names are left alone. Does not commit.
    """
    review_path = review_path or _DEFAULT_REVIEW_PATH
    threshold = max(fuzzy_threshold, MIN_AUTO_CONFIDENCE)
    _apply_manual_overrides(session, review_path)

    securities = session.execute(select(Security.isin, Security.name)).all()
    by_name = _group([(n.strip().casefold(), i) for i, n in securities if n])
    by_norm = _group([(_normalise(n), i) for i, n in securities if n])
    names_by_isin = {i: n for i, n in securities}
    norm_keys = list(by_norm)

    result = MapNamesResult()
    candidates: dict[str, tuple[str, float]] = {}

    for raw in dict.fromkeys(raw_names):
        existing = session.get(NameMap, raw)
        if existing is not None and (existing.isin is not None or existing.verified):
            continue

        method: str | None = None
        isin: str | None = None
        confidence: float | None = None
        exact = by_name.get(raw.strip().casefold(), set())
        norm = _normalise(raw)
        same_norm = by_norm.get(norm, set())
        if len(exact) == 1:
            method, isin, confidence = "exact", next(iter(exact)), 1.0
        elif not exact and norm and len(same_norm) == 1:
            method, isin, confidence = "fuzzy", next(iter(same_norm)), NORMALISED_EQUAL_CONFIDENCE
        elif norm and norm_keys:
            top = process.extract(norm, norm_keys, scorer=fuzz.token_sort_ratio, limit=3)
            scored = [(score / 100.0, key) for key, score, _ in top]
            best, best_key = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            best_isins = by_norm[best_key]
            candidates[raw] = (sorted(best_isins)[0], best)
            if best >= threshold and best - runner_up >= MIN_MARGIN and len(best_isins) == 1:
                method, isin, confidence = "fuzzy", next(iter(best_isins)), best

        if existing is None:
            session.add(NameMap(raw_name=raw, isin=isin, method=method, confidence=confidence))
        else:
            existing.isin, existing.method, existing.confidence = isin, method, confidence
        if method == "exact":
            result.exact += 1
        elif method == "fuzzy":
            result.fuzzy += 1
        else:
            result.unmapped += 1

    session.flush()
    link_isins(session)

    unmapped = session.scalars(
        select(NameMap.raw_name).where(NameMap.isin.is_(None)).order_by(NameMap.raw_name)
    ).all()
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["raw_name", "isin", "candidate_isin", "candidate_name", "score", "notes"])
        for raw in unmapped:
            cand_isin, score = candidates.get(raw, ("", 0.0))
            writer.writerow(
                [
                    raw, "", cand_isin, names_by_isin.get(cand_isin, ""),
                    f"{score:.2f}" if score else "", "",
                ]
            )
    return result
