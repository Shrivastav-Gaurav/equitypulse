"""ISIN succession: one security, several ISINs over time.

NSE issues a new ISIN for the same security on a face-value split and similar events
(KOTAKBANK INE237A01028 -> INE237A01036 on 2026-01-14). Prices stay keyed by the ISIN they
traded under; `isin_links` records the chain so loaders and readers can follow it.

Rule for a link old -> new (deterministic, no guessing):
  - a common NSE symbol: one each ISIN has held (current symbol or `symbol_history`, which the
    NSE symbol-change list fills; ACUTAAS's new ISIN first traded as AMIORG),
  - same issuer code (ISIN characters 3-7, e.g. "E237A"): a recycled symbol belongs to a
    different issuer and is never linked,
  - old's last price row is before new's first, with at most MAX_GAP_TRADING_DAYS trading
    days between them (the two never trade on the same day).
"""

import datetime as dt
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from equitypulse.db.models import IsinLink, PriceDaily, Security, SymbolHistory, TradingCalendar

SOURCE = "NSE_BHAVCOPY"
REASON = "ISIN_CHANGE"
MAX_GAP_TRADING_DAYS = 5


def issuer_code(isin: str) -> str:
    return isin[2:7]


class ChainUniverse(BaseModel):
    """Which bhavcopy rows a universe-restricted load keeps.

    A row is kept if its ISIN is a member, or if it trades under a member's symbol with the
    member's issuer code: that is how a member's pre-change ISIN gets loaded before anyone
    knows the old ISIN.
    """

    model_config = ConfigDict(frozen=True)

    isins: frozenset[str]
    issuers_by_symbol: dict[str, frozenset[str]] = {}

    def keeps(self, isin: str, symbol: str) -> bool:
        return isin in self.isins or issuer_code(isin) in self.issuers_by_symbol.get(symbol, ())

    @classmethod
    def strict(cls, isins: Iterable[str]) -> "ChainUniverse":
        return cls(isins=frozenset(isins))

    @classmethod
    def following_chains(cls, session: Session, isins: Iterable[str]) -> "ChainUniverse":
        """Members, every ISIN already linked to a member (whatever symbol it traded under:
        PCBL's old ISIN traded as PHILIPCARB), and any ISIN sharing a member's symbol (current
        or past) and issuer code, which is how a link is found in the first place."""
        members = frozenset(isins)
        linked = set(members)
        for isin in members:
            linked.update(chain_of(session, isin))
        by_symbol: dict[str, set[str]] = {}
        for isin, symbol in session.execute(
            select(Security.isin, Security.symbol).where(Security.isin.in_(members))
        ):
            by_symbol.setdefault(symbol, set()).add(issuer_code(isin))
        for isin, symbol in session.execute(
            select(SymbolHistory.isin, SymbolHistory.symbol).where(SymbolHistory.isin.in_(members))
        ):
            by_symbol.setdefault(symbol, set()).add(issuer_code(isin))
        return cls(
            isins=frozenset(linked),
            issuers_by_symbol={s: frozenset(v) for s, v in by_symbol.items()},
        )


def symbols_by_isin(session: Session) -> dict[str, set[str]]:
    """Every symbol each ISIN has held: its current symbol plus its symbol history."""
    held: dict[str, set[str]] = {}
    for isin, symbol in session.execute(select(Security.isin, Security.symbol)):
        held.setdefault(isin, set()).add(symbol)
    for isin, symbol in session.execute(select(SymbolHistory.isin, SymbolHistory.symbol)):
        held.setdefault(isin, set()).add(symbol)
    return held


def detect_links(session: Session) -> int:
    """Insert links found by the rule above; returns how many were added. Idempotent.

    Does not commit; the caller owns the transaction.
    """
    spans = {
        isin: (first, last)
        for isin, first, last in session.execute(
            select(PriceDaily.isin, func.min(PriceDaily.date), func.max(PriceDaily.date))
            .group_by(PriceDaily.isin)
        )
    }
    groups: dict[tuple[str, str], list[str]] = {}
    for isin, symbols in symbols_by_isin(session).items():
        if isin in spans:
            for symbol in symbols:
                groups.setdefault((symbol, issuer_code(isin)), []).append(isin)

    existing = {(o, n) for o, n in session.execute(select(IsinLink.old_isin, IsinLink.new_isin))}
    trading_days = sorted(
        session.scalars(select(TradingCalendar.date).where(TradingCalendar.is_trading_day))
    )
    added = 0
    for isins in groups.values():
        if len(isins) < 2:
            continue
        chain = sorted(isins, key=lambda i: spans[i][0])
        for old, new in zip(chain, chain[1:], strict=False):
            old_last, new_first = spans[old][1], spans[new][0]
            if old_last >= new_first or (old, new) in existing:
                continue
            existing.add((old, new))  # the same pair can sit in two symbol groups
            if _trading_days_between(trading_days, old_last, new_first) > MAX_GAP_TRADING_DAYS:
                continue
            session.add(
                IsinLink(
                    old_isin=old, new_isin=new, effective_date=new_first,
                    reason=REASON, source=SOURCE,
                )
            )
            added += 1
    session.flush()
    return added


def chain_of(session: Session, isin: str) -> list[str]:
    """All ISINs of the security `isin` belongs to, oldest first."""
    back = {new: old for old, new in session.execute(select(IsinLink.old_isin, IsinLink.new_isin))}
    fwd = {old: new for new, old in back.items()}
    first = isin
    while first in back:
        first = back[first]
    out = [first]
    while out[-1] in fwd:
        out.append(fwd[out[-1]])
    return out


def _trading_days_between(days: list[dt.date], a: dt.date, b: dt.date) -> int:
    """Trading days strictly between a and b."""
    return sum(1 for d in days if a < d < b)
