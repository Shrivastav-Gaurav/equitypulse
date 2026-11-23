"""T17: NSE symbol-change list -> symbol_history, and the ACUTAAS link it unlocks."""

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import IsinLink, PriceDaily, Security, SymbolHistory, TradingCalendar
from equitypulse.isin_links import ChainUniverse, detect_links
from equitypulse.master import symbol_as_of
from equitypulse.sources.nse import NseFormatError
from equitypulse.sources.nse_actions import parse_symbol_changes
from equitypulse.symbol_changes import load_symbol_changes
from tests.conftest import FIXTURES

TEXT = (FIXTURES / "nse" / "symbolchange_sample.csv").read_text(encoding="utf-8")
NEW, OLD = "INE00FF01025", "INE00FF01017"  # ACUTAAS now; AMIORG before 2025-04-25
HEADER = "NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"


def _sec(session, isin, symbol, listed=dt.date(2021, 9, 14), status="ACTIVE"):
    session.add(Security(isin=isin, symbol=symbol, name=symbol, status=status, source="T",
                         as_of=dt.date(2026, 11, 11), listing_date=listed))
    session.flush()


def _load(session, text=TEXT):
    return load_symbol_changes(session, fetch=lambda: text, min_rows=1)


def test_parse_fixture() -> None:
    changes = parse_symbol_changes(TEXT, min_rows=1)
    acutaas = next(c for c in changes if c.new_symbol == "ACUTAAS")
    assert (acutaas.old_symbol, acutaas.effective) == ("AMIORG", dt.date(2025, 6, 2))


def test_parse_refuses_a_short_file() -> None:
    with pytest.raises(NseFormatError, match="short"):
        parse_symbol_changes(TEXT)


def test_past_symbol_is_added_to_history(db_session: Session) -> None:
    _sec(db_session, NEW, "ACUTAAS")
    result = _load(db_session)
    row = db_session.scalar(select(SymbolHistory).where(SymbolHistory.isin == NEW))
    assert (row.symbol, row.valid_from, row.valid_to) == (
        "AMIORG", dt.date(2021, 9, 14), dt.date(2025, 6, 1)
    )
    assert result.inserted == 1
    assert symbol_as_of(db_session, NEW, dt.date(2025, 5, 1)) == "AMIORG"
    assert symbol_as_of(db_session, NEW, dt.date(2025, 7, 1)) == "ACUTAAS"
    assert _load(db_session).inserted == 0  # idempotent


def test_chained_renames_walk_back(db_session: Session) -> None:
    _sec(db_session, "INE0CHN01011", "CCC", listed=dt.date(2010, 1, 1))
    text = HEADER + "X Ltd,AAA,BBB,01-JAN-2015\nX Ltd,BBB,CCC,01-JAN-2020\n"
    _load(db_session, text)
    rows = db_session.scalars(
        select(SymbolHistory).where(SymbolHistory.isin == "INE0CHN01011")
        .order_by(SymbolHistory.valid_from)
    ).all()
    assert [(r.symbol, r.valid_from, r.valid_to) for r in rows] == [
        ("AAA", dt.date(2010, 1, 1), dt.date(2014, 12, 31)),
        ("BBB", dt.date(2015, 1, 1), dt.date(2019, 12, 31)),
    ]


def test_rename_before_listing_belongs_to_someone_else(db_session: Session) -> None:
    _sec(db_session, "INE0RCY01011", "NEWCO", listed=dt.date(2022, 1, 1))
    _load(db_session, HEADER + "Old Co,OLDCO,NEWCO,01-JAN-2018\n")  # symbol recycled
    assert db_session.scalar(select(SymbolHistory)) is None


def test_placeholder_without_listing_date_gets_no_history(db_session: Session) -> None:
    _sec(db_session, OLD, "ACUTAAS", listed=None, status="UNKNOWN_OR_DELISTED")
    _load(db_session)
    assert db_session.scalar(select(SymbolHistory)) is None


def test_symbol_history_unlocks_the_acutaas_isin_link(db_session: Session) -> None:
    """The ISIN changed on 2025-04-25 while the stock still traded as AMIORG."""
    d0 = dt.date(2025, 4, 21)
    for i in range(14):
        d = d0 + dt.timedelta(days=i)
        db_session.add(TradingCalendar(date=d, is_trading_day=d.weekday() < 5, source="T"))
    _sec(db_session, NEW, "ACUTAAS")
    _sec(db_session, OLD, "AMIORG", listed=None, status="UNKNOWN_OR_DELISTED")
    for isin, days in ((OLD, (21, 22, 23, 24)), (NEW, (25, 28, 29, 30))):
        for day in days:
            db_session.add(PriceDaily(isin=isin, date=dt.date(2025, 4, day), open=1, high=1,
                                      low=1, close=1, volume=1, series="EQ", source="T"))
    db_session.flush()
    assert detect_links(db_session) == 0  # before: no common symbol
    _load(db_session)
    assert detect_links(db_session) == 1
    link = db_session.scalar(select(IsinLink))
    assert (link.old_isin, link.new_isin, link.effective_date) == (OLD, NEW, dt.date(2025, 4, 25))


def test_chain_universe_follows_past_symbols(db_session: Session) -> None:
    _sec(db_session, NEW, "ACUTAAS")
    _load(db_session)
    u = ChainUniverse.following_chains(db_session, {NEW})
    assert u.keeps(OLD, "AMIORG")
