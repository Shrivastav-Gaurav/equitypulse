"""Load surveillance lists (ASM/GSM/ESM) into the `surveillance` table."""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import SURVEILLANCE_LISTS, Surveillance
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.surveillance import (
    SOURCE,
    fetch_surveillance,
    parse_surveillance,
)


class LoadSurveillanceResult(BaseModel):
    list_type: str
    inserted: int = 0
    already_present: int = 0


def load_surveillance(
    session: Session,
    list_type: str,
    *,
    fetch: Callable[[str], list[Any]] = fetch_surveillance,
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

    raw = fetch(list_type)
    entries = parse_surveillance(raw, list_type)

    store_snapshot(
        session,
        source="NSE",
        endpoint=f"surveillance-{list_type.lower()}",
        entity=list_type,
        as_of=as_of,
        payload=raw,
        fetched_at=now,
    )

    existing: set[str] = {
        row.isin
        for row in session.scalars(
            select(Surveillance).where(
                Surveillance.as_of == as_of,
                Surveillance.list_type == list_type,
            )
        )
    }

    result = LoadSurveillanceResult(list_type=list_type)
    for entry in entries:
        if entry.isin in existing:
            result.already_present += 1
        else:
            session.add(
                Surveillance(
                    as_of=as_of,
                    isin=entry.isin,
                    list_type=list_type,
                    stage=entry.stage,
                    source=SOURCE,
                )
            )
            result.inserted += 1

    session.flush()
    return result
