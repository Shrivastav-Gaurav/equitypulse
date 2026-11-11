"""Daily prices (bhavcopy): NSE UDiFF + delivery file, parse and upsert prices_daily.

Two fixture files for 2026-11-11 (a real trading day):
  bhavcopy_udiff_20261111.csv   — UDiFF format, has ISIN and prices, no delivery
  bhavcopy_delivery_20261111.csv — sec_bhavdata_full format, has SYMBOL + delivery %

HTTP is mocked with respx; all DB tests use the fixture DB.
"""

# ruff: noqa: E501

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Security
from equitypulse.prices import LoadPricesResult, load_bhavcopy
from equitypulse.sources.nse import (
    BHAVCOPY_DELIVERY_URL,
    BHAVCOPY_UDIFF_URL,
    NseFetchError,
    NseFormatError,
    fetch_bhavcopy_delivery,
    parse_bhavcopy_delivery,
    parse_bhavcopy_udiff,
)
from tests.conftest import FIXTURES

DATE = dt.date(2026, 11, 11)
UDIFF_CSV = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
DELIVERY_CSV = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")


# ── parse UDiFF ───────────────────────────────────────────────────────────────


def test_parse_udiff_returns_eq_rows() -> None:
    rows = parse_bhavcopy_udiff(UDIFF_CSV)
    assert len(rows) == 10
    by_isin = {r.isin: r for r in rows}
    r = by_isin["INE002A01018"]  # RELIANCE
    assert r.symbol == "RELIANCE"
    assert r.date == DATE
    assert r.series == "EQ"
    assert r.close > 0
    assert r.volume > 0


def test_parse_udiff_all_eq_series() -> None:
    rows = parse_bhavcopy_udiff(UDIFF_CSV)
    assert all(r.series == "EQ" for r in rows)


def test_parse_udiff_rejects_empty() -> None:
    with pytest.raises(NseFormatError, match="empty"):
        parse_bhavcopy_udiff("")


def test_parse_udiff_rejects_missing_isin_column() -> None:
    bad = "TradDt,SctySrs,TckrSymb\n2026-11-11,EQ,RELIANCE\n"
    with pytest.raises(NseFormatError, match="ISIN"):
        parse_bhavcopy_udiff(bad)


def test_parse_udiff_rejects_duplicate_isin() -> None:
    lines = UDIFF_CSV.splitlines()
    # Find header row (skip BOM if present), then duplicate first data row.
    header_idx = next(i for i, ln in enumerate(lines) if "TradDt" in ln)
    data_row = lines[header_idx + 1]
    dup = "\n".join(lines[:header_idx + 2] + [data_row] + lines[header_idx + 2:]) + "\n"
    with pytest.raises(NseFormatError, match="duplicate"):
        parse_bhavcopy_udiff(dup)


def test_parse_udiff_rejects_bad_date() -> None:
    bad = UDIFF_CSV.replace("2026-11-11", "not-a-date", 1)
    with pytest.raises(NseFormatError, match="date"):
        parse_bhavcopy_udiff(bad)


# ── parse delivery ────────────────────────────────────────────────────────────


def test_parse_delivery_returns_symbol_keyed_dict() -> None:
    deliv = parse_bhavcopy_delivery(DELIVERY_CSV)
    assert "RELIANCE" in deliv
    r = deliv["RELIANCE"]
    assert 0 < r.delivery_pct < 1  # stored as fraction
    assert r.delivery_qty is not None and r.delivery_qty > 0


def test_parse_delivery_handles_dash_as_null() -> None:
    # 21STCENMGM has DELIV_QTY=- and DELIV_PER=- in real data
    csv_with_dash = (
        "SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER\n"
        "IDEA,EQ,11-Nov-2026,10.00,10.50,11.00,10.00,10.50,10.50,10.75,100000,100.00,5000,-,-\n"
    )
    deliv = parse_bhavcopy_delivery(csv_with_dash)
    r = deliv["IDEA"]
    assert r.delivery_qty is None
    assert r.delivery_pct is None


def test_parse_delivery_rejects_empty() -> None:
    with pytest.raises(NseFormatError, match="empty"):
        parse_bhavcopy_delivery("")


# ── fetch ─────────────────────────────────────────────────────────────────────


def _no_sleep(_: float) -> None:
    pass


@respx.mock
def test_fetch_udiff_constructs_correct_url() -> None:
    url = BHAVCOPY_UDIFF_URL(DATE)
    route = respx.get(url).mock(return_value=httpx.Response(200, content=b"PK"))
    # We test the URL construction, not the unzip (which needs real zip data).
    # fetch_bhavcopy_udiff returns text; here we just verify the URL is called.
    # Use the low-level helper that returns raw bytes instead.
    from equitypulse.sources.nse import _fetch_bhavcopy_udiff_bytes
    _fetch_bhavcopy_udiff_bytes(DATE, sleep=_no_sleep)
    assert route.called
    assert "20261111" in str(route.calls.last.request.url)


