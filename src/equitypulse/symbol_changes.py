"""Fill `symbol_history` with the symbols a security held before its renames.

The equity master only knows today's symbol. NSE's symbol-change list says when each rename
happened, so for every security we walk back from its current symbol: the change that produced
it gives the previous symbol and its last day; the change before that gives the one before.

Guards against recycled symbols: a change is applied only if it is older than every later step
of the same walk and after the security's own listing date. Securities with no listing date
(placeholders created from a bhavcopy row) get no history: nothing proves which company an
older use of their symbol belonged to.
"""

import datetime as dt
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import Security, SymbolHistory
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse_actions import (
    SOURCE_SYMBOLS,
    SymbolChange,
    fetch_symbol_changes,
    parse_symbol_changes,
)


class LoadSymbolChangesResult(BaseModel):
    changes_in_file: int = 0
    inserted: int = 0
    already_present: int = 0


def load_symbol_changes(
    session: Session,
    *,
    fetch: Callable[[], str] = fetch_symbol_changes,
    min_rows: int = 100,
    now: dt.datetime | None = None,
) -> LoadSymbolChangesResult:
    """Insert past-symbol rows; idempotent. Does not commit."""
    text = fetch()
    changes = parse_symbol_changes(text, min_rows=min_rows)
    store_snapshot(
        session, source="NSE", endpoint="symbolchange.csv", entity="",
        as_of=today_ist(now), payload=text, fetched_at=now,
    )
    by_new: dict[str, list[SymbolChange]] = {}
    for c in changes:
        by_new.setdefault(c.new_symbol, []).append(c)

    existing = {
        (isin, start)
        for isin, start in session.execute(select(SymbolHistory.isin, SymbolHistory.valid_from))
    }
    result = LoadSymbolChangesResult(changes_in_file=len(changes))
    for sec in session.scalars(select(Security).where(Security.listing_date.is_not(None))):
        for row in _past_symbols(sec, by_new):
            if (row.isin, row.valid_from) in existing:
                result.already_present += 1
                continue
            session.add(row)
            existing.add((row.isin, row.valid_from))
            result.inserted += 1
    session.flush()
    return result


def _past_symbols(sec: Security, by_new: dict[str, list[SymbolChange]]) -> list[SymbolHistory]:
    listed = sec.listing_date
    assert listed is not None
    steps: list[SymbolChange] = []
    symbol, before = sec.symbol, dt.date.max
    seen = {symbol}
    while True:
        candidates = [
            c for c in by_new.get(symbol, []) if listed < c.effective < before
        ]
        if not candidates:
            break
        step = max(candidates, key=lambda c: c.effective)  # the most recent rename into it
        if step.old_symbol in seen:
            break  # a loop in the file; stop rather than invent history
        steps.append(step)
        seen.add(step.old_symbol)
        symbol, before = step.old_symbol, step.effective

    rows = []
    for i, step in enumerate(steps):
        older = steps[i + 1] if i + 1 < len(steps) else None
        valid_from = older.effective if older else listed
        rows.append(
            SymbolHistory(
                isin=sec.isin,
                symbol=step.old_symbol,
                valid_from=valid_from,
                valid_to=step.effective - dt.timedelta(days=1),
                source=SOURCE_SYMBOLS,
            )
        )
    return rows
