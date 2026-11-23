"""Split/bonus-adjusted, ISIN-stitched daily prices as they were knowable on a date.

`prices_daily` is raw and keyed by the ISIN a row traded under. For security `isin` as of D:
  1. the chain is the ISINs linked to it by `isin_links` with effective_date <= D;
  2. bars are every chain ISIN's rows with date <= D;
  3. each bar is multiplied by the product of the split/bonus factors of chain actions with
     bar date < ex_date <= D (volume is divided by it), so the latest bar is the raw print.
Nothing dated after D is read. Rights and demergers carry no factor and change nothing.

A chain link with no split or bonus within LINK_ACTION_WINDOW days of it is reported in
`warnings`: the series would show a jump there, and the cause is a missing action.
"""

import datetime as dt

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import CorporateAction, IsinLink, PriceDaily

LINK_ACTION_WINDOW = dt.timedelta(days=7)
COLUMNS = ["date", "isin", "series", "open", "high", "low", "close", "volume", "factor"]


def chain_as_of(session: Session, isin: str, as_of: dt.date) -> list[str]:
    """ISINs of `isin`'s security, oldest first, using only links in effect on `as_of`."""
    links = [
        (old, new)
        for old, new in session.execute(
            select(IsinLink.old_isin, IsinLink.new_isin).where(IsinLink.effective_date <= as_of)
        )
    ]
    back = {new: old for old, new in links}
    fwd = {old: new for old, new in links}
    first = isin
    while first in back:
        first = back[first]
    chain = [first]
    while chain[-1] in fwd:
        chain.append(fwd[chain[-1]])
    return chain


def adjusted_prices(
    session: Session, isin: str, as_of: dt.date, *, start: dt.date | None = None
) -> pd.DataFrame:
    """Adjusted bars for `isin`'s security on or before `as_of` (and on/after `start`).

    `df.attrs["chain"]` lists the ISINs used; `df.attrs["warnings"]` any unexplained link.
    """
    chain = chain_as_of(session, isin, as_of)
    q = select(
        PriceDaily.date, PriceDaily.isin, PriceDaily.series, PriceDaily.open, PriceDaily.high,
        PriceDaily.low, PriceDaily.close, PriceDaily.volume,
    ).where(PriceDaily.isin.in_(chain), PriceDaily.date <= as_of)
    if start is not None:
        q = q.where(PriceDaily.date >= start)
    df = pd.DataFrame(session.execute(q.order_by(PriceDaily.date)).all(), columns=COLUMNS[:-1])
    if df.duplicated("date").any():
        # Two chain ISINs on one day should not happen; keep the newer ISIN's print.
        order = {i: n for n, i in enumerate(chain)}
        df = (
            df.assign(_rank=df["isin"].map(order))
            .sort_values(["date", "_rank"])
            .drop_duplicates("date", keep="last")
            .drop(columns="_rank")
        )

    actions = {
        (ex, kind): factor
        for ex, kind, factor in session.execute(
            select(CorporateAction.ex_date, CorporateAction.action_type, CorporateAction.adj_factor)
            .where(
                CorporateAction.isin.in_(chain),
                CorporateAction.ex_date <= as_of,
                CorporateAction.adj_factor.is_not(None),
            )
        )
    }  # one action can sit on two chain ISINs: (ex_date, type) counts it once
    factor = pd.Series(1.0, index=df.index)
    for (ex_date, _kind), f in actions.items():
        factor[df["date"] < ex_date] *= f
    df["factor"] = factor
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * df["factor"]
    df["volume"] = df["volume"] / df["factor"]
    df = df.reset_index(drop=True)[COLUMNS]

    warnings = []
    for old, new, effective in session.execute(
        select(IsinLink.old_isin, IsinLink.new_isin, IsinLink.effective_date).where(
            IsinLink.new_isin.in_(chain), IsinLink.effective_date <= as_of
        )
    ):
        if not any(abs(ex - effective) <= LINK_ACTION_WINDOW for ex, _ in actions):
            warnings.append(f"no split/bonus near ISIN change {old}->{new} on {effective}")
    df.attrs["chain"] = chain
    df.attrs["warnings"] = warnings
    return df
