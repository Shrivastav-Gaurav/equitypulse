"""Tapetide MCP client with budget guard, snapshot dedup, and DB cache.

Every call path:
  1. Check the DB cache (snapshots); return cached payload if present for the same as_of.
  2. Check the daily budget (api_usage), count the attempt and commit it; refuse if exhausted.
  3. Call the MCP tool (async, run via asyncio.run); store the raw payload in snapshots.

All Tapetide calls go through `call_tool()` — never call the MCP session directly from
other modules.
"""

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.config import get_settings
from equitypulse.db.models import ApiUsage, Snapshot
from equitypulse.snapshots import canonical_json, store_snapshot

SOURCE = "TAPETIDE"
log = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """The daily Tapetide call budget is exhausted."""


class TapetideError(RuntimeError):
    """The MCP call failed or returned an unexpected response."""


# ── budget guard ──────────────────────────────────────────────────────────────


def _get_or_create_usage(session: Session, date: dt.date) -> ApiUsage:
    row = session.get(ApiUsage, (date, SOURCE))
    if row is None:
        row = ApiUsage(date=date, source=SOURCE, calls=0)
        session.add(row)
        session.flush()
    return row


def _check_budget(
    session: Session, now: dt.datetime | None = None, budget: int | None = None
) -> None:
    """Raise BudgetExceededError if today's call count is at or above the daily budget."""
    if budget is None:
        budget = get_settings().tapetide_daily_budget
    today = today_ist(now)
    usage = _get_or_create_usage(session, today)
    if usage.calls >= budget:
        raise BudgetExceededError(
            f"Tapetide daily budget of {budget} calls reached for {today} "
            f"(used {usage.calls}). Increase TAPETIDE_DAILY_BUDGET or wait until tomorrow."
        )


def _record_call(session: Session, now: dt.datetime | None = None) -> None:
    """Increment today's Tapetide call count by 1."""
    today = today_ist(now)
    usage = _get_or_create_usage(session, today)
    usage.calls += 1


def _reserve_call(
    session: Session, now: dt.datetime | None = None, budget: int | None = None
) -> None:
    """Check the budget, count one attempt, and COMMIT before the request is sent.

    Committing here means a later rollback by the caller can never erase the count. SQLite
    allows one writer, so the count cannot use a separate connection while the caller holds
    pending writes; the caller's pending work is committed together with the count instead.
    """
    _check_budget(session, now, budget=budget)
    _record_call(session, now)
    session.commit()


# ── cache lookup ──────────────────────────────────────────────────────────────


