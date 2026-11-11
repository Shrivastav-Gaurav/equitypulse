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
    get_price_history(db_session, "RELIANCE", days=5, as_of=DATE, **kwargs)
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


# ── get_price_history params and paging ───────────────────────────────────────

from equitypulse.sources.tapetide import fetch_price_history  # noqa: E402


def _page(rows: list[str], *, truncated: bool, served: int, cursor: str | None) -> dict:
    return {
        "data": [{"date": d, "close": 1, "raw_close": 1} for d in rows],
        "meta": {"window": {"truncated": truncated, "served_days": served, "next_cursor": cursor}},
    }


def test_get_price_history_sends_symbol_days_interval_and_end_date(db_session: Session) -> None:
    seen: list[dict] = []

    def caller(tool: str, args: dict) -> dict:
        seen.append(args)
        return SAMPLE_PAYLOAD

    get_price_history(
        db_session, "RELIANCE", days=30, end_date=dt.date(2026, 11, 11),
        now=_NOW, _mcp_caller=caller, _budget=10,
    )
    assert seen == [
        {"symbol": "RELIANCE", "days": 30, "interval": "daily", "end_date": "2026-11-11"}
    ]


def test_get_price_history_omits_end_date_when_not_given(db_session: Session) -> None:
    seen: list[dict] = []

    def caller(tool: str, args: dict) -> dict:
        seen.append(args)
        return SAMPLE_PAYLOAD

    get_price_history(db_session, "RELIANCE", days=5, now=_NOW, _mcp_caller=caller, _budget=10)
    assert "end_date" not in seen[0]


@pytest.mark.parametrize("days", [0, -1, 2001])
def test_get_price_history_rejects_bad_days(db_session: Session, days: int) -> None:
    with pytest.raises(ValueError, match="days"):
        get_price_history(db_session, "RELIANCE", days=days, now=_NOW,
                          _mcp_caller=lambda t, a: {}, _budget=10)


def test_get_price_history_cache_key_includes_days_and_end_date(db_session: Session) -> None:
    n = [0]

    def caller(tool: str, args: dict) -> dict:
        n[0] += 1
        return {"n": n[0]}

    kw = dict(now=_NOW, _mcp_caller=caller, _budget=10, as_of=DATE)
    a = get_price_history(db_session, "RELIANCE", days=5, **kw)
    b = get_price_history(db_session, "RELIANCE", days=6, **kw)
    c = get_price_history(db_session, "RELIANCE", days=5, end_date=dt.date(2026, 1, 1), **kw)
    again = get_price_history(db_session, "RELIANCE", days=5, **kw)
    assert len({a["n"], b["n"], c["n"]}) == 3
    assert again == a


def test_fetch_price_history_single_page_when_not_truncated(db_session: Session) -> None:
    calls: list[dict] = []

    def caller(tool: str, args: dict) -> dict:
        calls.append(args)
        return _page(["2026-11-06", "2026-11-09"], truncated=False, served=5, cursor="2026-11-05")

    rows = fetch_price_history(db_session, "X", days=5, now=_NOW, _mcp_caller=caller, _budget=10)
    assert [r["date"] for r in rows] == ["2026-11-06", "2026-11-09"]
    assert len(calls) == 1  # has_more-style cursor alone does not trigger paging


def test_fetch_price_history_pages_back_with_next_cursor(db_session: Session) -> None:
    calls: list[dict] = []
    pages = [
        _page(["2026-03-02", "2026-03-03"], truncated=True, served=2000, cursor="2026-03-01"),
        _page(["2026-02-27", "2026-03-01", "2026-03-02"], truncated=False, served=500, cursor=None),
    ]

    def caller(tool: str, args: dict) -> dict:
        calls.append(args)
        return pages[len(calls) - 1]

    rows = fetch_price_history(
        db_session, "X", days=2500, end_date=dt.date(2026, 3, 3),
        now=_NOW, _mcp_caller=caller, _budget=10,
    )
    assert calls[0]["days"] == 2000 and calls[0]["end_date"] == "2026-03-03"
    assert calls[1]["days"] == 500 and calls[1]["end_date"] == "2026-03-01"
    assert [r["date"] for r in rows] == ["2026-02-27", "2026-03-01", "2026-03-02", "2026-03-03"]


def test_fetch_price_history_refuses_a_cursor_that_does_not_move(db_session: Session) -> None:
    def caller(tool: str, args: dict) -> dict:
        return _page(["2026-03-03"], truncated=True, served=10, cursor="2026-03-03")

    with pytest.raises(TapetideError, match="cursor"):
        fetch_price_history(db_session, "X", days=100, end_date=dt.date(2026, 3, 3),
                            now=_NOW, _mcp_caller=caller, _budget=10)


def test_fetch_price_history_gives_up_after_max_pages(db_session: Session) -> None:
    n = [0]

    def caller(tool: str, args: dict) -> dict:
        n[0] += 1
        return _page([f"2026-01-{30 - n[0]:02d}"], truncated=True, served=1,
                     cursor=f"2026-01-{29 - n[0]:02d}")

    with pytest.raises(TapetideError, match="truncated"):
        fetch_price_history(db_session, "X", days=100, max_pages=3,
                            now=_NOW, _mcp_caller=caller, _budget=10)
    assert n[0] == 3


# ── usage is committed before the call ────────────────────────────────────────


def _usage_rows(db_session: Session) -> list[tuple]:
    import sqlite3

    path = db_session.get_bind().url.database
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT calls FROM api_usage").fetchall()


def test_count_is_committed_before_the_request_is_sent(db_session: Session) -> None:
    seen_during_call: list = []

    def caller(tool: str, args: dict) -> dict:
        seen_during_call.append(_usage_rows(db_session))  # a separate connection
        return SAMPLE_PAYLOAD

    _call(db_session, caller)
    assert seen_during_call == [[(1,)]]


def test_rollback_after_a_successful_call_keeps_the_count(db_session: Session) -> None:
    _call(db_session, lambda t, a: SAMPLE_PAYLOAD)
    db_session.rollback()  # caller abandons its transaction (snapshot is lost, count is not)
    assert _usage_rows(db_session) == [(1,)]
    assert db_session.scalar(select(Snapshot)) is None


def test_rollback_after_a_failed_call_keeps_the_count(db_session: Session) -> None:
    caller, _ = _flaky([_status_error(400)])
    with pytest.raises(httpx.HTTPStatusError):
        _call(db_session, caller)
    db_session.rollback()
    assert _usage_rows(db_session) == [(1,)]


def test_each_retry_attempt_is_committed_separately(db_session: Session) -> None:
    during: list = []

    def caller(tool: str, args: dict) -> dict:
        during.append(_usage_rows(db_session)[0][0])
        if len(during) < 3:
            raise _status_error(503)
        return SAMPLE_PAYLOAD

    _call(db_session, caller)
    assert during == [1, 2, 3]


def test_budget_refusal_does_not_count_an_attempt(db_session: Session) -> None:
    _call(db_session, lambda t, a: SAMPLE_PAYLOAD, budget=1)
    with pytest.raises(BudgetExceededError):
        call_tool(db_session, "search_stocks", {"q": "y"}, entity="y", as_of=DATE,
                  **_kw(lambda t, a: SAMPLE_PAYLOAD, budget=1))
    db_session.rollback()
    assert _usage_rows(db_session) == [(1,)]
