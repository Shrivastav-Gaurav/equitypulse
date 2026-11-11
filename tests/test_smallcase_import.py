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


# ── review fixes ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _private_review_path(tmp_path, monkeypatch):
    """Tests must never write to the real data/private/name_map_review.csv."""
    monkeypatch.setattr(
        "equitypulse.imports.smallcase._DEFAULT_REVIEW_PATH", tmp_path / "name_map_review.csv"
    )


def _add_sec(session: Session, isin: str, name: str) -> None:
    session.add(Security(isin=isin, symbol=isin[-6:], name=name, status="ACTIVE", source="TEST", as_of=DATE))
    session.flush()


def test_stock_names_exclude_cash_rows(db_session: Session) -> None:
    result = import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    assert result.stock_names == ["HDFC Bank Limited", "Infosys Limited", "Reliance Industries Limited"]


def test_import_handles_open_ended_period(db_session: Session) -> None:
    wb = openpyxl.load_workbook(io.BytesIO(WORKBOOK_BYTES))
    wb["Constituent_Periods"].append(["SC1", dt.datetime(2024, 4, 1), None, "Infosys Limited", 0.4, False])
    buf = io.BytesIO()
    wb.save(buf)
    import_smallcase(db_session, load_workbook=lambda _p: buf.getvalue(), now=_NOW)
    open_row = db_session.scalar(select(SmallcasePeriod).where(SmallcasePeriod.period_end.is_(None)))
    assert open_row is not None


def test_ampersand_and_and_are_the_same_word(db_session: Session) -> None:
    _add_sec(db_session, "INE018A01030", "Larsen & Toubro Limited")
    map_names(db_session, ["Larsen and Toubro Ltd."])
    assert db_session.get(NameMap, "Larsen and Toubro Ltd.").isin == "INE018A01030"


def test_ltd_vs_limited_with_punctuation(db_session: Session) -> None:
    _add_sec(db_session, "INE0AAA01011", "Acme Industries Limited")
    map_names(db_session, ["ACME Industries Ltd."])
    assert db_session.get(NameMap, "ACME Industries Ltd.").isin == "INE0AAA01011"


def test_ambiguous_name_is_not_auto_accepted(db_session: Session) -> None:
    _add_sec(db_session, "INE0BBB01011", "Foo Holdings Limited")
    _add_sec(db_session, "INE0BBB01029", "Foo Holdings Ltd")
    map_names(db_session, ["Foo Holdings"])
    assert db_session.get(NameMap, "Foo Holdings").isin is None


def test_close_sibling_names_are_not_auto_accepted(db_session: Session) -> None:
    _add_sec(db_session, "INE0CCC01011", "Tata Power Company Limited")
    _add_sec(db_session, "INE0CCC01029", "Tata Power Renewable Limited")
    map_names(db_session, ["Tata Power Co"])
    assert db_session.get(NameMap, "Tata Power Co").isin == "INE0CCC01011"
    map_names(db_session, ["Tata Power Renew"])
    assert db_session.get(NameMap, "Tata Power Renew").isin is None


def test_threshold_cannot_be_lowered_below_floor(db_session: Session) -> None:
    _add_sec(db_session, "INE0DDD01011", "Bharat Heavy Electricals Limited")
    map_names(db_session, ["Bharat Electricals"], fuzzy_threshold=0.1)
    assert db_session.get(NameMap, "Bharat Electricals").isin is None


def test_renamed_company_needs_manual_review_then_override(db_session: Session, tmp_path) -> None:
    _add_sec(db_session, "INE758T01015", "Eternal Limited")
    review = tmp_path / "review.csv"
    map_names(db_session, ["Zomato Ltd"], review_path=review)
    assert db_session.get(NameMap, "Zomato Ltd").isin is None
    assert "Zomato Ltd" in review.read_text(encoding="utf-8")
    review.write_text("raw_name,isin,notes\nZomato Ltd,INE758T01015,renamed\n", encoding="utf-8")
    map_names(db_session, ["Zomato Ltd"], review_path=review)
    nm = db_session.get(NameMap, "Zomato Ltd")
    assert (nm.isin, nm.method, nm.verified) == ("INE758T01015", "manual", True)
    assert "Zomato Ltd" not in review.read_text(encoding="utf-8")


def test_manual_override_with_unknown_isin_is_ignored(db_session: Session, tmp_path) -> None:
    review = tmp_path / "review.csv"
    review.write_text("raw_name,isin,notes\nGhost Ltd,INE000000000,x\n", encoding="utf-8")
    map_names(db_session, ["Ghost Ltd"], review_path=review)
    assert db_session.get(NameMap, "Ghost Ltd").isin is None


def test_unmapped_names_are_retried_after_master_grows(db_session: Session) -> None:
    map_names(db_session, ["Late Arrival Limited"])
    assert db_session.get(NameMap, "Late Arrival Limited").isin is None
    _add_sec(db_session, "INE0EEE01011", "Late Arrival Limited")
    map_names(db_session, ["Late Arrival Limited"])
    assert db_session.get(NameMap, "Late Arrival Limited").isin == "INE0EEE01011"


def test_review_file_is_rewritten_not_appended(db_session: Session, tmp_path) -> None:
    review = tmp_path / "review.csv"
    for _ in range(2):
        map_names(db_session, ["Nobody Limited"], review_path=review)
    assert review.read_text(encoding="utf-8").count("Nobody Limited") == 1


def test_map_names_links_isin_onto_periods_and_events(db_session: Session) -> None:
    _seed_securities(db_session)
    result = import_smallcase(db_session, load_workbook=_load_workbook, now=_NOW)
    map_names(db_session, result.stock_names)
    db_session.commit()
    period_isin = db_session.scalar(
        select(SmallcasePeriod.isin).where(SmallcasePeriod.raw_name == "Infosys Limited")
    )
    event_isin = db_session.scalar(
        select(SmallcaseEvent.isin).where(SmallcaseEvent.raw_name == "HDFC Bank Limited")
    )
    assert period_isin == "INE009A01021"
    assert event_isin == "INE040A01034"
