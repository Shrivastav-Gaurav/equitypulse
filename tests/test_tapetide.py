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


# ── review fixes ──────────────────────────────────────────────────────────────

import httpx  # noqa: E402

from equitypulse.sources.tapetide import TapetideError  # noqa: E402


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://mcp.example/mcp")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def _flaky(errors: list):
    calls: list[str] = []

    def caller(tool: str, args: dict) -> dict:
        calls.append(tool)
        if errors:
            raise errors.pop(0)
        return SAMPLE_PAYLOAD

    return caller, calls


def _kw(caller, budget=10):
    return dict(now=_NOW, _mcp_caller=caller, _budget=budget, _sleep=lambda _s: None)


def _call(db_session, caller, budget=10):
    return call_tool(
        db_session, "search_stocks", {"q": "x"}, entity="x", as_of=DATE, **_kw(caller, budget)
    )


def test_retries_transient_5xx_then_succeeds(db_session: Session) -> None:
    caller, calls = _flaky([_status_error(503), httpx.ConnectError("down")])
    assert _call(db_session, caller)["result"] == "ok"
    assert len(calls) == 3


def test_retries_inside_exception_group(db_session: Session) -> None:
    caller, calls = _flaky([ExceptionGroup("tg", [httpx.ReadTimeout("slow")])])
    _call(db_session, caller)
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_never_retries_4xx(db_session: Session, code: int) -> None:
    caller, calls = _flaky([_status_error(code)])
    with pytest.raises(httpx.HTTPStatusError):
        _call(db_session, caller)
    assert len(calls) == 1


def test_4xx_inside_exception_group_not_retried(db_session: Session) -> None:
    caller, calls = _flaky([ExceptionGroup("tg", [_status_error(401)])])
    with pytest.raises(ExceptionGroup):
        _call(db_session, caller)
    assert len(calls) == 1


def test_tool_error_not_retried(db_session: Session) -> None:
    caller, calls = _flaky([TapetideError("bad tool args")])
    with pytest.raises(TapetideError):
        _call(db_session, caller)
    assert len(calls) == 1


def test_gives_up_after_max_attempts(db_session: Session) -> None:
    caller, calls = _flaky([_status_error(500)] * 5)
    with pytest.raises(httpx.HTTPStatusError):
        _call(db_session, caller)
    assert len(calls) == 3


def test_every_attempt_counts_against_budget(db_session: Session) -> None:
    caller, _ = _flaky([_status_error(500), _status_error(500)])
    _call(db_session, caller)
    assert db_session.get(ApiUsage, (DATE, "TAPETIDE")).calls == 3


def test_failed_call_still_counts_and_stores_no_snapshot(db_session: Session) -> None:
    caller, _ = _flaky([_status_error(400)])
    with pytest.raises(httpx.HTTPStatusError):
        _call(db_session, caller)
    assert db_session.get(ApiUsage, (DATE, "TAPETIDE")).calls == 1
    assert db_session.scalar(select(Snapshot)) is None


def test_retries_stop_when_budget_runs_out(db_session: Session) -> None:
    caller, calls = _flaky([_status_error(500)] * 5)
    with pytest.raises(BudgetExceededError):
        _call(db_session, caller, budget=2)
    assert len(calls) == 2


def test_budget_override_requires_injected_caller(db_session: Session) -> None:
    with pytest.raises(ValueError, match="_budget"):
        call_tool(
            db_session, "search_stocks", {"q": "x"}, entity="x", as_of=DATE,
            now=_NOW, _budget=10**6,
        )


def test_screen_stocks_filters_sharing_a_prefix_do_not_collide(db_session: Session) -> None:
    prefix = {"sector": "x" * 80}
    seen: list[dict] = []

    def caller(tool: str, args: dict) -> dict:
        seen.append(args)
        return {"n": len(seen)}

    kw = dict(now=_NOW, _mcp_caller=caller, _budget=10)
    a = screen_stocks(db_session, {**prefix, "min_cap": 1}, as_of=DATE, **kw)
    b = screen_stocks(db_session, {**prefix, "min_cap": 2}, as_of=DATE, **kw)
    assert a != b
    assert len(seen) == 2


def test_cache_returns_latest_snapshot_and_snapshots_carry_as_of(db_session: Session) -> None:
    from equitypulse.snapshots import store_snapshot

    for n in (1, 2):
        store_snapshot(
            db_session, source="TAPETIDE", endpoint="search_stocks", entity="x",
            as_of=DATE, payload={"n": n},
        )
    db_session.commit()
    assert _call(db_session, lambda t, a: {}) == {"n": 2}
    assert all(s.as_of == DATE for s in db_session.scalars(select(Snapshot)).all())
