"""Daily prices: load bhavcopy data into prices_daily.

Primary path (NSE): joins two NSE files per trading date:
  - UDiFF bhavcopy (ISIN + prices, no delivery data)
  - sec_bhavdata_full (SYMBOL + delivery %, no ISIN)

Fallback (Tapetide): if the NSE fetch fails, `get_price_history` per symbol is used.
  Raw prices come from `raw_close`; source is set to TAPETIDE_BHAVCOPY.
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
from equitypulse.master import ensure_security, symbol_as_of
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
_MAX_CONSECUTIVE_FAILURES = 3


class LoadPricesResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    degraded: bool = False  # True when Tapetide fallback was used


def _bar_to_values(bar: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Tapetide bar into raw (unadjusted) price fields, or None if it can't be trusted.

    Tapetide's open/high/low/close are split/bonus-adjusted; `raw_close` is the raw print.
    `prices_daily` is raw, so close = raw_close and open/high/low are rescaled by
    raw_close / close (the adjustment factor is constant within a day). A null `raw_close`
    means no raw print exists: the bar is skipped, never filled with the adjusted close.
    `delivery_pct`, when present, is a percentage (0..100) and is stored as a fraction.
    """
    try:
        raw_close = bar.get("raw_close")
        adj_close = bar.get("close")
        if raw_close is None or not adj_close:
            return None
        scale = float(raw_close) / float(adj_close)
        values: dict[str, Any] = {
            "open": float(bar["open"]) * scale,
            "high": float(bar["high"]) * scale,
            "low": float(bar["low"]) * scale,
            "close": float(raw_close),
            "volume": int(bar["volume"]),
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    pct = bar.get("delivery_pct")
    values["delivery_pct"] = (
        float(pct) / 100.0 if isinstance(pct, int | float) and 0 <= pct <= 100 else None
    )
    return values


def _load_from_tapetide(
    session: Session,
    date: dt.date,
    *,
    tapetide_caller: Any,
    now: dt.datetime | None,
    universe: set[str] | None = None,
) -> LoadPricesResult:
    """Fallback: one budgeted `get_price_history` call per symbol for `date`.

    Only fills gaps: a row already present (e.g. from NSE) is never overwritten. Stops at the
    first budget refusal or after repeated failures; the result is always `degraded`.
    """
    from equitypulse.sources.tapetide import BudgetExceededError, get_price_history

    have = {row.isin for row in session.scalars(select(PriceDaily).where(PriceDaily.date == date))}
    securities = [
        sec
        for sec in session.scalars(select(Security).where(Security.status == "ACTIVE"))
        if sec.isin not in have and (universe is None or sec.isin in universe)
    ]
    result = LoadPricesResult(degraded=True)
    if not securities:
        log.warning("Tapetide fallback: nothing to fetch for %s", date)
        return result

    failures = 0
    for sec in securities:
        symbol = symbol_as_of(session, sec.isin, date)
        if symbol is None:
            continue
        kw: dict[str, Any] = {"now": now}
        if tapetide_caller is not None:
            kw["_mcp_caller"] = tapetide_caller
        try:
            payload = get_price_history(session, symbol, days=1, end_date=date, **kw)
        except BudgetExceededError:
            log.warning("Tapetide fallback: budget exhausted at %s", symbol)
            break
        except Exception as exc:
            log.warning("Tapetide fallback: get_price_history failed for %s: %s", symbol, exc)
            failures += 1
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                break
            continue
        failures = 0

        bar = next(
            (b for b in payload.get("data") or [] if _parse_date(b.get("date")) == date), None
        )
        values = _bar_to_values(bar) if bar is not None else None
        if values is None:
            continue
        session.add(
            PriceDaily(
                isin=sec.isin,
                date=date,
                prev_close=None,
                traded_value=None,
                delivery_qty=None,
                series="EQ",
                source=SOURCE_TAPETIDE,
                **values,
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
    allow_fallback: bool = True,
    universe: set[str] | None = None,
) -> LoadPricesResult:
    """Fetch, parse and upsert daily prices for `date`.

    If the NSE fetch fails and `tapetide_caller` is provided (or the real MCP is available),
    falls back to Tapetide. Result carries `degraded=True` in that case.

    `allow_fallback=False` re-raises the NSE error instead (bulk backfills must not spend the
    Tapetide budget). `universe`, if given, restricts the load to those ISINs.

    Does not commit; the caller owns the transaction.
    """
    try:
        udiff_text = fetch_udiff(date)
        delivery_text = fetch_delivery(date)
    except NseFetchError as exc:
        if not allow_fallback:
            raise
        log.warning("NSE bhavcopy fetch failed for %s: %s — trying Tapetide fallback", date, exc)
        return _load_from_tapetide(
            session, date, tapetide_caller=tapetide_caller, now=now, universe=universe
        )

    udiff_rows = parse_bhavcopy_udiff(udiff_text)
    if universe is not None:
        udiff_rows = [r for r in udiff_rows if r.isin in universe]
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
            changed = (
                price_row.close != urow.close
                or price_row.volume != urow.volume
                or price_row.source != SOURCE_NSE
            )
            if changed:
                price_row.source = SOURCE_NSE
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
