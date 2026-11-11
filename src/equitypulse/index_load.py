"""Load index membership into `index_membership`.

A monthly refresh:
 - New ISINs → open row (date_to=NULL).
 - ISINs present before but absent now → close the open row (date_to = refresh_date - 1).
 - ISINs still present → no-op.

ISIN FK constraint: calls ensure_security() for any ISIN not yet in `securities`.
"""

import datetime as dt
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import IndexMembership
from equitypulse.master import ensure_security
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.indices import SOURCE, IndexConstituent, fetch_index_csv, parse_index_csv


class LoadIndexResult(BaseModel):
    index_name: str
    added: int = 0
    removed: int = 0
    unchanged: int = 0


def load_index_membership(
    session: Session,
    index_name: str,
    *,
    fetch: Callable[[str], str] = fetch_index_csv,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
) -> LoadIndexResult:
    """Refresh one index's membership for `as_of`.

    Idempotent: re-running on the same date closes no extra rows and opens no duplicates.
    Does not commit; the caller owns the transaction.
    """
    if as_of is None:
        as_of = today_ist(now)

    text = fetch(index_name)
    constituents = parse_index_csv(text, index_name)

    store_snapshot(
        session,
        source="NIFTYINDICES",
        endpoint=f"index-{index_name.lower()}",
        entity=index_name,
        as_of=as_of,
        payload={"index": index_name, "count": len(constituents)},
        fetched_at=now,
    )

    incoming: set[str] = {c.isin for c in constituents}
    by_isin: dict[str, IndexConstituent] = {c.isin: c for c in constituents}

    # Fetch currently-open rows for this index.
    open_rows: dict[str, IndexMembership] = {
        row.isin: row
        for row in session.scalars(
            select(IndexMembership).where(
                IndexMembership.index_name == index_name,
                IndexMembership.date_to.is_(None),
            )
        )
    }

    result = LoadIndexResult(index_name=index_name)

    for isin in incoming:
        if isin not in open_rows:
            constituent = by_isin[isin]
            ensure_security(
                session, isin, constituent.symbol, source=SOURCE, as_of=as_of,
                name=constituent.name,
            )
            session.add(
                IndexMembership(
                    index_name=index_name,
                    isin=isin,
                    date_from=as_of,
                    date_to=None,
                    source=SOURCE,
                )
            )
            result.added += 1
        else:
            result.unchanged += 1

    for isin, row in open_rows.items():
        if isin not in incoming:
            if row.date_from == as_of:
                # Opened on the same date it's being removed: shouldn't happen in production,
                # but handle gracefully by closing on the same day.
                row.date_to = as_of
            else:
                row.date_to = as_of - dt.timedelta(days=1)
            result.removed += 1

    session.flush()
    return result
