"""T18: legacy bhavcopy and MTO delivery (pre-UDiFF), recorded from NSE on 2021-06-15."""

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Snapshot
from equitypulse.prices import load_bhavcopy
from equitypulse.sources.nse import NseFileMissing, NseFormatError, parse_bhavcopy_delivery
from equitypulse.sources.nse_legacy import (
    LEGACY_BHAVCOPY_URL,
    MTO_URL,
    UDIFF_START,
    parse_legacy_bhavcopy,
    parse_mto,
)
from tests.conftest import FIXTURES

D = dt.date(2021, 6, 15)
LEGACY = (FIXTURES / "nse" / "legacy_bhavcopy_15JUN2021.csv").read_text(encoding="utf-8")
MTO = (FIXTURES / "nse" / "MTO_15062021.DAT").read_text(encoding="utf-8")
FULL = (FIXTURES / "nse" / "sec_bhavdata_full_15062021.csv").read_text(encoding="utf-8")


def test_urls_match_the_verified_archive_paths() -> None:
    assert LEGACY_BHAVCOPY_URL(D) == (
        "https://nsearchives.nseindia.com/content/historical/EQUITIES/2021/JUN/"
        "cm15JUN2021bhav.csv.zip"
    )
    assert MTO_URL(dt.date(2019, 1, 2)).endswith("/archives/equities/mto/MTO_02012019.DAT")
    assert UDIFF_START == dt.date(2024, 7, 8)


def test_parse_legacy_keeps_eq_and_be_rows_and_drops_other_segments() -> None:
    rows = parse_legacy_bhavcopy(LEGACY, D)
    series = sorted({r.series for r in rows})
    assert series == ["BE", "EQ"]  # the GB (government bond) row is dropped
    assert sum(r.series == "EQ" for r in rows) == 10
    rel = next(r for r in rows if r.symbol == "RELIANCE")
    assert (rel.isin, rel.open, rel.high, rel.low, rel.close, rel.prev_close, rel.volume) == (
        "INE002A01018", 2261.0, 2274.9, 2240.3, 2250.0, 2244.9, 7002898
    )
    assert rel.traded_value == pytest.approx(15808611100.25)


def test_parse_legacy_refuses_a_file_for_another_date() -> None:
    with pytest.raises(NseFormatError, match="dated 2021-06-15"):
        parse_legacy_bhavcopy(LEGACY, dt.date(2021, 6, 16))


def test_parse_legacy_refuses_missing_columns() -> None:
    with pytest.raises(NseFormatError, match="ISIN"):
        parse_legacy_bhavcopy("SYMBOL,SERIES,OPEN\nX,EQ,1\n", D)


def test_parse_legacy_refuses_a_duplicate_isin_in_one_series() -> None:
    lines = LEGACY.strip().splitlines()
    eq_line = next(ln for ln in lines if ln.startswith("RELIANCE,EQ,"))
    with pytest.raises(NseFormatError, match="duplicate"):
        parse_legacy_bhavcopy("\n".join([*lines, eq_line]), D)


def test_parse_mto() -> None:
    deliv = parse_mto(MTO, D)
    rel = deliv[("RELIANCE", "EQ")]
    assert (rel.delivery_qty, rel.delivery_pct) == (2501188, pytest.approx(0.3572))
    assert all(series == "EQ" for _sym, series in deliv)  # rolling settlement only


def test_parse_mto_refuses_a_file_for_another_date() -> None:
    with pytest.raises(NseFormatError, match="dated 2021-06-15"):
        parse_mto(MTO, dt.date(2021, 6, 14))


def test_mto_and_sec_bhavdata_full_agree_where_both_exist() -> None:
    mto = parse_mto(MTO, D)
    full = parse_bhavcopy_delivery(FULL)
    common = set(mto) & set(full)
    assert len(common) == 10
    for key in common:
        assert mto[key].delivery_qty == full[key].delivery_qty, key
        assert mto[key].delivery_pct == pytest.approx(full[key].delivery_pct, abs=1e-9), key


def _fail(_d: dt.date) -> str:
    raise AssertionError("this format must not be fetched for that date")


def _missing(_d: dt.date) -> str:
    raise NseFileMissing("NSE delivery bhavcopy returned HTTP 404")


def test_legacy_date_uses_the_full_delivery_file_where_it_exists(db_session: Session) -> None:
    result = load_bhavcopy(
        db_session, D, fetch_udiff=_fail, fetch_legacy=lambda _d: LEGACY,
        fetch_delivery=lambda _d: FULL, fetch_mto=_fail,
    )
    assert result.inserted == 12
    rel = db_session.get(PriceDaily, ("INE002A01018", D))
    assert (rel.delivery_qty, rel.delivery_pct) == (2501188, pytest.approx(0.3572))
    be = db_session.scalars(select(PriceDaily).where(PriceDaily.series == "BE")).all()
    assert be and all(r.delivery_pct is None for r in be)  # NSE prints "-" for BE: left empty
    snap = db_session.scalar(select(Snapshot).where(Snapshot.endpoint == "bhavcopy-legacy"))
    assert '"delivery":"sec_bhavdata_full"' in snap.payload_json


def test_legacy_date_falls_back_to_mto_when_the_full_file_is_missing(db_session: Session) -> None:
    load_bhavcopy(
        db_session, D, fetch_udiff=_fail, fetch_legacy=lambda _d: LEGACY,
        fetch_delivery=_missing, fetch_mto=lambda _d: MTO,
    )
    rel = db_session.get(PriceDaily, ("INE002A01018", D))
    assert (rel.delivery_qty, rel.delivery_pct) == (2501188, pytest.approx(0.3572))
    be = db_session.scalars(select(PriceDaily).where(PriceDaily.series == "BE")).all()
    assert be and all(r.delivery_pct is None for r in be)  # no BE figure in MTO either
    snap = db_session.scalar(select(Snapshot).where(Snapshot.endpoint == "bhavcopy-legacy"))
    assert '"delivery":"MTO"' in snap.payload_json


def test_udiff_dates_never_touch_the_legacy_files(db_session: Session) -> None:
    udiff = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
    delivery = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")
    result = load_bhavcopy(
        db_session, dt.date(2026, 11, 11), fetch_udiff=lambda _d: udiff,
        fetch_delivery=lambda _d: delivery, fetch_legacy=_fail, fetch_mto=_fail,
    )
    assert result.inserted == 10


def test_legacy_reload_is_idempotent(db_session: Session) -> None:
    kw = dict(fetch_udiff=_fail, fetch_legacy=lambda _d: LEGACY,
              fetch_delivery=lambda _d: FULL, fetch_mto=_fail)
    load_bhavcopy(db_session, D, **kw)
    again = load_bhavcopy(db_session, D, **kw)
    assert (again.inserted, again.updated) == (0, 0)
