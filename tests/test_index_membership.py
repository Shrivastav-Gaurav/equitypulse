"""Index membership: Nifty 500 constituent CSV parse and upsert into index_membership."""

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import IndexMembership, Security
from equitypulse.index_load import LoadIndexResult, load_index_membership
from equitypulse.sources.indices import NseFormatError, parse_index_csv
from tests.conftest import FIXTURES

DATE1 = dt.date(2026, 11, 11)
DATE2 = dt.date(2026, 12, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

NIFTY500_CSV = (FIXTURES / "nse" / "nifty500_sample.csv").read_text(encoding="utf-8")


def _fetch(index_name: str) -> str:
    return NIFTY500_CSV


def _fetch_shrunk(index_name: str) -> str:
    # Remove RELIANCE (INE002A01018) from the list.
    lines = NIFTY500_CSV.splitlines()
    return "\n".join(ln for ln in lines if "INE002A01018" not in ln) + "\n"


# ── parse ─────────────────────────────────────────────────────────────────────


def test_parse_nifty500_returns_all_rows() -> None:
    rows = parse_index_csv(NIFTY500_CSV, "NIFTY500")
    assert len(rows) == 10


def test_parse_nifty500_fields() -> None:
    rows = parse_index_csv(NIFTY500_CSV, "NIFTY500")
    by_isin = {r.isin: r for r in rows}
    r = by_isin["INE002A01018"]
    assert r.symbol == "RELIANCE"
    assert r.name == "Reliance Industries Limited"
    assert r.industry == "Oil Gas & Consumable Fuels"


def test_parse_rejects_empty() -> None:
    with pytest.raises(NseFormatError, match="empty"):
        parse_index_csv("", "NIFTY500")


def test_parse_rejects_missing_isin_column() -> None:
    bad = "Company Name,Symbol\nReliance,RELIANCE\n"
    with pytest.raises(NseFormatError, match="ISIN"):
        parse_index_csv(bad, "NIFTY500")


# ── load ─────────────────────────────────────────────────────────────────────


def test_load_inserts_all_rows(db_session: Session) -> None:
    result = load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW)
    db_session.commit()
    assert isinstance(result, LoadIndexResult)
    assert result.added == 10
    assert result.removed == 0


def test_load_creates_securities_for_unknown_isins(db_session: Session) -> None:
    load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW)
    db_session.commit()
    sec = db_session.get(Security, "INE002A01018")
    assert sec is not None  # ensure_security() was called


def test_load_is_idempotent(db_session: Session) -> None:
    load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW)
    db_session.commit()
    result2 = load_index_membership(
        db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW
    )
    db_session.commit()
    assert result2.added == 0
    assert result2.removed == 0
    assert result2.unchanged == 10


def test_load_closes_removed_member(db_session: Session) -> None:
    # First load: 10 members.
    load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW)
    db_session.commit()
    # Second load: RELIANCE removed.
    result = load_index_membership(
        db_session, "NIFTY500", fetch=_fetch_shrunk, as_of=DATE2, now=_NOW
    )
    db_session.commit()
    assert result.removed == 1
    # RELIANCE's row should be closed.
    row = db_session.scalar(
        select(IndexMembership).where(
            IndexMembership.index_name == "NIFTY500",
            IndexMembership.isin == "INE002A01018",
        )
    )
    assert row is not None
    assert row.date_to == DATE2 - dt.timedelta(days=1)


def test_load_reopens_returning_member(db_session: Session) -> None:
    # Load, remove, then re-add.
    load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE1, now=_NOW)
    db_session.commit()
    load_index_membership(db_session, "NIFTY500", fetch=_fetch_shrunk, as_of=DATE2, now=_NOW)
    db_session.commit()
    result = load_index_membership(db_session, "NIFTY500", fetch=_fetch, as_of=DATE2, now=_NOW)
    db_session.commit()
    assert result.added >= 1  # RELIANCE re-added
    # There should now be two rows for RELIANCE: closed + open.
    all_rows = db_session.scalars(
        select(IndexMembership).where(
            IndexMembership.index_name == "NIFTY500",
            IndexMembership.isin == "INE002A01018",
        )
    ).all()
    assert len(all_rows) == 2
    open_rows = [r for r in all_rows if r.date_to is None]
    assert len(open_rows) == 1


def test_parse_skips_nse_placeholder_rows() -> None:
    text = NIFTY500_CSV.rstrip("\n") + "\nDUMMY HEG,Capital Goods,DUMMYHEG,EQ,DUM545A01024\n"
    rows = parse_index_csv(text, "NIFTY500")
    assert len(rows) == 10
    assert "DUM545A01024" not in {r.isin for r in rows}
