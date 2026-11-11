"""Smallcase import: xlsx parsing, DB upsert, name->ISIN mapping."""
# ruff: noqa: E501

import datetime as dt
import io

import openpyxl
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from equitypulse.db.models import (
    NameMap,
    Security,
    SmallcaseEvent,
    SmallcaseIndex,
    SmallcasePeriod,
)
from equitypulse.imports.smallcase import (
    SmallcaseImportResult,
    import_smallcase,
    map_names,
)

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)


def _make_workbook() -> bytes:
    """Build a minimal xlsx with the same sheet structure as the real file."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # type: ignore[arg-type]

    # Constituent_Periods
    ws_cp = wb.create_sheet("Constituent_Periods")
    ws_cp.append(["Smallcase", "Period start", "Period end", "Stock", "Weight", "Cash or ETF"])
    ws_cp.append(["SC1", dt.datetime(2024, 1, 1), dt.datetime(2024, 3, 31), "Reliance Industries Limited", 0.5, False])
    ws_cp.append(["SC1", dt.datetime(2024, 1, 1), dt.datetime(2024, 3, 31), "Infosys Limited", 0.3, False])
    ws_cp.append(["SC1", dt.datetime(2024, 1, 1), dt.datetime(2024, 3, 31), "CASH", 0.2, True])

    # Rebalance_Events
    ws_re = wb.create_sheet("Rebalance_Events")
    ws_re.append(["Smallcase", "Rebalance date", "Stock", "Action", "Weight", "Cash or ETF"])
    ws_re.append(["SC1", dt.datetime(2024, 1, 1), "Reliance Industries Limited", "INITIAL", 0.5, False])
    ws_re.append(["SC1", dt.datetime(2024, 1, 1), "Infosys Limited", "INITIAL", 0.3, False])
    ws_re.append(["SC1", dt.datetime(2024, 1, 1), "CASH", "INITIAL", 0.2, True])
    ws_re.append(["SC1", dt.datetime(2024, 4, 1), "Infosys Limited", "REMOVE", 0.0, False])
    ws_re.append(["SC1", dt.datetime(2024, 4, 1), "HDFC Bank Limited", "ADD", 0.3, False])

    # Index_Values
    ws_iv = wb.create_sheet("Index_Values")
    ws_iv.append(["Smallcase", "Date", "Index value", "Rebalance on this date"])
    ws_iv.append(["SC1", dt.datetime(2024, 1, 1), 100.0, True])
    ws_iv.append(["SC1", dt.datetime(2024, 1, 2), 101.5, None])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _seed_securities(session: Session) -> None:
    for isin, sym, name in [
        ("INE002A01018", "RELIANCE", "Reliance Industries Limited"),
        ("INE009A01021", "INFY", "Infosys Limited"),
        ("INE040A01034", "HDFCBANK", "HDFC Bank Limited"),
    ]:
        if session.get(Security, isin) is None:
            session.add(Security(isin=isin, symbol=sym, name=name, status="ACTIVE", source="TEST", as_of=DATE))
    session.flush()


WORKBOOK_BYTES = _make_workbook()


def _load_workbook(path: str) -> bytes:
    return WORKBOOK_BYTES


# ── import ────────────────────────────────────────────────────────────────────


def test_import_inserts_periods(db_session: Session) -> None:
    _seed_securities(db_session)
    result = import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    db_session.commit()
    assert isinstance(result, SmallcaseImportResult)
    # 3 rows including CASH row
    count = db_session.scalar(select(func.count()).select_from(SmallcasePeriod))
    assert count == 3


def test_import_inserts_events(db_session: Session) -> None:
    _seed_securities(db_session)
    import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    db_session.commit()
    # 5 event rows including CASH row
    count = db_session.scalar(select(func.count()).select_from(SmallcaseEvent))
    assert count == 5


def test_import_inserts_index_values(db_session: Session) -> None:
    _seed_securities(db_session)
    import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    db_session.commit()
    count = db_session.scalar(select(func.count()).select_from(SmallcaseIndex))
    assert count == 2


def test_import_is_idempotent(db_session: Session) -> None:
    _seed_securities(db_session)
    r1 = import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    db_session.commit()
    r2 = import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    db_session.commit()
    assert r2.periods_inserted == 0
    assert r2.events_inserted == 0
    assert r2.index_rows_inserted == 0
    assert r1.periods_inserted == 3


# ── name mapping ──────────────────────────────────────────────────────────────


def test_map_names_exact_match(db_session: Session) -> None:
    _seed_securities(db_session)
    names = ["Reliance Industries Limited", "Infosys Limited"]
    map_names(db_session, names)
    db_session.commit()
    nm = db_session.get(NameMap, "Reliance Industries Limited")
    assert nm is not None
    assert nm.isin == "INE002A01018"
    assert nm.method == "exact"
    assert nm.confidence == pytest.approx(1.0)


def test_map_names_fuzzy_match(db_session: Session) -> None:
    _seed_securities(db_session)
    # Slight typo: should fuzzy-match to Infosys Limited
    names = ["Infosys Ltd"]
    map_names(db_session, names, fuzzy_threshold=0.7)
    db_session.commit()
    nm = db_session.get(NameMap, "Infosys Ltd")
    assert nm is not None
    assert nm.isin == "INE009A01021"
    assert nm.method == "fuzzy"
    assert nm.confidence is not None and nm.confidence > 0.7


def test_map_names_low_confidence_left_unmapped(db_session: Session) -> None:
    _seed_securities(db_session)
    # Complete gibberish should not map.
    names = ["XYZZY Corp Ltd Pvt XYZ 999"]
    map_names(db_session, names, fuzzy_threshold=0.9)
    db_session.commit()
    nm = db_session.get(NameMap, "XYZZY Corp Ltd Pvt XYZ 999")
    # Row exists but no ISIN.
    assert nm is not None
    assert nm.isin is None


def test_map_names_idempotent(db_session: Session) -> None:
    _seed_securities(db_session)
    names = ["Reliance Industries Limited"]
    map_names(db_session, names)
    db_session.commit()
    map_names(db_session, names)
    db_session.commit()
    count = db_session.scalar(
        select(func.count()).select_from(NameMap).where(NameMap.raw_name == "Reliance Industries Limited")
    )
    assert count == 1
