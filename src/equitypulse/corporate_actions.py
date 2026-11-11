"""Load corporate actions (splits, bonuses, rights, demergers) via Tapetide.

Tapetide `get_adjustment_factors` returns a list of events for one ISIN.
SPLIT and BONUS events carry an `adj_factor`; RIGHTS and DEMERGER events are
flagged with `adj_factor=None`.

Budget-awareness: if the budget is exhausted mid-loop, the run is marked
DEGRADED and remaining ISINs are skipped rather than raising.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.calendar import today_ist
from equitypulse.db.models import CorporateAction
from equitypulse.sources.tapetide import BudgetExceededError, get_adjustment_factors

SOURCE = "TAPETIDE"

# Action types that always have an adjustment factor.
_FACTOR_ACTIONS = {"SPLIT", "BONUS"}


class LoadCorporateActionsResult(BaseModel):
    inserted: int = 0
    already_present: int = 0
    degraded: bool = False
    skipped_isins: int = 0


def load_corporate_actions(
    session: Session,
    isins: list[str],
    *,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
    _mcp_caller: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    _budget: int | None = None,
) -> LoadCorporateActionsResult:
    """Fetch and upsert corporate actions for the given ISINs.

    Calls are cached by Tapetide's DB cache, so a second run for the same ISIN
    and as_of date costs no API call.  Does not commit; the caller owns the
    transaction.
    """
    if as_of is None:
        as_of = today_ist(now)

    result = LoadCorporateActionsResult()
    kw: dict[str, Any] = dict(as_of=as_of, now=now)
    if _mcp_caller is not None:
        kw["_mcp_caller"] = _mcp_caller
    if _budget is not None:
        kw["_budget"] = _budget

    for isin in isins:
        try:
            payload = get_adjustment_factors(session, isin, **kw)
        except BudgetExceededError:
            result.degraded = True
            result.skipped_isins += 1
            continue

        adjustments = payload.get("adjustments") or []
        for adj in adjustments:
            ex_date = dt.date.fromisoformat(adj["ex_date"])
            action_type = adj["action_type"].upper()
            details = adj.get("details")
            adj_factor = adj.get("adj_factor")

            # Check for existing row (idempotency).
            existing = session.scalar(
                select(CorporateAction).where(
                    CorporateAction.isin == isin,
                    CorporateAction.ex_date == ex_date,
                    CorporateAction.action_type == action_type,
                )
            )
            if existing is not None:
                result.already_present += 1
                continue

            session.add(
                CorporateAction(
                    isin=isin,
                    ex_date=ex_date,
                    action_type=action_type,
                    details=details,
                    adj_factor=adj_factor,
                    source=SOURCE,
                )
            )
            result.inserted += 1

        session.flush()

    return result
