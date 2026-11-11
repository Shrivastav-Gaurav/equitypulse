"""Daily prices: load bhavcopy data into prices_daily.

Joins two NSE files for each trading date:
  - UDiFF bhavcopy (ISIN + prices, no delivery data)
  - sec_bhavdata_full (SYMBOL + delivery %, no ISIN)

For each row, calls `ensure_security` if the ISIN is not yet in `securities`.
"""

import datetime as dt
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import PriceDaily
from equitypulse.master import ensure_security
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse import (
    fetch_bhavcopy_delivery,
    fetch_bhavcopy_udiff,
    parse_bhavcopy_delivery,
    parse_bhavcopy_udiff,
)

SOURCE = "NSE_BHAVCOPY"


class LoadPricesResult(BaseModel):
    inserted: int = 0
    updated: int = 0


def load_bhavcopy(
    session: Session,
    date: dt.date,
    *,
    fetch_udiff: Callable[[dt.date], str] = fetch_bhavcopy_udiff,
    fetch_delivery: Callable[[dt.date], str] = fetch_bhavcopy_delivery,
    now: dt.datetime | None = None,
) -> LoadPricesResult:
    """Fetch, parse and upsert daily prices for `date`.

    Does not commit; the caller owns the transaction.
    """
    udiff_text = fetch_udiff(date)
    delivery_text = fetch_delivery(date)
    udiff_rows = parse_bhavcopy_udiff(udiff_text)
    delivery = parse_bhavcopy_delivery(delivery_text)

    store_snapshot(
        session,
        source="NSE",
        endpoint="bhavcopy-udiff",
        entity=date.isoformat(),
        as_of=date,
        payload={"date": date.isoformat(), "rows": len(udiff_rows)},
        fetched_at=now,
    )

    existing: set[str] = {
        row.isin
        for row in session.scalars(
            select(PriceDaily).where(PriceDaily.date == date)
        )
    }

    result = LoadPricesResult()
    as_of = today_ist(now)

    for urow in udiff_rows:
        ensure_security(
            session,
            urow.isin,
            urow.symbol,
            source=SOURCE,
            as_of=as_of,
        )
        deliv = delivery.get(urow.symbol)
        if urow.isin in existing:
            # Check if anything changed (idempotent rerun).
            price_row = session.get(PriceDaily, (urow.isin, date))
            if price_row is None:
                continue
            changed = (
                price_row.close != urow.close
                or price_row.volume != urow.volume
            )
            if changed:
                price_row.open = urow.open
                price_row.high = urow.high
                price_row.low = urow.low
                price_row.close = urow.close
                price_row.prev_close = urow.prev_close
                price_row.volume = urow.volume
                price_row.traded_value = urow.traded_value
                price_row.delivery_qty = deliv.delivery_qty if deliv else None
                price_row.delivery_pct = deliv.delivery_pct if deliv else None
                result.updated += 1
        else:
            session.add(
                PriceDaily(
                    isin=urow.isin,
                    date=date,
                    open=urow.open,
                    high=urow.high,
                    low=urow.low,
                    close=urow.close,
                    prev_close=urow.prev_close,
                    volume=urow.volume,
                    traded_value=urow.traded_value,
                    delivery_qty=deliv.delivery_qty if deliv else None,
                    delivery_pct=deliv.delivery_pct if deliv else None,
                    series=urow.series,
                    source=SOURCE,
                )
            )
            result.inserted += 1

    session.flush()
    return result
