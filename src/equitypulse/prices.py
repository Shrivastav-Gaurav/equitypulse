"""Daily prices: load bhavcopy data into prices_daily.

Primary path (NSE): joins two NSE files per trading date:
  - UDiFF bhavcopy (ISIN + prices, no delivery data)
  - sec_bhavdata_full (SYMBOL + delivery %, no ISIN)

Fallback (Tapetide): if the NSE fetch fails, `get_price_history` per ISIN is used.
  Delivery data is unavailable in this path; source is set to TAPETIDE_BHAVCOPY.
  The result carries `degraded=True` so the caller can mark the audit row DEGRADED.

For each row, calls `ensure_security` if the ISIN is not yet in `securities`.
"""

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import PriceDaily, Security
from equitypulse.master import ensure_security
from equitypulse.snapshots import store_snapshot
from equitypulse.sources.nse import (
    NseFetchError,
    fetch_bhavcopy_delivery,
    fetch_bhavcopy_udiff,
    parse_bhavcopy_delivery,
    parse_bhavcopy_udiff,
)

SOURCE_NSE = "NSE_BHAVCOPY"
SOURCE_TAPETIDE = "TAPETIDE_BHAVCOPY"
log = logging.getLogger(__name__)


class LoadPricesResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    degraded: bool = False  # True when Tapetide fallback was used


def _load_from_tapetide(
    session: Session,
    date: dt.date,
    *,
    tapetide_caller: Any,
    now: dt.datetime | None,
) -> LoadPricesResult:
    """Fallback: fetch prices via Tapetide get_price_history for all known ACTIVE securities."""
    from equitypulse.sources.tapetide import get_price_history

    securities = session.scalars(
        select(Security).where(Security.status == "ACTIVE")
    ).all()
    if not securities:
        log.warning("Tapetide fallback: no ACTIVE securities to fetch prices for %s", date)
        return LoadPricesResult(degraded=True)

    result = LoadPricesResult(degraded=True)
    existing: set[str] = {
        row.isin for row in session.scalars(select(PriceDaily).where(PriceDaily.date == date))
    }

    for sec in securities:
        try:
            payload = get_price_history(
                session, sec.isin, as_of=date,
                now=now, _mcp_caller=tapetide_caller,
            )
        except Exception as exc:
            log.warning("Tapetide fallback: get_price_history failed for %s: %s", sec.isin, exc)
            continue

        # Tapetide returns a list of daily bars; find the one for `date`.
        bars = payload.get("data") or payload.get("prices") or []
        bar = next(
            (b for b in bars if _parse_date(b.get("date")) == date), None
        )
        if bar is None:
            continue

        try:
            close = float(bar["close"])
            volume = int(bar.get("volume", 0))
            open_ = float(bar.get("open", close))
            high = float(bar.get("high", close))
            low = float(bar.get("low", close))
            prev_close = float(bar["prev_close"]) if bar.get("prev_close") else None
            traded_value = float(bar["traded_value"]) if bar.get("traded_value") else None
        except (KeyError, TypeError, ValueError):
            continue

        if sec.isin in existing:
            price_row = session.get(PriceDaily, (sec.isin, date))
            if price_row and (price_row.close != close or price_row.volume != volume):
                price_row.close = close
                price_row.open = open_
                price_row.high = high
                price_row.low = low
                price_row.volume = volume
                price_row.traded_value = traded_value
                price_row.source = SOURCE_TAPETIDE
                result.updated += 1
        else:
            session.add(
                PriceDaily(
                    isin=sec.isin,
                    date=date,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    prev_close=prev_close,
                    volume=volume,
                    traded_value=traded_value,
                    delivery_qty=None,
                    delivery_pct=None,
                    series="EQ",
                    source=SOURCE_TAPETIDE,
                )
            )
            result.inserted += 1

    session.flush()
    return result


def _parse_date(val: Any) -> dt.date | None:
    if val is None:
        return None
    try:
        return dt.date.fromisoformat(str(val))
    except ValueError:
        return None


def load_bhavcopy(
    session: Session,
    date: dt.date,
    *,
    fetch_udiff: Callable[[dt.date], str] = fetch_bhavcopy_udiff,
    fetch_delivery: Callable[[dt.date], str] = fetch_bhavcopy_delivery,
    now: dt.datetime | None = None,
    tapetide_caller: Any = None,  # injectable for tests; None = use real MCP
) -> LoadPricesResult:
    """Fetch, parse and upsert daily prices for `date`.

    If the NSE fetch fails and `tapetide_caller` is provided (or the real MCP is available),
    falls back to Tapetide. Result carries `degraded=True` in that case.

    Does not commit; the caller owns the transaction.
    """
    try:
        udiff_text = fetch_udiff(date)
        delivery_text = fetch_delivery(date)
    except NseFetchError as exc:
        log.warning("NSE bhavcopy fetch failed for %s: %s — trying Tapetide fallback", date, exc)
        return _load_from_tapetide(
            session, date, tapetide_caller=tapetide_caller, now=now
        )

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
            source=SOURCE_NSE,
            as_of=as_of,
        )
        deliv = delivery.get(urow.symbol)
        if urow.isin in existing:
            price_row = session.get(PriceDaily, (urow.isin, date))
            if price_row is None:
                continue
            changed = price_row.close != urow.close or price_row.volume != urow.volume
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
                    source=SOURCE_NSE,
                )
            )
            result.inserted += 1

    session.flush()
    return result
