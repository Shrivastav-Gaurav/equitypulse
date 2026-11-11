"""Equity master: NSE EQUITY_L.csv download, parse, and upsert into securities/symbol_history.

HTTP is mocked with respx; all DB tests use the in-memory fixture DB.
"""
# ruff: noqa: E501  — CSV header strings must match the real file format exactly

import datetime as dt
import textwrap

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import Security, Snapshot, SymbolHistory
from equitypulse.master import LoadResult, ensure_security, load_equity_master
from equitypulse.sources.nse import (
    EQUITY_LIST_URL,
    NseFetchError,
    NseFormatError,
    fetch_equity_list,
    parse_equity_list,
)
from tests.conftest import FIXTURES

EQUITY_CSV = (FIXTURES / "nse" / "EQUITY_L_sample.csv").read_text(encoding="utf-8")

# A minimal two-row CSV for targeted tests (EQ + BE)
_MINI = textwrap.dedent("""\
    SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
    RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10
    HDFCBANK,HDFC Bank Limited,EQ,08-NOV-1995,1,1,INE040A01034,1
""")


# ── parse ─────────────────────────────────────────────────────────────────────


def test_parse_equity_list_returns_all_rows() -> None:
    rows = parse_equity_list(EQUITY_CSV)
    assert len(rows) == 20


def test_parse_equity_list_strips_header_spaces() -> None:
    rows = parse_equity_list(EQUITY_CSV)
    by_isin = {r.isin: r for r in rows}
    r = by_isin["INE002A01018"]
    assert r.symbol == "RELIANCE"
    assert r.name == "Reliance Industries Limited"
    assert r.series == "EQ"
    assert r.listing_date == dt.date(1995, 11, 29)
    assert r.isin == "INE002A01018"


def test_parse_equity_list_includes_be_and_bz_series() -> None:
    rows = parse_equity_list(EQUITY_CSV)
    series = {r.series for r in rows}
    assert "EQ" in series
    assert "BE" in series
    assert "BZ" in series


def test_parse_equity_list_rejects_empty_text() -> None:
    with pytest.raises(NseFormatError, match="empty"):
        parse_equity_list("")


def test_parse_equity_list_rejects_missing_isin_column() -> None:
    bad = "SYMBOL,NAME OF COMPANY, SERIES\nREL,Reliance,EQ\n"
    with pytest.raises(NseFormatError, match="ISIN"):
        parse_equity_list(bad)


def test_parse_equity_list_rejects_malformed_isin() -> None:
    bad = textwrap.dedent("""\
        SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
        RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,BADFORMAT,10
    """)
    with pytest.raises(NseFormatError, match="ISIN"):
        parse_equity_list(bad)


def test_parse_equity_list_rejects_duplicate_isin() -> None:
    dup = _MINI + "RELIANCE2,Reliance Copy,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    with pytest.raises(NseFormatError, match="duplicate"):
        parse_equity_list(dup)


def test_parse_equity_list_rejects_bad_listing_date() -> None:
    bad = textwrap.dedent("""\
        SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
        RELIANCE,Reliance Industries Limited,EQ,NOT-A-DATE,10,1,INE002A01018,10
    """)
    with pytest.raises(NseFormatError, match="listing_date"):
        parse_equity_list(bad)


# ── fetch ─────────────────────────────────────────────────────────────────────


def _no_sleep(_: float) -> None:
    pass


@respx.mock
def test_fetch_equity_list_sends_browser_headers() -> None:
    route = respx.get(EQUITY_LIST_URL).mock(return_value=httpx.Response(200, text=EQUITY_CSV))
    text = fetch_equity_list(sleep=_no_sleep)
    assert text == EQUITY_CSV
    req = route.calls.last.request
    assert "Mozilla" in req.headers["user-agent"]


@respx.mock
def test_fetch_equity_list_retries_server_errors() -> None:
    route = respx.get(EQUITY_LIST_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=EQUITY_CSV)]
    )
    assert fetch_equity_list(sleep=_no_sleep) == EQUITY_CSV
    assert route.call_count == 2


@respx.mock
def test_fetch_equity_list_does_not_retry_404() -> None:
    route = respx.get(EQUITY_LIST_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NseFetchError, match="404"):
        fetch_equity_list(sleep=_no_sleep)
    assert route.call_count == 1


