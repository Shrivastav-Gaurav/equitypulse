"""Load NSE corporate actions into `corporate_actions` (free; replaces Tapetide for factors).

Each action is stored on the ISIN that traded on its ex-date, found among the ISINs that share
the record's issuer code (ISIN characters 3-7) and have held its symbol. NSE's own `isin` field
is not trusted (see sources.nse_actions). An action whose security is not in `securities`
(most of the market: we keep the Nifty 500 and its ISIN chains) is counted, not stored.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import CorporateAction, PriceDaily
from equitypulse.isin_links import issuer_code, symbols_by_isin
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse_actions import (
    SOURCE_ACTIONS,
    ParsedAction,
    fetch_corporate_actions,
    parse_corporate_actions,
)


class LoadNseActionsResult(BaseModel):
    rows_in: int = 0
    inserted: int = 0
    already_present: int = 0
    not_in_universe: int = 0
    skipped_kinds: int = 0  # dividends, interest, buy-backs...
    unparsed: int = 0  # mention a split/bonus in a shape we do not trust: stored as OTHER


def load_nse_corporate_actions(
    session: Session,
    start: dt.date,
    end: dt.date,
    *,
    fetch: Callable[[dt.date, dt.date], list[dict[str, Any]]] = fetch_corporate_actions,
    now: dt.datetime | None = None,
) -> LoadNseActionsResult:
    """Fetch [start, end] one calendar year per request and upsert. Idempotent. No commit."""
    result = LoadNseActionsResult()
    resolver = _Resolver(session)
    existing = {
        (isin, ex, kind)
        for isin, ex, kind in session.execute(
            select(CorporateAction.isin, CorporateAction.ex_date, CorporateAction.action_type)
        )
    }
    for chunk_start, chunk_end in _year_chunks(start, end):
        rows = fetch(chunk_start, chunk_end)
        store_snapshot(
            session, source="NSE", endpoint="corporates-corporateActions",
            entity=f"{chunk_start}..{chunk_end}", as_of=today_ist(now), payload=rows,
            fetched_at=now,
        )
        actions, skipped = parse_corporate_actions(rows)
        result.rows_in += len(rows)
        result.skipped_kinds += skipped
        for action in actions:
            if action.action_type == "OTHER":
                result.unparsed += 1
            isin = resolver.resolve(action)
            if isin is None:
                result.not_in_universe += 1
                continue
            key = (isin, action.ex_date, action.action_type)
            if key in existing:
                result.already_present += 1
                continue
            session.add(
                CorporateAction(
                    isin=isin, ex_date=action.ex_date, action_type=action.action_type,
                    details=action.subject, adj_factor=action.adj_factor, source=SOURCE_ACTIONS,
                )
            )
            existing.add(key)
            result.inserted += 1
    session.flush()
    return result


class _Resolver:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._candidates: dict[tuple[str, str], list[str]] = {}
        for isin, symbols in symbols_by_isin(session).items():
            for symbol in symbols:
                self._candidates.setdefault((symbol, issuer_code(isin)), []).append(isin)

    def resolve(self, action: ParsedAction) -> str | None:
        isins = self._candidates.get((action.symbol, issuer_code(action.isin)), [])
        if not isins:
            return None
        if len(isins) == 1:
            return isins[0]
        # Several ISINs in the chain: the one that traded on the ex-date (the new one, for a
        # split that also changed the ISIN); else the one that traded most recently before it;
        # with no prices at all, the highest serial (NSE numbers newer ISINs higher).
        on_ex = sorted(
            self._session.scalars(
                select(PriceDaily.isin).where(
                    PriceDaily.isin.in_(isins), PriceDaily.date == action.ex_date
                )
            )
        )
        if on_ex:
            return on_ex[0]
        last_before = self._session.execute(
            select(PriceDaily.isin)
            .where(PriceDaily.isin.in_(isins), PriceDaily.date < action.ex_date)
            .order_by(PriceDaily.date.desc())
            .limit(1)
        ).scalar()
        return last_before or sorted(isins)[-1]


def _year_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    if end < start:
        raise ValueError("end before start")
    chunks = []
    cur = start
    while cur <= end:
        stop = min(dt.date(cur.year, 12, 31), end)
        chunks.append((cur, stop))
        cur = stop + dt.timedelta(days=1)
    return chunks
