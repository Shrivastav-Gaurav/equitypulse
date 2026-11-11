"""Equity master: load and maintain `securities` and `symbol_history` from NSE EQUITY_L.csv.

Rules:
- New ISIN → insert ACTIVE security + open symbol_history row.
- Known ISIN, symbol changed → close old history row (valid_to = as_of - 1 day), open new one;
  update securities.symbol. If the existing history row already has valid_from == as_of, update
  it in place instead (idempotent re-run on the same date).
- ISIN in DB but absent from list → set status UNKNOWN_OR_DELISTED.
- ISIN absent then reappears → set status back to ACTIVE; reopen history.
- Truncation guard: if the incoming list covers < 90% of currently ACTIVE securities, refuse
  with NseFormatError (the file is likely a partial download).
"""

import datetime as dt
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import Security, SymbolHistory
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse import NseFormatError, fetch_equity_list, parse_equity_list

SOURCE = "NSE_EQUITY_L"
_TRUNCATION_THRESHOLD = 0.90  # refuse if incoming < 90% of currently-active rows


def symbol_as_of(session: Session, isin: str, date: dt.date) -> str | None:
    """The NSE symbol `isin` traded under on `date` (symbols change; ISINs rarely do).

    Uses symbol_history; falls back to the current symbol when no history row covers `date`.
    """
    hist = session.scalar(
        select(SymbolHistory.symbol)
        .where(
            SymbolHistory.isin == isin,
            SymbolHistory.valid_from <= date,
            (SymbolHistory.valid_to.is_(None)) | (SymbolHistory.valid_to >= date),
        )
        .order_by(SymbolHistory.valid_from.desc())
        .limit(1)
    )
    if hist is not None:
        return hist
    sec = session.get(Security, isin)
    return sec.symbol if sec is not None else None


class LoadResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    renamed: int = 0
    deactivated: int = 0
    reactivated: int = 0


def load_equity_master(
    session: Session,
    *,
    fetch: Callable[[], str] = fetch_equity_list,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
) -> LoadResult:
    """Fetch EQUITY_L.csv, store a snapshot, and upsert securities/symbol_history.

    Does not commit; the caller owns the transaction.
    """
    text = fetch()
    rows = parse_equity_list(text)
    if as_of is None:
        as_of = today_ist(now)

    # Truncation guard: compare against currently-active count.
    active_count = session.scalar(
        select(Security).where(Security.status == "ACTIVE").with_only_columns(
            __import__("sqlalchemy").func.count()
        )
    ) or 0
    if active_count > 0 and len(rows) < active_count * _TRUNCATION_THRESHOLD:
        raise NseFormatError(
            f"EQUITY_L.csv looks truncated: {len(rows)} rows vs {active_count} active "
            f"securities (threshold {_TRUNCATION_THRESHOLD:.0%})"
        )

    store_snapshot(
        session,
        source="NSE",
        endpoint="EQUITY_L.csv",
        entity="",
        as_of=as_of,
        payload={"rows": len(rows)},
        fetched_at=now,
    )

    incoming = {r.isin: r for r in rows}
    existing: dict[str, Security] = {
        s.isin: s for s in session.scalars(select(Security))
    }

    result = LoadResult()

    for isin, row in incoming.items():
        sec = existing.get(isin)
        if sec is None:
            session.add(
                Security(
                    isin=isin,
                    symbol=row.symbol,
                    name=row.name,
                    series=row.series,
                    listing_date=row.listing_date,
                    status="ACTIVE",
                    source=SOURCE,
                    as_of=as_of,
                )
            )
            session.add(
                SymbolHistory(
                    isin=isin,
                    symbol=row.symbol,
                    valid_from=as_of,
                    valid_to=None,
                    source=SOURCE,
                )
            )
            result.inserted += 1
        else:
            changed = False
            if sec.status == "UNKNOWN_OR_DELISTED":
                sec.status = "ACTIVE"
                sec.source = SOURCE
                sec.as_of = as_of
                result.reactivated += 1
                changed = True

            if sec.symbol != row.symbol:
                # Close the current open history row.
                open_hist = session.scalar(
                    select(SymbolHistory).where(
                        SymbolHistory.isin == isin, SymbolHistory.valid_to.is_(None)
                    )
                )
                if open_hist is not None:
                    if open_hist.valid_from == as_of:
                        # Same date: update in place (idempotent rerun).
                        open_hist.symbol = row.symbol
                        open_hist.source = SOURCE
                    else:
                        open_hist.valid_to = as_of - dt.timedelta(days=1)
                        session.add(
                            SymbolHistory(
                                isin=isin,
                                symbol=row.symbol,
                                valid_from=as_of,
                                valid_to=None,
                                source=SOURCE,
                            )
                        )
                else:
                    session.add(
                        SymbolHistory(
                            isin=isin,
                            symbol=row.symbol,
                            valid_from=as_of,
                            valid_to=None,
                            source=SOURCE,
                        )
                    )
                sec.symbol = row.symbol
                result.renamed += 1
                changed = True

            # Update non-key fields if anything else changed.
            meta_changed = (
                sec.name != row.name
                or sec.series != row.series
                or sec.listing_date != row.listing_date
            )
            if meta_changed:
                sec.name = row.name
                sec.series = row.series
                sec.listing_date = row.listing_date
                changed = True

            if changed:
                result.updated += 1

    # Deactivate ISINs missing from the list.
    for isin, sec in existing.items():
        if isin not in incoming and sec.status == "ACTIVE":
            sec.status = "UNKNOWN_OR_DELISTED"
            result.deactivated += 1

    session.flush()
    return result


def ensure_security(
    session: Session,
    isin: str,
    symbol: str,
    *,
    source: str,
    as_of: dt.date,
    name: str | None = None,
) -> None:
    """Insert a minimal UNKNOWN_OR_DELISTED security row if the ISIN is not yet in `securities`.

    Used by bhavcopy and other loaders when they encounter an ISIN that the equity master
    hasn't seen yet. Never overwrites an existing row's status or symbol.
    """
    existing = session.get(Security, isin)
    if existing is not None:
        return
    session.add(
        Security(
            isin=isin,
            symbol=symbol,
            name=name or symbol,
            series="",
            status="UNKNOWN_OR_DELISTED",
            source=source,
            as_of=as_of,
        )
    )
    session.flush()