@respx.mock
def test_fetch_equity_list_gives_up_after_max_attempts() -> None:
    route = respx.get(EQUITY_LIST_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(NseFetchError, match="3 attempts"):
        fetch_equity_list(attempts=3, sleep=_no_sleep)
    assert route.call_count == 3


# ── load_equity_master ────────────────────────────────────────────────────────


def _fetch_fixture() -> str:
    return EQUITY_CSV


def test_load_inserts_all_rows_first_run(db_session: Session) -> None:
    result = load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    assert isinstance(result, LoadResult)
    assert result.inserted == 20
    assert result.updated == 0
    count = db_session.scalar(select(Security).where(Security.status == "ACTIVE").with_only_columns(  # noqa
        __import__("sqlalchemy").func.count()
    ))
    # just verify via the result
    assert result.inserted == 20


def test_load_inserts_securities_and_symbol_history(db_session: Session) -> None:
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.symbol == "RELIANCE"
    assert sec.status == "ACTIVE"
    hist = db_session.scalars(
        select(SymbolHistory).where(SymbolHistory.isin == "INE002A01018")
    ).all()
    assert len(hist) == 1
    assert hist[0].symbol == "RELIANCE"
    assert hist[0].valid_to is None


def test_load_stores_snapshot(db_session: Session) -> None:
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    snaps = db_session.scalars(select(Snapshot)).all()
    assert len(snaps) == 1
    assert snaps[0].source == "NSE"
    assert snaps[0].endpoint == "EQUITY_L.csv"


def test_load_is_idempotent(db_session: Session) -> None:
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    result2 = load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    assert result2.inserted == 0
    assert result2.updated == 0


def test_load_updates_symbol_on_rename(db_session: Session) -> None:
    # First load: RELIANCE is active.
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()

    # Second load with RELIANCE renamed to RELIANCE2.
    new_csv = EQUITY_CSV.replace("RELIANCE,Reliance Industries Limited", "RELIANCE2,Reliance Industries Limited")

    def fetch_renamed() -> str:
        return new_csv

    as_of = dt.date(2026, 11, 12)
    result = load_equity_master(db_session, fetch=fetch_renamed, as_of=as_of)
    db_session.commit()
    assert result.renamed >= 1
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.symbol == "RELIANCE2"
    # Old history row should be closed; new one open.
    hist = db_session.scalars(
        select(SymbolHistory).where(SymbolHistory.isin == "INE002A01018").order_by(SymbolHistory.valid_from)
    ).all()
    assert len(hist) == 2
    assert hist[0].symbol == "RELIANCE"
    assert hist[0].valid_to is not None
    assert hist[1].symbol == "RELIANCE2"
    assert hist[1].valid_to is None


def test_load_deactivates_missing_isins(db_session: Session) -> None:
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()

    # Remove RELIANCE from the list.
    lines = EQUITY_CSV.splitlines()
    shrunk = "\n".join(line for line in lines if "INE002A01018" not in line) + "\n"

    def fetch_shrunk() -> str:
        return shrunk

    result = load_equity_master(db_session, fetch=fetch_shrunk)
    db_session.commit()
    assert result.deactivated >= 1
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.status == "UNKNOWN_OR_DELISTED"


def test_load_reactivates_returning_isin(db_session: Session) -> None:
    # Load, deactivate RELIANCE, then reload with it back.
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()

    lines = EQUITY_CSV.splitlines()
    shrunk = "\n".join(line for line in lines if "INE002A01018" not in line) + "\n"
    load_equity_master(db_session, fetch=lambda: shrunk)
    db_session.commit()

    result = load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.status == "ACTIVE"
    assert result.inserted == 0  # was just deactivated, not new


def test_load_refuses_truncated_list(db_session: Session) -> None:
    # Seed 20 rows, then try to load only 1 row (5% of 20 = way below 90% threshold).
    load_equity_master(db_session, fetch=_fetch_fixture)
    db_session.commit()

    one_row = (
        "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
        "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    )
    with pytest.raises(NseFormatError, match="truncat"):
        load_equity_master(db_session, fetch=lambda: one_row)


# ── ensure_security ───────────────────────────────────────────────────────────


def test_ensure_security_inserts_unknown_row(db_session: Session) -> None:
    as_of = dt.date(2026, 11, 11)
    ensure_security(
        db_session, "INE002A01018", "RELIANCE", source="NSE_BHAVCOPY", as_of=as_of
    )
    db_session.commit()
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.status == "UNKNOWN_OR_DELISTED"
    assert sec.symbol == "RELIANCE"
    assert sec.source == "NSE_BHAVCOPY"


def test_ensure_security_does_not_overwrite_existing(db_session: Session) -> None:
    as_of = dt.date(2026, 11, 11)
    load_equity_master(db_session, fetch=_fetch_fixture, as_of=as_of)
    db_session.commit()
    # RELIANCE already exists as ACTIVE; ensure_security should leave it alone.
    ensure_security(
        db_session, "INE002A01018", "RELIANCE", source="NSE_BHAVCOPY", as_of=as_of
    )
    db_session.commit()
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None
    assert sec.status == "ACTIVE"  # not overwritten


def test_ensure_security_is_idempotent(db_session: Session) -> None:
    as_of = dt.date(2026, 11, 11)
    ensure_security(db_session, "INE999Z99999", "NEWCO", source="NSE_BHAVCOPY", as_of=as_of)
    db_session.commit()
    ensure_security(db_session, "INE999Z99999", "NEWCO", source="NSE_BHAVCOPY", as_of=as_of)
    db_session.commit()
    count = len(db_session.scalars(select(Security).where(Security.isin == "INE999Z99999")).all())
    assert count == 1