@respx.mock
def test_fetch_delivery_constructs_correct_url() -> None:
    url = BHAVCOPY_DELIVERY_URL(DATE)
    route = respx.get(url).mock(return_value=httpx.Response(200, text=DELIVERY_CSV))
    text = fetch_bhavcopy_delivery(DATE, sleep=_no_sleep)
    assert text == DELIVERY_CSV
    assert route.called


@respx.mock
def test_fetch_delivery_retries_server_errors() -> None:
    url = BHAVCOPY_DELIVERY_URL(DATE)
    route = respx.get(url).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=DELIVERY_CSV)]
    )
    fetch_bhavcopy_delivery(DATE, sleep=_no_sleep)
    assert route.call_count == 2


@respx.mock
def test_fetch_delivery_raises_on_404() -> None:
    url = BHAVCOPY_DELIVERY_URL(DATE)
    respx.get(url).mock(return_value=httpx.Response(404))
    with pytest.raises(NseFetchError, match="404"):
        fetch_bhavcopy_delivery(DATE, sleep=_no_sleep)


# ── load_bhavcopy ─────────────────────────────────────────────────────────────


def _fetch_udiff(_date: dt.date) -> str:
    return UDIFF_CSV


def _fetch_delivery(_date: dt.date) -> str:
    return DELIVERY_CSV


def _seed_securities(session: Session) -> None:
    """Pre-seed securities so FK is satisfied for all 10 fixture ISINs."""
    isins = {
        "INE002A01018": "RELIANCE",
        "INE296A01032": "BAJFINANCE",
        "INE040A01034": "HDFCBANK",
        "INE335Y01020": "IRCTC",
        "INE009A01021": "INFY",
        "INE154A01025": "ITC",
        "INE018A01030": "LT",
        "INE467B01029": "TCS",
        "INE062A01020": "SBIN",
        "INE144J01027": "20MICRONS",
    }
    for isin, sym in isins.items():
        session.add(
            Security(
                isin=isin,
                symbol=sym,
                name=sym,
                series="EQ",
                status="ACTIVE",
                source="TEST",
                as_of=DATE,
            )
        )
    session.flush()


def test_load_bhavcopy_inserts_rows(db_session: Session) -> None:
    _seed_securities(db_session)
    result = load_bhavcopy(
        db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery
    )
    db_session.commit()
    assert isinstance(result, LoadPricesResult)
    assert result.inserted == 10
    assert result.updated == 0


def test_load_bhavcopy_sets_delivery(db_session: Session) -> None:
    _seed_securities(db_session)
    load_bhavcopy(db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery)
    db_session.commit()
    # RELIANCE should have delivery data from the delivery file.
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    assert row is not None
    assert row.delivery_pct is not None
    assert 0 < row.delivery_pct < 1


def test_load_bhavcopy_is_idempotent(db_session: Session) -> None:
    _seed_securities(db_session)
    load_bhavcopy(db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery)
    db_session.commit()
    result2 = load_bhavcopy(
        db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery
    )
    db_session.commit()
    assert result2.inserted == 0
    assert result2.updated == 0


def test_load_bhavcopy_inserts_unknown_security(db_session: Session) -> None:
    # No securities seeded — loader must call ensure_security.
    result = load_bhavcopy(
        db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery
    )
    db_session.commit()
    assert result.inserted == 10
    # All 10 ISINs should now exist in securities as UNKNOWN_OR_DELISTED.
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.status == "UNKNOWN_OR_DELISTED"


def test_load_bhavcopy_stores_snapshot(db_session: Session) -> None:
    _seed_securities(db_session)
    load_bhavcopy(db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery)
    db_session.commit()
    from equitypulse.db.models import Snapshot
    snaps = db_session.scalars(select(Snapshot)).all()
    sources = {s.source for s in snaps}
    assert "NSE" in sources


def test_load_bhavcopy_golden_check(db_session: Session) -> None:
    """Closes and volumes for RELIANCE on 2026-11-11 match the fixture (from real NSE data)."""
    _seed_securities(db_session)
    load_bhavcopy(db_session, DATE, fetch_udiff=_fetch_udiff, fetch_delivery=_fetch_delivery)
    db_session.commit()
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    assert row is not None
    # Values from the fixture file (real NSE data for 2026-11-11).
    # UDiFF ClsPric for RELIANCE:
    udiff_rows = [r for r in parse_bhavcopy_udiff(UDIFF_CSV) if r.isin == "INE002A01018"]
    assert len(udiff_rows) == 1
    assert row.close == pytest.approx(udiff_rows[0].close, abs=0.01)
    assert row.volume == udiff_rows[0].volume