def _cached_payload(
    session: Session, tool: str, entity: str, as_of: dt.date
) -> dict[str, Any] | None:
    """Return the cached payload for (tool, entity, as_of) if one exists, else None."""
    row = session.scalar(
        select(Snapshot)
        .where(
            Snapshot.source == SOURCE,
            Snapshot.endpoint == tool,
            Snapshot.entity == entity,
            Snapshot.as_of == as_of,
        )
        .order_by(Snapshot.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    try:
        return json.loads(row.payload_json)
    except json.JSONDecodeError:
        return None


# ── MCP call ──────────────────────────────────────────────────────────────────


async def _call_mcp_tool(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Open a one-shot MCP session, call `tool`, and return the parsed JSON payload."""
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    settings = get_settings()
    url = settings.tapetide_mcp_url
    token = settings.tapetide_api_token.get_secret_value()

    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http_client, streamable_http_client(url, http_client=http_client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if result.is_error:
                raise TapetideError(f"Tapetide tool {tool!r} returned an error: {result.content}")
            # MCP returns a list of content blocks; we expect one text block with JSON.
            if not result.content:
                raise TapetideError(f"Tapetide tool {tool!r} returned empty content")
            text_block = next(
                (b for b in result.content if getattr(b, "type", None) == "text"), None
            )
            if text_block is None:
                raise TapetideError(
                    f"Tapetide tool {tool!r} returned no text content block"
                )
            try:
                return json.loads(text_block.text)
            except json.JSONDecodeError as exc:
                raise TapetideError(
                    f"Tapetide tool {tool!r} returned non-JSON text: {text_block.text[:200]}"
                ) from exc


# ── retries ───────────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0

# The mcp package ships its own copy of httpx ("httpx2"), so its errors are not httpx's.
_HTTP_MODULES: tuple[Any, ...] = (httpx,)
try:
    import httpx2

    _HTTP_MODULES = (httpx, httpx2)
except ImportError:  # pragma: no cover
    pass
_STATUS_ERRORS = tuple(m.HTTPStatusError for m in _HTTP_MODULES)
_TRANSIENT = (*(m.TransportError for m in _HTTP_MODULES), TimeoutError, ConnectionError)


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten exception groups: anyio task groups wrap the real error."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _is_retryable(exc: BaseException) -> bool:
    """Network errors and 5xx are transient. Any 4xx (bad request, auth, rate limit) or a
    tool-level error is not: retrying would burn budget for the same answer."""
    for leaf in _leaves(exc):
        if isinstance(leaf, _STATUS_ERRORS):
            if leaf.response.status_code < 500:
                return False
        elif not isinstance(leaf, _TRANSIENT):
            return False
    return True


# ── public call_tool ──────────────────────────────────────────────────────────


def call_tool(
    session: Session,
    tool: str,
    arguments: dict[str, Any],
    *,
    entity: str = "",
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
    _mcp_caller: Any = None,  # injectable for tests
    _budget: int | None = None,  # test-only override; requires _mcp_caller
    _sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Call a Tapetide MCP tool, respecting budget and caching.

    - `entity`: logical identifier for the cache key; it must determine `arguments`.
    - `as_of`: the date the data is valid for; defaults to today_ist().
    - `_mcp_caller`: callable (tool, args) → dict, injectable for testing.

    Every attempt, including retries and failures, counts against the daily budget and is
    committed before the request is sent (this also commits the caller's pending work). The
    snapshot of the response is flushed, not committed: the caller commits it.
    """
    if _budget is not None and _mcp_caller is None:
        raise ValueError("_budget override is only allowed together with an injected _mcp_caller")
    if as_of is None:
        as_of = today_ist(now)

    cached = _cached_payload(session, tool, entity, as_of)
    if cached is not None:
        log.debug("Tapetide cache hit: tool=%s entity=%s as_of=%s", tool, entity, as_of)
        return cached

    caller = _mcp_caller or (lambda t, a: asyncio.run(_call_mcp_tool(t, a)))
    payload: dict[str, Any] = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _reserve_call(session, now, budget=_budget)
        try:
            payload = caller(tool, arguments)
            break
        except Exception as exc:
            if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            log.warning("Tapetide %s attempt %d failed (%s); retrying", tool, attempt, exc)
            _sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))

    store_snapshot(
        session,
        source=SOURCE,
        endpoint=tool,
        entity=entity,
        as_of=as_of,
        payload=payload,
        fetched_at=now,
    )
    session.flush()
    return payload


# ── typed wrappers ────────────────────────────────────────────────────────────


def search_stocks(
    session: Session, query: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(session, "search_stocks", {"query": query}, entity=query, as_of=as_of, **kw)


MAX_HISTORY_DAYS = 2000


def get_price_history(
    session: Session,
    symbol: str,
    *,
    days: int,
    end_date: dt.date | None = None,
    interval: str = "daily",
    as_of: dt.date | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """One page of `get_price_history` (NSE `symbol`, not ISIN; `days` are calendar days).

    `close` in the response is split/bonus-adjusted; `raw_close` is the raw print.
    """
    if not 1 <= days <= MAX_HISTORY_DAYS:
        raise ValueError(f"days must be 1..{MAX_HISTORY_DAYS}, got {days}")
    arguments: dict[str, Any] = {"symbol": symbol, "days": days, "interval": interval}
    end = end_date.isoformat() if end_date else ""
    if end_date is not None:
        arguments["end_date"] = end
    return call_tool(
        session,
        "get_price_history",
        arguments,
        entity=f"{symbol}:{interval}:{days}:{end}",
        as_of=as_of or end_date,
        **kw,
    )


def fetch_price_history(
    session: Session,
    symbol: str,
    *,
    days: int,
    end_date: dt.date | None = None,
    interval: str = "daily",
    max_pages: int = 10,
    **kw: Any,
) -> list[dict[str, Any]]:
    """All bars for `days`, newest page first, paging back while `meta.window.truncated`.

    Each page is one budgeted call. Rows are de-duplicated by date and sorted ascending.
    """
    rows: dict[str, dict[str, Any]] = {}
    remaining, cursor = days, end_date
    for _ in range(max_pages):
        payload = get_price_history(
            session, symbol, days=min(remaining, MAX_HISTORY_DAYS), end_date=cursor,
            interval=interval, **kw,
        )
        for row in payload.get("data") or []:
            rows[str(row["date"])] = row
        window = (payload.get("meta") or {}).get("window") or {}
        if not window.get("truncated"):
            break
        remaining -= int(window.get("served_days") or 0)
        next_cursor = window.get("next_cursor")
        if remaining <= 0 or not next_cursor:
            break
        new_cursor = dt.date.fromisoformat(str(next_cursor))
        if cursor is not None and new_cursor >= cursor:
            raise TapetideError(f"get_price_history cursor did not move back: {next_cursor}")
        cursor = new_cursor
    else:
        raise TapetideError(
            f"get_price_history for {symbol} still truncated after {max_pages} pages"
        )
    return [rows[k] for k in sorted(rows)]


def get_company_profile(
    session: Session, isin: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(
        session, "get_company_profile", {"isin": isin}, entity=isin, as_of=as_of, **kw
    )


def get_forecasts(
    session: Session, isin: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(session, "get_forecasts", {"isin": isin}, entity=isin, as_of=as_of, **kw)


def screen_stocks(
    session: Session, filters: dict[str, Any], *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    # Hash, not a truncated prefix: two filter sets sharing a prefix must not share a cache key.
    entity = "sha256:" + hashlib.sha256(canonical_json(filters).encode()).hexdigest()[:32]
    return call_tool(session, "screen_stocks", filters, entity=entity, as_of=as_of, **kw)


def get_index_membership_asof(
    session: Session,
    index: str,
    date: dt.date,
    *,
    as_of: dt.date | None = None,
    **kw: Any,
) -> dict[str, Any]:
    return call_tool(
        session,
        "get_index_membership_asof",
        {"index": index, "date": date.isoformat()},
        entity=f"{index}:{date}",
        as_of=as_of or date,
        **kw,
    )


def get_adjustment_factors(
    session: Session, isin: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(
        session, "get_adjustment_factors", {"isin": isin}, entity=isin, as_of=as_of, **kw
    )
