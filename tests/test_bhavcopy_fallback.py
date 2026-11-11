"""T8: Bhavcopy fallback — when the NSE fetch fails, use Tapetide get_price_history.

The real tool takes an NSE `symbol`, `days` and `end_date`, returns split/bonus-adjusted
open/high/low/close plus `raw_close`, and the fallback must store raw prices.
"""

import copy
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Security, SymbolHistory
from equitypulse.prices import SOURCE_NSE, SOURCE_TAPETIDE, LoadPricesResult, load_bhavcopy
from equitypulse.sources.nse import NseFetchError
from tests.conftest import FIXTURES

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

_RESPONSE = json.loads(
    (FIXTURES / "tapetide" / "get_price_history_RELIANCE_5d.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def _budget_from_stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget guard reads settings; keep these tests independent of any real .env."""
    monkeypatch.setattr(
        "equitypulse.sources.tapetide.get_settings",
        lambda: SimpleNamespace(tapetide_daily_budget=45),
    )


def _failing_fetch(date: dt.date) -> str:
    raise NseFetchError("NSE is down")


def _fallback(session: Session, caller: Any, **kw: Any) -> LoadPricesResult:
    return load_bhavcopy(
        session, DATE, fetch_udiff=_failing_fetch, fetch_delivery=_failing_fetch,
        now=_NOW, tapetide_caller=caller, **kw,
    )


def _caller_with(bar_overrides: dict[str, Any] | None = None, record: list | None = None):
    payload = copy.deepcopy(_RESPONSE)
    if bar_overrides is not None:
        payload["data"][-1].update(bar_overrides)

    def caller(tool: str, args: dict) -> dict:
        if record is not None:
            record.append((tool, args))
        return payload

    return caller


def _seed(session: Session, isin="INE002A01018", symbol="RELIANCE") -> None:
    session.add(
        Security(isin=isin, symbol=symbol, name=symbol, series="EQ", status="ACTIVE",
                 source="TEST", as_of=DATE)
    )
    session.flush()


def test_fallback_marks_degraded_and_inserts_raw_row(db_session: Session) -> None:
    _seed(db_session)
    result = _fallback(db_session, _caller_with())
    db_session.commit()
    assert result.degraded is True
    assert result.inserted == 1
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    assert row.source == SOURCE_TAPETIDE
    assert row.close == pytest.approx(1335.4)
    assert row.volume == 17554310
    assert row.delivery_pct is None  # absent from the response


def test_fallback_calls_tool_with_symbol_days_and_end_date(db_session: Session) -> None:
    _seed(db_session)
    seen: list = []
    _fallback(db_session, _caller_with(record=seen))
    assert seen == [
        ("get_price_history",
         {"symbol": "RELIANCE", "days": 1, "interval": "daily", "end_date": "2026-11-11"})
    ]


def test_fallback_stores_raw_not_adjusted_prices(db_session: Session) -> None:
    """After a 2:1 split the adjusted close is half the raw print; OHLC are rescaled to raw."""
    _seed(db_session)
    _fallback(db_session, _caller_with(
        {"open": 500.0, "high": 520.0, "low": 490.0, "close": 510.0, "raw_close": 1020.0}
    ))
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    assert row.close == pytest.approx(1020.0)
    assert row.open == pytest.approx(1000.0)
    assert row.high == pytest.approx(1040.0)
    assert row.low == pytest.approx(980.0)


def test_fallback_skips_bar_without_raw_close(db_session: Session) -> None:
    _seed(db_session)
    result = _fallback(db_session, _caller_with({"raw_close": None}))
    assert result.inserted == 0
    assert db_session.get(PriceDaily, ("INE002A01018", DATE)) is None


def test_fallback_delivery_percent_becomes_fraction(db_session: Session) -> None:
    _seed(db_session)
    _fallback(db_session, _caller_with({"delivery_pct": 48.6}))
    assert db_session.get(PriceDaily, ("INE002A01018", DATE)).delivery_pct == pytest.approx(0.486)


def test_fallback_ignores_out_of_range_delivery(db_session: Session) -> None:
    _seed(db_session)
    _fallback(db_session, _caller_with({"delivery_pct": 486}))
    assert db_session.get(PriceDaily, ("INE002A01018", DATE)).delivery_pct is None


def test_fallback_resolves_symbol_for_the_date_from_symbol_history(db_session: Session) -> None:
    _seed(db_session, symbol="NEWSYM")
    db_session.add_all([
        SymbolHistory(isin="INE002A01018", symbol="OLDSYM", valid_from=dt.date(2020, 1, 1),
                      valid_to=dt.date(2026, 11, 30), source="TEST"),
        SymbolHistory(isin="INE002A01018", symbol="NEWSYM", valid_from=dt.date(2026, 12, 1),
                      valid_to=None, source="TEST"),
    ])
    db_session.flush()
    seen: list = []
    _fallback(db_session, _caller_with(record=seen))
    assert seen[0][1]["symbol"] == "OLDSYM"


def test_fallback_never_overwrites_an_nse_row(db_session: Session) -> None:
    _seed(db_session)
    db_session.add(PriceDaily(isin="INE002A01018", date=DATE, open=1, high=2, low=1, close=1.5,
                              volume=10, series="EQ", source=SOURCE_NSE))
    db_session.flush()
    seen: list = []
    result = _fallback(db_session, _caller_with(record=seen))
    assert result.inserted == 0
    assert seen == []  # no call spent on a date that is already loaded
    assert db_session.get(PriceDaily, ("INE002A01018", DATE)).close == 1.5


def test_fallback_stops_when_budget_is_exhausted(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "equitypulse.sources.tapetide.get_settings",
        lambda: SimpleNamespace(tapetide_daily_budget=1),
    )
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        _seed(db_session, isin=f"INE00000000{i}", symbol=sym)
    seen: list = []
    result = _fallback(db_session, _caller_with(record=seen))
    assert result.degraded is True
    assert len(seen) == 1


def test_no_fallback_when_nse_succeeds(db_session: Session) -> None:
    udiff = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
    delivery = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")
    seen: list = []
    result = load_bhavcopy(
        db_session, DATE, fetch_udiff=lambda _d: udiff, fetch_delivery=lambda _d: delivery,
        now=_NOW, tapetide_caller=_caller_with(record=seen),
    )
    assert result.degraded is False
    assert seen == []


def test_fallback_can_be_disabled_for_bulk_backfills(db_session: Session) -> None:
    _seed(db_session)
    seen: list = []
    with pytest.raises(NseFetchError):
        _fallback(db_session, _caller_with(record=seen), allow_fallback=False)
    assert seen == []


def test_universe_filter_limits_nse_load(db_session: Session) -> None:
    udiff = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
    delivery = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")
    result = load_bhavcopy(
        db_session, DATE, fetch_udiff=lambda _d: udiff, fetch_delivery=lambda _d: delivery,
        now=_NOW, universe={"INE002A01018"},
    )
    assert result.inserted == 1


def test_nse_load_replaces_fallback_row_even_with_identical_close(db_session: Session) -> None:
    udiff = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
    delivery = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")
    kw = dict(fetch_udiff=lambda _d: udiff, fetch_delivery=lambda _d: delivery, now=_NOW)
    load_bhavcopy(db_session, DATE, **kw)
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    row.source = SOURCE_TAPETIDE
    db_session.flush()
    load_bhavcopy(db_session, DATE, **kw)
    assert db_session.get(PriceDaily, ("INE002A01018", DATE)).source == SOURCE_NSE
