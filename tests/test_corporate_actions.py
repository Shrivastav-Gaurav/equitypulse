"""Corporate actions: Tapetide adjustment factors loader."""
# ruff: noqa: E501

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.corporate_actions import LoadCorporateActionsResult, load_corporate_actions
from equitypulse.db.models import CorporateAction, Security
from tests.conftest import FIXTURES

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

_TAP_FIXTURES = FIXTURES.parent / "fixtures" / "tapetide"

RELIANCE_ISIN = "INE002A01018"
INFY_ISIN = "INE009A01021"
ITC_ISIN = "INE154A01025"


def _load_fixture(isin: str) -> dict:
    path = _TAP_FIXTURES / f"adjustment_factors_{isin}.json"
    return json.loads(path.read_text())


def _make_caller(isins: list[str]) -> object:
    """Return an injectable _mcp_caller that serves fixture data."""
    fixtures = {isin: _load_fixture(isin) for isin in isins}

    def caller(tool: str, args: dict) -> dict:
        isin = args.get("isin", "")
        if tool == "get_adjustment_factors" and isin in fixtures:
            return fixtures[isin]
        return {"adjustments": []}

    return caller


def _seed_securities(session: Session, isins: list[str]) -> None:
    for isin in isins:
        if session.get(Security, isin) is None:
            session.add(Security(isin=isin, symbol=isin[:6], name=isin, status="ACTIVE", source="TEST", as_of=DATE))
    session.flush()


# ── load ─────────────────────────────────────────────────────────────────────


def test_load_inserts_known_split(db_session: Session) -> None:
    _seed_securities(db_session, [RELIANCE_ISIN])
    caller = _make_caller([RELIANCE_ISIN])
    result = load_corporate_actions(
        db_session, [RELIANCE_ISIN], as_of=DATE, now=_NOW,
        _mcp_caller=caller, _budget=10,
    )
    db_session.commit()
    assert isinstance(result, LoadCorporateActionsResult)
    assert result.inserted >= 2  # BONUS + SPLIT


def test_load_split_has_adj_factor(db_session: Session) -> None:
    _seed_securities(db_session, [RELIANCE_ISIN])
    caller = _make_caller([RELIANCE_ISIN])
    load_corporate_actions(
        db_session, [RELIANCE_ISIN], as_of=DATE, now=_NOW,
        _mcp_caller=caller, _budget=10,
    )
    db_session.commit()
    split = db_session.scalar(
        select(CorporateAction).where(
            CorporateAction.isin == RELIANCE_ISIN,
            CorporateAction.action_type == "SPLIT",
        )
    )
    assert split is not None
    assert split.adj_factor == pytest.approx(0.5)
    assert split.ex_date == dt.date(2020, 5, 22)


def test_load_rights_has_no_adj_factor(db_session: Session) -> None:
    _seed_securities(db_session, [ITC_ISIN])
    caller = _make_caller([ITC_ISIN])
    load_corporate_actions(
        db_session, [ITC_ISIN], as_of=DATE, now=_NOW,
        _mcp_caller=caller, _budget=10,
    )
    db_session.commit()
    rights = db_session.scalar(
        select(CorporateAction).where(
            CorporateAction.isin == ITC_ISIN,
            CorporateAction.action_type == "RIGHTS",
        )
    )
    assert rights is not None
    assert rights.adj_factor is None


def test_load_is_idempotent(db_session: Session) -> None:
    _seed_securities(db_session, [INFY_ISIN])
    caller = _make_caller([INFY_ISIN])
    kw = dict(as_of=DATE, now=_NOW, _mcp_caller=caller, _budget=10)
    r1 = load_corporate_actions(db_session, [INFY_ISIN], **kw)
    db_session.commit()
    r2 = load_corporate_actions(db_session, [INFY_ISIN], **kw)
    db_session.commit()
    assert r1.inserted == 2
    assert r2.inserted == 0
    assert r2.already_present == 2


def test_load_multiple_isins(db_session: Session) -> None:
    isins = [RELIANCE_ISIN, INFY_ISIN]
    _seed_securities(db_session, isins)
    caller = _make_caller(isins)
    result = load_corporate_actions(
        db_session, isins, as_of=DATE, now=_NOW, _mcp_caller=caller, _budget=10,
    )
    db_session.commit()
    assert result.inserted == 4  # 2 each for RELIANCE and INFY


def test_budget_guard_stops_early(db_session: Session) -> None:
    isins = [RELIANCE_ISIN, INFY_ISIN, ITC_ISIN]
    _seed_securities(db_session, isins)
    caller = _make_caller(isins)
    # Only 1 call allowed → only the first ISIN processed.
    result = load_corporate_actions(
        db_session, isins, as_of=DATE, now=_NOW,
        _mcp_caller=caller, _budget=1,
    )
    db_session.commit()
    assert result.degraded is True
    # Only RELIANCE's actions loaded.
    all_rows = db_session.scalars(select(CorporateAction)).all()
    isins_in_db = {r.isin for r in all_rows}
    assert RELIANCE_ISIN in isins_in_db
    assert INFY_ISIN not in isins_in_db


def test_source_recorded(db_session: Session) -> None:
    _seed_securities(db_session, [RELIANCE_ISIN])
    caller = _make_caller([RELIANCE_ISIN])
    load_corporate_actions(
        db_session, [RELIANCE_ISIN], as_of=DATE, now=_NOW,
        _mcp_caller=caller, _budget=10,
    )
    db_session.commit()
    row = db_session.scalar(
        select(CorporateAction).where(CorporateAction.isin == RELIANCE_ISIN)
    )
    assert row is not None
    assert row.source == "TAPETIDE"
