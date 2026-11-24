"""Hand-verified data (config/manual_data.yaml) and the coverage measure that uses it."""

import datetime as dt
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.adjust import adjusted_prices
from equitypulse.coverage import price_coverage
from equitypulse.db.models import (
    CorporateAction,
    DataAudit,
    IsinLink,
    PriceDaily,
    Security,
    TradingCalendar,
)
from equitypulse.manual_data import ManualData, apply_manual_data, load_manual_data

D0 = dt.date(2026, 1, 5)  # a Monday


def _cal(session: Session, n: int = 21) -> list[dt.date]:
    days = []
    for i in range(n):
        d = D0 + dt.timedelta(days=i)
        trading = d.weekday() < 5
        session.add(TradingCalendar(date=d, is_trading_day=trading, source="T"))
        if trading:
            days.append(d)
    session.flush()
    return days


def _sec(session: Session, isin: str, symbol: str, listed: dt.date | None = None) -> None:
    session.add(Security(isin=isin, symbol=symbol, name=symbol, status="ACTIVE", source="T",
                         as_of=D0, listing_date=listed))
    session.flush()


def _bars(session: Session, isin: str, days: list[dt.date], close: float = 100.0) -> None:
    for d in days:
        session.add(PriceDaily(isin=isin, date=d, open=close, high=close, low=close, close=close,
                               volume=1, series="EQ", source="T"))
    session.flush()


def _data(**kw) -> ManualData:
    return ManualData.model_validate(kw)


# ── the committed file ────────────────────────────────────────────────────────


def test_committed_file_loads_and_is_consistent() -> None:
    data = load_manual_data()
    assert {g.symbol for g in data.gaps} == {"SHRIPISTON", "FORCEMOT", "PATANJALI"}
    assert {(a.symbol, a.ex_date, a.action_type) for a in data.corporate_actions} == {
        ("IIFL", dt.date(2019, 5, 30), "DEMERGER"), ("BSOFT", dt.date(2019, 1, 24), "DEMERGER"),
    }
    assert data.isin_links[0].reason == "RECONSTRUCTION"
    for g in data.gaps:
        assert g.start <= g.end


