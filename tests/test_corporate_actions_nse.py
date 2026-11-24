"""T17: NSE corporate actions — subject parsing, ISIN resolution, idempotent load."""
# ruff: noqa: E501

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.corporate_actions_nse import _year_chunks, load_nse_corporate_actions
from equitypulse.db.models import CorporateAction, PriceDaily, Security
from equitypulse.sources.nse import NseFormatError
from equitypulse.sources.nse_actions import parse_corporate_actions, parse_subject
from tests.conftest import FIXTURES

ROWS = json.loads((FIXTURES / "nse" / "corporate_actions_sample.json").read_text(encoding="utf-8"))
D = dt.date(2026, 1, 1)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", [("SPLIT", 0.2)]),
        ("Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share", [("SPLIT", 0.2)]),
        ("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share", [("SPLIT", 10.0)]),
        ("Bonus 1:10", [("BONUS", 10 / 11)]),
        ("Bonus 4:1", [("BONUS", 1 / 5)]),
        ("Bonus 1:1", [("BONUS", 0.5)]),
        ("Rights 5:41 @ Premium Rs 109/-", [("RIGHTS", None)]),
        ("Demerger", [("DEMERGER", None)]),
        ("Scheme Of Arrangement - Bonus Ncrps 4:1", [("OTHER", None)]),  # preference shares
        ("Interim Dividend - Rs 2 Per Share", []),
        ("Buy Back", []),
        # Variants recorded from 2018-2024 NSE records:
        (" Bonus 1:1 /Dividend- Rs 29 Per Share", [("BONUS", 0.5)]),  # TCS 2018
        (" Bonus 1:2/Face Value Split (Sub-Division) From Rs 2/- Per Share To Re 1/- Per Share",
         [("BONUS", 2 / 3), ("SPLIT", 0.5)]),  # AVANTIFEED 2018: two actions in one record
        (" Face Value Split (Sub-Division) - From Rs 10 /- Per Share To Rs 2/- Per Share",
         [("SPLIT", 0.2)]),  # ZENSARTECH 2018
        ("Face Value Split (Sub-Division) - From Rs10/- Per Share To Re 1/- Per Share",
         [("SPLIT", 0.1)]),  # NESTLEIND 2024
        ("Bonus- 1:2", [("BONUS", 2 / 3)]),  # AJANTPHARM 2022
        ("Bonus 1: 1", [("BONUS", 0.5)]),
        (" Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
         [("OTHER", None)]),  # BRITANNIA: a debenture, not shares
        ("Bonus Ncrps 1:116", [("OTHER", None)]),
        ("Bonus 1:1 And Face Value Split Of Rs 10 To Rs 5", [("OTHER", None)]),  # unknown shape
    ],
)
def test_parse_subject(subject: str, expected) -> None:
    got = parse_subject(subject)
    assert [kind for kind, _f in got] == [kind for kind, _f in expected]
    for (_k, f_got), (_k2, f_exp) in zip(got, expected, strict=True):
        assert f_got == (pytest.approx(f_exp) if f_exp is not None else None)


def test_parse_fixture_keeps_share_changes_and_flags_and_skips_dividends() -> None:
    actions, skipped = parse_corporate_actions(ROWS)
    assert skipped == 1  # the dividend
    kinds = {(a.symbol, a.action_type) for a in actions}
    assert ("BESTAGRO", "SPLIT") in kinds and ("BESTAGRO", "BONUS") in kinds
    assert ("ITC", "DEMERGER") in kinds and ("CAMLINFINE", "RIGHTS") in kinds
    assert ("TVSMOTOR", "OTHER") in kinds
    kotak = next(a for a in actions if a.symbol == "KOTAKBANK")
    assert kotak.ex_date == dt.date(2026, 1, 14)


def test_parse_rejects_a_non_list_payload() -> None:
    with pytest.raises(NseFormatError, match="not a list"):
        parse_corporate_actions({"data": []})  # type: ignore[arg-type]


def test_year_chunks_split_at_calendar_years() -> None:
    assert _year_chunks(dt.date(2024, 11, 11), dt.date(2026, 1, 31)) == [
        (dt.date(2024, 11, 11), dt.date(2024, 12, 31)),
        (dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
        (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
    ]


# ── load ─────────────────────────────────────────────────────────────────────

KOTAK_OLD, KOTAK_NEW = "INE237A01028", "INE237A01036"


def _sec(session: Session, isin: str, symbol: str) -> None:
    session.add(Security(isin=isin, symbol=symbol, name=symbol, status="ACTIVE", source="T", as_of=D))


def _bar(session: Session, isin: str, date: dt.date) -> None:
    session.add(PriceDaily(isin=isin, date=date, open=1, high=1, low=1, close=1, volume=1,
                           series="EQ", source="NSE_BHAVCOPY"))


def _seed(session: Session) -> None:
    _sec(session, KOTAK_OLD, "KOTAKBANK")
    _sec(session, KOTAK_NEW, "KOTAKBANK")
    _sec(session, "INE745G01043", "MCX")  # MCX's post-split ISIN; the record carries the old one
    _sec(session, "INE154A01025", "ITC")
    _sec(session, "INE00FF01025", "ACUTAAS")
    session.flush()
    _bar(session, KOTAK_OLD, dt.date(2026, 1, 13))
    _bar(session, KOTAK_NEW, dt.date(2026, 1, 14))
    session.flush()


def test_actions_resolve_by_issuer_and_symbol_not_by_nse_isin(db_session: Session) -> None:
    _seed(db_session)
    result = load_nse_corporate_actions(db_session, D, dt.date(2026, 1, 31),
                                        fetch=lambda _s, _e: ROWS)
    stored = {(a.isin, a.action_type) for a in db_session.scalars(select(CorporateAction))}
    # KOTAK's record says INE237A01010; it lands on the ISIN that traded on the ex-date.
    assert (KOTAK_NEW, "SPLIT") in stored
    assert ("INE745G01043", "SPLIT") in stored  # MCX, same issuer E745G
    assert ("INE154A01025", "DEMERGER") in stored
    assert ("INE00FF01025", "SPLIT") in stored
    assert result.not_in_universe == len(parse_corporate_actions(ROWS)[0]) - 4
    kotak = db_session.scalar(select(CorporateAction).where(CorporateAction.isin == KOTAK_NEW))
    assert kotak.adj_factor == pytest.approx(0.2)
    assert kotak.source == "NSE_CORP_ACTIONS"


def test_load_is_idempotent(db_session: Session) -> None:
    _seed(db_session)
    load_nse_corporate_actions(db_session, D, dt.date(2026, 1, 31), fetch=lambda _s, _e: ROWS)
    again = load_nse_corporate_actions(db_session, D, dt.date(2026, 1, 31),
                                       fetch=lambda _s, _e: ROWS)
    assert again.inserted == 0 and again.already_present == 4


def test_unparsed_share_changes_are_counted(db_session: Session) -> None:
    _seed(db_session)
    result = load_nse_corporate_actions(db_session, D, dt.date(2026, 1, 31),
                                        fetch=lambda _s, _e: ROWS)
    assert result.unparsed == 1  # TVSMOTOR's NCRPS bonus


def test_one_request_per_calendar_year(db_session: Session) -> None:
    calls: list = []

    def fetch(s: dt.date, e: dt.date) -> list:
        calls.append((s, e))
        return []

    load_nse_corporate_actions(db_session, dt.date(2024, 11, 11), dt.date(2026, 1, 31), fetch=fetch)
    assert len(calls) == 3
