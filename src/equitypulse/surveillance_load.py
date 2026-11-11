"""Load surveillance lists (ASM/GSM/ESM) into the `surveillance` table.

The source file is keyed by symbol, so symbols are mapped to ISINs through `securities`.
Symbols with no securities row (mostly SME scrips) are counted, not stored: the table needs
an ISIN and guessing one would be worse than a gap.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import SURVEILLANCE_LISTS, Security, Surveillance
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.surveillance import SOURCE, fetch_surveillance, parse_surveillance


class LoadSurveillanceResult(BaseModel):
    list_type: str
    inserted: int = 0
    already_present: int = 0
    unmapped_symbols: int = 0


def _isin_by_symbol(session: Session) -> dict[str, str]:
    """Symbol -> ISIN. ACTIVE securities win over delisted ones; ambiguous symbols are dropped."""
    active: dict[str, set[str]] = {}
    other: dict[str, set[str]] = {}
    for sec in session.scalars(select(Security)):
        (active if sec.status == "ACTIVE" else other).setdefault(sec.symbol, set()).add(sec.isin)
    merged = {**other, **active}
    return {sym: next(iter(isins)) for sym, isins in merged.items() if len(isins) == 1}


def load_surveillance(
    session: Session,
    list_type: str,
    *,
    fetch: Callable[[str, dt.date], list[Any]] = fetch_surveillance,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
) -> LoadSurveillanceResult:
    """Fetch and upsert one surveillance list for `as_of` (default: today_ist).

    Idempotent: re-running for the same (as_of, list_type) writes 0 new rows.
    Does not commit; the caller owns the transaction.
    """
    if list_type not in SURVEILLANCE_LISTS:
        raise ValueError(f"Unknown surveillance list type: {list_type!r}")
    if as_of is None:
        as_of = today_ist(now)

    raw = fetch(list_type, as_of)
    entries = parse_surveillance(raw, list_type)

    store_snapshot(
        session,
        source="NSE",
        endpoint="reg1-ind",
        entity=as_of.isoformat(),
        as_of=as_of,
        payload=raw,
        fetched_at=now,
    )

    isin_for = _isin_by_symbol(session)
    existing: set[str] = {
        row.isin
        for row in session.scalars(
            select(Surveillance).where(
                Surveillance.as_of == as_of, Surveillance.list_type == list_type
            )
        )
    }

    result = LoadSurveillanceResult(list_type=list_type)
    for entry in entries:
        isin = isin_for.get(entry.symbol)
        if isin is None:
            result.unmapped_symbols += 1
        elif isin in existing:
            result.already_present += 1
        else:
            session.add(
                Surveillance(
                    as_of=as_of, isin=isin, list_type=list_type, stage=entry.stage, source=SOURCE
                )
            )
            existing.add(isin)
            result.inserted += 1

    session.flush()
    return result
