"""Tapetide MCP client: budget guard, DB cache, snapshot storage, typed wrappers.

All tests use an injectable `_mcp_caller` so no real network calls are made.
"""

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import ApiUsage, Snapshot
from equitypulse.sources.tapetide import (
    BudgetExceededError,
    call_tool,
    get_adjustment_factors,
    get_company_profile,
    get_forecasts,
    get_index_membership_asof,
    get_price_history,
    screen_stocks,
    search_stocks,
)

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

SAMPLE_PAYLOAD = {"result": "ok", "data": [1, 2, 3]}


def _mock_caller(tool: str, args: dict) -> dict:
    return {**SAMPLE_PAYLOAD, "tool": tool}


# ── basic call ────────────────────────────────────────────────────────────────


_KW = dict(now=_NOW, _mcp_caller=_mock_caller, _budget=10)


def test_call_tool_returns_payload(db_session: Session) -> None:
    result = call_tool(
        db_session, "search_stocks", {"query": "RELIANCE"},
        entity="RELIANCE", as_of=DATE, **_KW,
    )
    assert result["result"] == "ok"
    assert result["tool"] == "search_stocks"


def test_call_tool_stores_snapshot(db_session: Session) -> None:
    call_tool(
        db_session, "get_company_profile", {"isin": "INE002A01018"},
        entity="INE002A01018", as_of=DATE, **_KW,
    )
    db_session.commit()
    snap = db_session.scalar(select(Snapshot).where(Snapshot.source == "TAPETIDE"))
    assert snap is not None
    assert snap.endpoint == "get_company_profile"
    assert snap.entity == "INE002A01018"
    assert snap.as_of == DATE


def test_call_tool_increments_usage(db_session: Session) -> None:
    call_tool(
        db_session, "search_stocks", {"query": "X"},
        entity="X", as_of=DATE, **_KW,
    )
    db_session.commit()
    usage = db_session.get(ApiUsage, (DATE, "TAPETIDE"))
    assert usage is not None
    assert usage.calls == 1


# ── cache ─────────────────────────────────────────────────────────────────────


def test_call_tool_uses_cache_on_second_call(db_session: Session) -> None:
    call_count = [0]

    def counting_caller(tool: str, args: dict) -> dict:
        call_count[0] += 1
        return SAMPLE_PAYLOAD

    kw = dict(now=_NOW, _mcp_caller=counting_caller, _budget=10)
    call_tool(db_session, "search_stocks", {"query": "X"}, entity="X", as_of=DATE, **kw)
    db_session.commit()
    call_tool(db_session, "search_stocks", {"query": "X"}, entity="X", as_of=DATE, **kw)
    assert call_count[0] == 1  # only called once; second was from cache


def test_cache_miss_different_as_of(db_session: Session) -> None:
    call_count = [0]

    def counting_caller(tool: str, args: dict) -> dict:
        call_count[0] += 1
        return SAMPLE_PAYLOAD

    kw = dict(now=_NOW, _mcp_caller=counting_caller, _budget=10)
    call_tool(
        db_session, "get_price_history", {"isin": "INE002A01018"},
        entity="INE002A01018", as_of=DATE, **kw,
    )
    db_session.commit()
    call_tool(
        db_session, "get_price_history", {"isin": "INE002A01018"},
        entity="INE002A01018", as_of=DATE + dt.timedelta(days=1), **kw,
    )
    assert call_count[0] == 2  # different as_of → not cached


# ── budget guard ──────────────────────────────────────────────────────────────


def test_budget_refusal_at_limit(db_session: Session) -> None:
    for i in range(2):
        call_tool(
            db_session, "search_stocks", {"query": str(i)},
            entity=str(i), as_of=DATE, now=_NOW, _mcp_caller=_mock_caller, _budget=2,
        )
        db_session.commit()
    with pytest.raises(BudgetExceededError, match="budget"):
        call_tool(
            db_session, "search_stocks", {"query": "overflow"},
            entity="overflow", as_of=DATE, now=_NOW, _mcp_caller=_mock_caller, _budget=2,
        )


def test_budget_zero_refuses_immediately(db_session: Session) -> None:
    with pytest.raises(BudgetExceededError):
        call_tool(
            db_session, "search_stocks", {"query": "X"},
            entity="X", as_of=DATE, now=_NOW, _mcp_caller=_mock_caller, _budget=0,
        )


def test_cache_hit_does_not_consume_budget(db_session: Session) -> None:
    call_tool(
        db_session, "search_stocks", {"query": "X"},
        entity="X", as_of=DATE, now=_NOW, _mcp_caller=_mock_caller, _budget=1,
    )
    db_session.commit()
    # Budget exhausted (calls=1, budget=1), but cache hit should still work.
    result = call_tool(
        db_session, "search_stocks", {"query": "X"},
        entity="X", as_of=DATE, now=_NOW, _mcp_caller=_mock_caller, _budget=1,
    )
    assert result["result"] == "ok"
    usage = db_session.get(ApiUsage, (DATE, "TAPETIDE"))
    assert usage is not None
    assert usage.calls == 1


# ── typed wrappers ────────────────────────────────────────────────────────────


def test_all_wrappers_call_correct_tool(db_session: Session) -> None:
    tools_called = []

    def recording_caller(tool: str, args: dict) -> dict:
        tools_called.append(tool)
        return SAMPLE_PAYLOAD

    kwargs: dict = dict(now=_NOW, _mcp_caller=recording_caller, _budget=50)
    search_stocks(db_session, "RELIANCE", as_of=DATE, **kwargs)
    get_price_history(db_session, "INE002A01018", as_of=DATE, **kwargs)
    get_company_profile(db_session, "INE002A01018", as_of=dt.date(2026, 11, 10), **kwargs)
    get_forecasts(db_session, "INE002A01018", as_of=dt.date(2026, 11, 9), **kwargs)
    screen_stocks(db_session, {"sector": "IT"}, as_of=DATE, **kwargs)
    get_index_membership_asof(db_session, "NIFTY500", DATE, **kwargs)
    get_adjustment_factors(db_session, "INE002A01018", as_of=dt.date(2026, 11, 8), **kwargs)

    assert "search_stocks" in tools_called
    assert "get_price_history" in tools_called
    assert "get_company_profile" in tools_called
    assert "get_forecasts" in tools_called
    assert "screen_stocks" in tools_called
    assert "get_index_membership_asof" in tools_called
    assert "get_adjustment_factors" in tools_called
