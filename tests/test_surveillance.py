"""Surveillance list tests (ASM, GSM, ESM).

Fetch is mocked; fixture JSON files are real-format sample data.
"""

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import Snapshot, Surveillance
from equitypulse.sources.surveillance import NseFormatError, parse_surveillance
from equitypulse.surveillance_load import LoadSurveillanceResult, load_surveillance
from tests.conftest import FIXTURES

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

ASM_FIXTURE = json.loads((FIXTURES / "nse" / "surveillance_asm_sample.json").read_text())
GSM_FIXTURE = json.loads((FIXTURES / "nse" / "surveillance_gsm_sample.json").read_text())
ESM_FIXTURE = json.loads((FIXTURES / "nse" / "surveillance_esm_sample.json").read_text())


def _make_fetch(fixtures: dict[str, list]) -> callable:
    def fetch(list_type: str) -> list:
        return fixtures[list_type]
    return fetch


_FETCH = _make_fetch({"ASM": ASM_FIXTURE, "GSM": GSM_FIXTURE, "ESM": ESM_FIXTURE})


# ── parse ─────────────────────────────────────────────────────────────────────


def test_parse_asm_returns_entries() -> None:
    entries = parse_surveillance(ASM_FIXTURE, "ASM")
    assert len(entries) == 3
    isins = {e.isin for e in entries}
    assert "INE528G01035" in isins  # YESBANK
    for e in entries:
        assert e.list_type == "ASM"


def test_parse_gsm_returns_stage() -> None:
    entries = parse_surveillance(GSM_FIXTURE, "GSM")
    by_isin = {e.isin: e for e in entries}
    assert by_isin["INE106I01010"].stage == "1"
    assert by_isin["INE583L01014"].stage == "2"


def test_parse_esm() -> None:
    entries = parse_surveillance(ESM_FIXTURE, "ESM")
    assert len(entries) == 1
    assert entries[0].isin == "INE748C01038"


def test_parse_skips_rows_without_isin() -> None:
    rows = [{"symbol": "NONAME", "listname": "ASM"}]  # no ISIN
    entries = parse_surveillance(rows, "ASM")
    assert entries == []


def test_parse_rejects_non_list() -> None:
    with pytest.raises(NseFormatError):
        parse_surveillance({"key": "value"}, "ASM")  # type: ignore[arg-type]


# ── load ─────────────────────────────────────────────────────────────────────


def test_load_asm_inserts_rows(db_session: Session) -> None:
    result = load_surveillance(db_session, "ASM", fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    assert isinstance(result, LoadSurveillanceResult)
    assert result.inserted == 3
    assert result.already_present == 0


def test_load_known_asm_stock_present(db_session: Session) -> None:
    load_surveillance(db_session, "ASM", fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    row = db_session.scalar(
        select(Surveillance).where(
            Surveillance.isin == "INE528G01035",  # YESBANK
            Surveillance.list_type == "ASM",
            Surveillance.as_of == DATE,
        )
    )
    assert row is not None


def test_load_stores_snapshot(db_session: Session) -> None:
    load_surveillance(db_session, "ASM", fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    snap = db_session.scalar(
        select(Snapshot).where(Snapshot.endpoint == "surveillance-asm")
    )
    assert snap is not None
    assert snap.entity == "ASM"
    assert snap.as_of == DATE


def test_load_is_idempotent(db_session: Session) -> None:
    load_surveillance(db_session, "ASM", fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    result2 = load_surveillance(db_session, "ASM", fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    assert result2.inserted == 0
    assert result2.already_present == 3


def test_load_all_three_lists(db_session: Session) -> None:
    for lt in ("ASM", "GSM", "ESM"):
        load_surveillance(db_session, lt, fetch=_FETCH, as_of=DATE, now=_NOW)
    db_session.commit()
    all_rows = db_session.scalars(select(Surveillance)).all()
    types = {r.list_type for r in all_rows}
    assert types == {"ASM", "GSM", "ESM"}


def test_load_rejects_unknown_list_type(db_session: Session) -> None:
    with pytest.raises(ValueError, match="Unknown"):
        load_surveillance(db_session, "XYZ", fetch=_FETCH, as_of=DATE, now=_NOW)
