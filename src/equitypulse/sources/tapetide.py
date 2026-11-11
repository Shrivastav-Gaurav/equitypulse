"""Tapetide MCP client with budget guard, snapshot dedup, and DB cache.

Every call path:
  1. Check the DB cache (snapshots); return cached payload if present for the same as_of.
  2. Check the daily budget (api_usage); refuse if exhausted.
  3. Call the MCP tool (async, run via asyncio.run); store the raw payload in snapshots.
  4. Increment api_usage.calls for today.

All Tapetide calls go through `call_tool()` — never call the MCP session directly from
other modules.
"""

import asyncio
import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.config import get_settings
from equitypulse.db.models import ApiUsage, Snapshot
from equitypulse.db.types import utcnow
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


# ── cache lookup ──────────────────────────────────────────────────────────────


def _cached_payload(
    session: Session, tool: str, entity: str, as_of: dt.date
) -> dict[str, Any] | None:
    """Return the cached payload for (tool, entity, as_of) if one exists, else None."""
    row = session.scalar(
        select(Snapshot).where(
            Snapshot.source == SOURCE,
            Snapshot.endpoint == tool,
            Snapshot.entity == entity,
            Snapshot.as_of == as_of,
        )
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
    from mcp.client.streamable_http import streamable_http_client

    settings = get_settings()
    url = settings.tapetide_mcp_url
    token = settings.tapetide_api_token.get_secret_value()

    async with streamable_http_client(
        url, headers={"Authorization": f"Bearer {token}"}
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if result.isError:
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
    _budget: int | None = None,  # injectable for tests; overrides settings
) -> dict[str, Any]:
    """Call a Tapetide MCP tool, respecting budget and caching.

    - `entity`: logical identifier for the cache key (e.g. ISIN or index name).
    - `as_of`: the date the data is valid for; defaults to today_ist().
    - `_mcp_caller`: async callable (tool, args) → dict, injectable for testing.

    Does not commit; the caller owns the transaction.
    """
    if as_of is None:
        as_of = today_ist(now)

    cached = _cached_payload(session, tool, entity, as_of)
    if cached is not None:
        log.debug("Tapetide cache hit: tool=%s entity=%s as_of=%s", tool, entity, as_of)
        return cached

    _check_budget(session, now, budget=_budget)

    caller = _mcp_caller or (lambda t, a: asyncio.run(_call_mcp_tool(t, a)))
    payload = caller(tool, arguments)

    store_snapshot(
        session,
        source=SOURCE,
        endpoint=tool,
        entity=entity,
        as_of=as_of,
        payload=payload,
        fetched_at=utcnow(),
    )
    _record_call(session, now)
    session.flush()
    return payload


# ── typed wrappers ────────────────────────────────────────────────────────────


def search_stocks(
    session: Session, query: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(session, "search_stocks", {"query": query}, entity=query, as_of=as_of, **kw)


def get_price_history(
    session: Session, isin: str, *, as_of: dt.date | None = None, **kw: Any
) -> dict[str, Any]:
    return call_tool(session, "get_price_history", {"isin": isin}, entity=isin, as_of=as_of, **kw)


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
    entity = canonical_json(filters)[:64]
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
