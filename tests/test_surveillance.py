"""T9: Surveillance lists from NSE's daily REG1_IND regulatory file.

The fixture keeps the real column names and layout; the stage values on the named stocks are
synthetic (they exercise the parser, they are not a statement about those stocks today).
"""

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import Security, Surveillance
from equitypulse.sources.nse import NseFetchError, NseFormatError
from equitypulse.sources.surveillance import (
    fetch_reg_ind,
    parse_reg_ind,
    parse_surveillance,
    reg_ind_url,
)
from equitypulse.surveillance_load import load_surveillance
from tests.conftest import FIXTURES

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.UTC)
TEXT = (FIXTURES / "nse" / "REG1_IND_sample.csv").read_text(encoding="utf-8")
ROWS = parse_reg_ind(TEXT, min_rows=1)
ISINS = {
    "20MICRONS": "INE144J01027", "RELIANCE": "INE002A01018", "YESBANK": "INE528G01035",
    "IDEA": "INE669E01016", "IRCTC": "INE335Y01020", "ANKITMETAL": "INE106I01010",
    "AGSTRA": "INE0AGS01010", "3IINFOLTD": "INE701B01021",
}


def _fetch(list_type: str, as_of: dt.date) -> list:
    return ROWS


def _seed(session: Session) -> None:
    for sym, isin in ISINS.items():
        session.add(
            Security(isin=isin, symbol=sym, name=sym, status="ACTIVE", source="T", as_of=DATE)
        )
    session.flush()


# ── parse ────────────────────────────────────────────────────────────────────


def test_url_uses_ddmmyy() -> None:
    assert reg_ind_url(DATE).endswith("/content/cm/REG1_IND111126.csv")


def test_parse_keeps_only_needed_columns() -> None:
    assert len(ROWS) == 9
    assert set(ROWS[0]) == {
        "Symbol", "Series", "GSM",
        "Long_Term_Additional_Surveillance_Measure (Long Term ASM)",
        "Short_Term_Additional_Surveillance_Measure (Short Term ASM)", "ESM",
    }


def test_asm_combines_long_and_short_term_stages() -> None:
    got = {e.symbol: e.stage for e in parse_surveillance(ROWS, "ASM")}
    assert got == {"YESBANK": "LT1", "IDEA": "LT4+ST2", "IRCTC": "ST1"}


def test_gsm_stage_zero_is_a_real_stage_and_100_is_not_listed() -> None:
    got = {e.symbol: e.stage for e in parse_surveillance(ROWS, "GSM")}
    assert got == {"ANKITMETAL": "0", "AGSTRA": "2"}


def test_esm_stages() -> None:
    got = {e.symbol: e.stage for e in parse_surveillance(ROWS, "ESM")}
    assert got == {"3IINFOLTD": "1", "SMESTOCK": "2"}


def test_parse_rejects_missing_column() -> None:
    with pytest.raises(NseFormatError, match="missing columns"):
        parse_reg_ind("Symbol,Series\nX,EQ\n", min_rows=1)


def test_parse_rejects_truncated_file() -> None:
    with pytest.raises(NseFormatError, match="truncated"):
        parse_reg_ind(TEXT)  # default minimum is ~1000 rows


def test_unknown_list_type_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        parse_surveillance(ROWS, "XYZ")


# ── load ─────────────────────────────────────────────────────────────────────


def test_known_asm_stock_is_loaded_with_isin(db_session: Session) -> None:
    _seed(db_session)
    result = load_surveillance(db_session, "ASM", fetch=_fetch, as_of=DATE, now=_NOW)
    assert result.inserted == 3
    row = db_session.get(Surveillance, (DATE, ISINS["YESBANK"], "ASM"))
    assert row.stage == "LT1"
    assert row.source == "NSE_REG_IND"


def test_symbols_without_a_securities_row_are_counted_not_stored(db_session: Session) -> None:
    _seed(db_session)
    result = load_surveillance(db_session, "ESM", fetch=_fetch, as_of=DATE, now=_NOW)
    assert (result.inserted, result.unmapped_symbols) == (1, 1)  # SMESTOCK is not in the master


def test_load_is_idempotent(db_session: Session) -> None:
    _seed(db_session)
    load_surveillance(db_session, "GSM", fetch=_fetch, as_of=DATE, now=_NOW)
    again = load_surveillance(db_session, "GSM", fetch=_fetch, as_of=DATE, now=_NOW)
    assert (again.inserted, again.already_present) == (0, 2)
    assert len(db_session.scalars(select(Surveillance)).all()) == 2


def test_lists_are_separate_per_date(db_session: Session) -> None:
    _seed(db_session)
    load_surveillance(db_session, "GSM", fetch=_fetch, as_of=DATE, now=_NOW)
    tomorrow = DATE + dt.timedelta(days=1)
    nxt = load_surveillance(db_session, "GSM", fetch=_fetch, as_of=tomorrow, now=_NOW)
    assert nxt.inserted == 2


def test_ambiguous_symbol_is_not_guessed(db_session: Session) -> None:
    _seed(db_session)
    db_session.add(Security(isin="INE0DUP01010", symbol="YESBANK", name="Other", status="ACTIVE",
                            source="T", as_of=DATE))
    db_session.flush()
    result = load_surveillance(db_session, "ASM", fetch=_fetch, as_of=DATE, now=_NOW)
    assert result.inserted == 2 and result.unmapped_symbols == 1


def test_active_security_wins_over_delisted_with_same_symbol(db_session: Session) -> None:
    _seed(db_session)
    db_session.add(
        Security(isin="INE0OLD01010", symbol="IRCTC", name="Old",
                 status="UNKNOWN_OR_DELISTED", source="T", as_of=DATE)
    )
    db_session.flush()
    load_surveillance(db_session, "ASM", fetch=_fetch, as_of=DATE, now=_NOW)
    assert db_session.get(Surveillance, (DATE, ISINS["IRCTC"], "ASM")) is not None


def test_unknown_list_rejected_by_loader(db_session: Session) -> None:
    with pytest.raises(ValueError):
        load_surveillance(db_session, "XYZ", fetch=_fetch, as_of=DATE)


# ── fetch ────────────────────────────────────────────────────────────────────


def test_fetch_404_is_not_retried() -> None:
    with respx.mock:
        respx.get("https://www.nseindia.com").respond(200)
        route = respx.get(reg_ind_url(DATE)).respond(404)
        with pytest.raises(NseFetchError, match="404"):
            fetch_reg_ind(DATE, sleep=lambda _s: None)
        assert route.call_count == 1


def test_fetch_retries_5xx_then_returns_text() -> None:
    with respx.mock:
        respx.get("https://www.nseindia.com").respond(200)
        respx.get(reg_ind_url(DATE)).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, content=TEXT.encode())]
        )
        assert fetch_reg_ind(DATE, sleep=lambda _s: None) == TEXT
