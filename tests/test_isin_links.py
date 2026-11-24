"""ISIN succession: link rule, chain-aware universe, chain lookup."""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import IsinLink, PriceDaily, Security, TradingCalendar
from equitypulse.isin_links import ChainUniverse, chain_of, detect_links, issuer_code

OLD, NEW = "INE237A01028", "INE237A01036"  # KOTAKBANK before/after its 2026-01-14 change
MON = dt.date(2026, 1, 5)


def _calendar(session: Session) -> None:
    for i in range(60):
        d = MON + dt.timedelta(days=i)
        session.add(TradingCalendar(date=d, is_trading_day=d.weekday() < 5, source="TEST"))


def _sec(session: Session, isin: str, symbol: str) -> None:
    session.add(Security(isin=isin, symbol=symbol, name=symbol, status="ACTIVE", source="T",
                         as_of=MON))


def _prices(session: Session, isin: str, start: dt.date, end: dt.date) -> None:
    d = start
    while d <= end:
        if d.weekday() < 5:
            session.add(PriceDaily(isin=isin, date=d, open=1, high=1, low=1, close=1, volume=1,
                                   series="EQ", source="NSE_BHAVCOPY"))
        d += dt.timedelta(days=1)


def _setup(session: Session, *, new_isin: str = NEW, new_symbol: str = "KOTAKBANK",
           old_end: dt.date = dt.date(2026, 1, 13), new_start: dt.date = dt.date(2026, 1, 14)):
    _calendar(session)
    _sec(session, OLD, "KOTAKBANK")
    _sec(session, new_isin, new_symbol)
    session.flush()  # parents first: no ORM relationship orders the inserts
    _prices(session, OLD, MON, old_end)
    _prices(session, new_isin, new_start, dt.date(2026, 2, 6))
    session.flush()


def test_issuer_code_is_isin_characters_3_to_7() -> None:
    assert issuer_code(OLD) == issuer_code(NEW) == "E237A"


def test_consecutive_isins_with_same_symbol_and_issuer_are_linked(db_session: Session) -> None:
    _setup(db_session)
    assert detect_links(db_session) == 1
    link = db_session.scalar(select(IsinLink))
    assert (link.old_isin, link.new_isin, link.effective_date) == (OLD, NEW, dt.date(2026, 1, 14))
    assert detect_links(db_session) == 0  # idempotent


def test_recycled_symbol_from_another_issuer_is_not_linked(db_session: Session) -> None:
    _setup(db_session, new_isin="INE999Z01011")
    assert detect_links(db_session) == 0


def test_different_symbol_is_not_linked(db_session: Session) -> None:
    _setup(db_session, new_symbol="KOTAKNEW")
    assert detect_links(db_session) == 0


def test_overlapping_trading_is_not_a_succession(db_session: Session) -> None:
    _setup(db_session, old_end=dt.date(2026, 1, 20))
    assert detect_links(db_session) == 0


def test_long_gap_is_not_linked(db_session: Session) -> None:
    _setup(db_session, old_end=dt.date(2026, 1, 13), new_start=dt.date(2026, 1, 26))  # 7 days
    assert detect_links(db_session) == 0


def test_short_suspension_gap_is_linked(db_session: Session) -> None:
    _setup(db_session, old_end=dt.date(2026, 1, 13), new_start=dt.date(2026, 1, 19))  # 3 days
    assert detect_links(db_session) == 1


def test_chain_of_returns_oldest_first_from_any_member(db_session: Session) -> None:
    _setup(db_session)
    detect_links(db_session)
    assert chain_of(db_session, NEW) == [OLD, NEW]
    assert chain_of(db_session, OLD) == [OLD, NEW]
    assert chain_of(db_session, "INE002A01018") == ["INE002A01018"]


def test_chain_universe_keeps_a_members_earlier_isin(db_session: Session) -> None:
    _sec(db_session, NEW, "KOTAKBANK")
    db_session.flush()
    u = ChainUniverse.following_chains(db_session, {NEW})
    assert u.keeps(NEW, "KOTAKBANK")
    assert u.keeps(OLD, "KOTAKBANK")  # unknown ISIN, same symbol + issuer
    assert not u.keeps("INE999Z01011", "KOTAKBANK")  # recycled symbol, other issuer
    assert not u.keeps("INE237A01044", "OTHERSYM")


def test_strict_universe_keeps_only_listed_isins() -> None:
    u = ChainUniverse.strict({NEW})
    assert u.keeps(NEW, "KOTAKBANK")
    assert not u.keeps(OLD, "KOTAKBANK")


def test_chain_universe_keeps_a_linked_isin_under_any_symbol(db_session: Session) -> None:
    """PCBL: its old ISIN traded as PHILIPCARB until 2022-01-12, then as PCBL until the split."""
    old, new = "INE602A01023", "INE602A01031"
    _sec(db_session, old, "PCBL")
    _sec(db_session, new, "PCBL")
    db_session.flush()
    db_session.add(IsinLink(old_isin=old, new_isin=new, effective_date=dt.date(2022, 4, 12),
                            reason="ISIN_CHANGE", source="NSE_BHAVCOPY"))
    db_session.flush()
    u = ChainUniverse.following_chains(db_session, {new})
    assert u.keeps(old, "PHILIPCARB")  # by ISIN: the symbol no longer matters
    assert not u.keeps("INE602A01049", "PHILIPCARB")  # an unlinked ISIN still needs the symbol