def test_a_manual_split_or_bonus_is_refused(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/manual_data.yaml").read_text(encoding="utf-8"))
    raw["corporate_actions"][0]["action_type"] = "SPLIT"  # a factor must come from NSE data
    with pytest.raises(ValidationError):
        ManualData.model_validate(raw)


# ── applying it ───────────────────────────────────────────────────────────────


def test_manual_demerger_is_added_once_with_no_factor(db_session: Session) -> None:
    _sec(db_session, "INE530B01024", "IIFL")
    data = _data(corporate_actions=[{
        "isin": "INE530B01024", "symbol": "IIFL", "ex_date": "2019-05-30",
        "action_type": "DEMERGER", "details": "IIFL Holdings demerger",
    }])
    assert apply_manual_data(db_session, data).actions_added == 1
    assert apply_manual_data(db_session, data).actions_added == 0
    action = db_session.scalar(select(CorporateAction))
    assert (action.action_type, action.adj_factor, action.source) == ("DEMERGER", None, "manual")


def test_manual_action_for_an_unknown_isin_fails_loudly(db_session: Session) -> None:
    data = _data(corporate_actions=[{
        "isin": "INE000000000", "symbol": "X", "ex_date": "2019-05-30",
        "action_type": "DEMERGER", "details": "x",
    }])
    with pytest.raises(ValueError, match="unknown ISIN"):
        apply_manual_data(db_session, data)


def test_explained_link_stops_the_adjustment_warning(db_session: Session) -> None:
    days = _cal(db_session)
    _sec(db_session, "INE528G01027", "YESBANK")
    _sec(db_session, "INE528G01035", "YESBANK")
    _bars(db_session, "INE528G01027", days[:5])
    _bars(db_session, "INE528G01035", days[5:])
    db_session.add(IsinLink(old_isin="INE528G01027", new_isin="INE528G01035",
                            effective_date=days[5], reason="ISIN_CHANGE", source="NSE_BHAVCOPY"))
    db_session.flush()
    assert adjusted_prices(db_session, "INE528G01035", days[-1]).attrs["warnings"]
    data = _data(isin_links=[{
        "old_isin": "INE528G01027", "new_isin": "INE528G01035", "symbol": "YESBANK",
        "reason": "RECONSTRUCTION", "note": "reconstruction scheme, no split",
    }])
    assert apply_manual_data(db_session, data).links_updated == 1
    assert adjusted_prices(db_session, "INE528G01035", days[-1]).attrs["warnings"] == []


def test_explaining_a_missing_link_fails_loudly(db_session: Session) -> None:
    data = _data(isin_links=[{
        "old_isin": "INE0AAA01011", "new_isin": "INE0AAA01029", "symbol": "X",
        "reason": "RECONSTRUCTION", "note": "x",
    }])
    with pytest.raises(ValueError, match="no ISIN link"):
        apply_manual_data(db_session, data)


def test_each_explanation_is_audited_once(db_session: Session) -> None:
    data = _data(gaps=[{
        "isin": "INE526E01018", "symbol": "SHRIPISTON", "from": "2019-01-01", "to": "2023-11-03",
        "reason": "no trades, illiquid BE series",
    }])
    assert apply_manual_data(db_session, data).audit_rows_added == 1
    assert apply_manual_data(db_session, data).audit_rows_added == 0
    row = db_session.scalar(select(DataAudit).where(DataAudit.status == "OK"))
    assert (row.job, row.as_of) == ("gap_review", dt.date(2019, 1, 1))
    assert "SHRIPISTON" in row.error and "illiquid BE" in row.error


# ── coverage ──────────────────────────────────────────────────────────────────


def test_coverage_counts_gaps_and_marks_explained_days(db_session: Session) -> None:
    days = _cal(db_session)
    _sec(db_session, "INE0AAA01011", "AAA", listed=dt.date(2010, 1, 1))
    _bars(db_session, "INE0AAA01011", [d for i, d in enumerate(days) if i not in (3, 4, 9)])
    note = _data(gaps=[{"isin": "INE0AAA01011", "symbol": "AAA", "from": str(days[3]),
                        "to": str(days[4]), "reason": "no trades"}]).gaps
    r = price_coverage(db_session, {"INE0AAA01011"}, days[0], days[-1], gap_notes=note)
    assert (r.expected, r.missing, r.explained, r.unexplained) == (15, 3, 2, 1)
    assert r.gaps[0].first_missing == days[3] and r.late_starts == []


def test_coverage_starts_at_listing_and_reports_late_starts(db_session: Session) -> None:
    days = _cal(db_session)
    _sec(db_session, "INE0NEW01011", "NEWCO", listed=days[5])  # listed mid-window
    _bars(db_session, "INE0NEW01011", days[5:])
    _sec(db_session, "INE0LAT01011", "LATE", listed=dt.date(2010, 1, 1))  # rows start late
    _bars(db_session, "INE0LAT01011", days[4:])
    r = price_coverage(db_session, {"INE0NEW01011", "INE0LAT01011"}, days[0], days[-1])
    assert r.expected == 10 + 15
    assert r.missing == 4  # LATE's first four days; NEWCO owes nothing before listing
    assert [g.symbol for g in r.late_starts] == ["LATE"]
    assert r.late_starts[0].first_row == days[4]


def test_coverage_reads_the_whole_isin_chain_and_lists_securities_without_rows(
    db_session: Session,
) -> None:
    days = _cal(db_session)
    _sec(db_session, "INE237A01028", "KOTAKBANK", listed=dt.date(1995, 1, 1))
    _sec(db_session, "INE237A01036", "KOTAKBANK", listed=dt.date(1995, 1, 1))
    _sec(db_session, "INE041025011", "EMBASSY", listed=dt.date(2019, 4, 1))
    _bars(db_session, "INE237A01028", days[:7])
    _bars(db_session, "INE237A01036", days[7:])
    db_session.add(IsinLink(old_isin="INE237A01028", new_isin="INE237A01036",
                            effective_date=days[7], reason="ISIN_CHANGE", source="T"))
    db_session.flush()
    r = price_coverage(db_session, {"INE237A01036", "INE041025011"}, days[0], days[-1])
    assert (r.expected, r.missing) == (15, 0)
    assert r.no_rows == ["EMBASSY"]
