"""T17: read-time split/bonus adjustment, stitched across ISIN changes, as known on D."""

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from equitypulse.adjust import adjusted_prices, chain_as_of
from equitypulse.db.models import CorporateAction, IsinLink, PriceDaily, Security

A = "INE0AAA01011"
OLD, NEW = "INE237A01028", "INE237A01036"  # KOTAKBANK, ISIN changed with the 2026-01-14 split
EX = dt.date(2026, 1, 14)


def _sec(session: Session, isin: str, symbol: str = "X") -> None:
    session.add(Security(isin=isin, symbol=symbol, name=symbol, status="ACTIVE", source="T",
                         as_of=EX))
    session.flush()


def _bars(session: Session, isin: str, closes: dict[dt.date, float], volume: int = 1000) -> None:
    for d, c in closes.items():
        session.add(PriceDaily(isin=isin, date=d, open=c, high=c * 1.01, low=c * 0.99, close=c,
                               volume=volume, series="EQ", source="NSE_BHAVCOPY"))
    session.flush()


def _action(session: Session, isin: str, ex: dt.date, kind: str, factor: float | None) -> None:
    session.add(CorporateAction(isin=isin, ex_date=ex, action_type=kind, details=kind,
                                adj_factor=factor, source="NSE_CORP_ACTIONS"))
    session.flush()


def _d(day: int) -> dt.date:
    return dt.date(2026, 1, day)


def test_split_adjusts_earlier_bars_and_volume(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 1000.0, _d(13): 1010.0, _d(14): 204.0, _d(15): 206.0})
    _action(db_session, A, EX, "SPLIT", 0.2)  # face value 10 -> 2
    df = adjusted_prices(db_session, A, _d(15))
    assert df["close"].tolist() == pytest.approx([200.0, 202.0, 204.0, 206.0])
    assert df["volume"].tolist() == pytest.approx([5000, 5000, 1000, 1000])
    assert df["factor"].tolist() == pytest.approx([0.2, 0.2, 1.0, 1.0])


def test_bonus_one_for_one_halves_earlier_prices(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 500.0, _d(14): 251.0})
    _action(db_session, A, EX, "BONUS", 0.5)
    assert adjusted_prices(db_session, A, _d(14))["close"].tolist() == pytest.approx([250.0, 251.0])


def test_split_and_bonus_on_one_day_compound(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 600.0, _d(14): 40.0})
    _action(db_session, A, EX, "SPLIT", 0.1)
    _action(db_session, A, EX, "BONUS", 2 / 3)  # bonus 1:2
    assert adjusted_prices(db_session, A, _d(14))["close"].tolist() == pytest.approx([40.0, 40.0])


def test_rights_and_demergers_change_nothing(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 100.0, _d(14): 90.0})
    _action(db_session, A, EX, "RIGHTS", None)
    _action(db_session, A, EX, "DEMERGER", None)
    assert adjusted_prices(db_session, A, _d(14))["close"].tolist() == [100.0, 90.0]


def test_no_look_ahead_on_actions_and_prices(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 1000.0, _d(13): 1010.0, _d(14): 204.0})
    _action(db_session, A, EX, "SPLIT", 0.2)
    df = adjusted_prices(db_session, A, _d(13))  # the day before the ex-date
    assert df["date"].tolist() == [_d(12), _d(13)]  # the 14th is not read
    assert df["close"].tolist() == [1000.0, 1010.0]  # the split is not known yet


def test_kotak_series_is_continuous_across_its_isin_change(db_session: Session) -> None:
    _sec(db_session, OLD, "KOTAKBANK")
    _sec(db_session, NEW, "KOTAKBANK")
    _bars(db_session, OLD, {_d(12): 2100.0, _d(13): 2110.0})
    _bars(db_session, NEW, {_d(14): 423.0, _d(15): 425.0})
    db_session.add(IsinLink(old_isin=OLD, new_isin=NEW, effective_date=EX, reason="ISIN_CHANGE",
                            source="NSE_BHAVCOPY"))
    _action(db_session, NEW, EX, "SPLIT", 0.2)  # face value 5 -> 1, stored on the new ISIN
    for start in (OLD, NEW):  # either ISIN gives the same stitched series
        df = adjusted_prices(db_session, start, _d(15))
        assert df["close"].tolist() == pytest.approx([420.0, 422.0, 423.0, 425.0])
        assert df["isin"].tolist() == [OLD, OLD, NEW, NEW]
        assert df.attrs["chain"] == [OLD, NEW]
        assert df.attrs["warnings"] == []


def test_isin_change_without_a_split_is_reported(db_session: Session) -> None:
    _sec(db_session, OLD, "KOTAKBANK")
    _sec(db_session, NEW, "KOTAKBANK")
    _bars(db_session, OLD, {_d(13): 2110.0})
    _bars(db_session, NEW, {_d(14): 423.0})
    db_session.add(IsinLink(old_isin=OLD, new_isin=NEW, effective_date=EX, reason="ISIN_CHANGE",
                            source="NSE_BHAVCOPY"))
    db_session.flush()
    df = adjusted_prices(db_session, NEW, _d(14))
    assert len(df.attrs["warnings"]) == 1 and "no split/bonus" in df.attrs["warnings"][0]


def test_a_link_is_not_used_before_it_takes_effect(db_session: Session) -> None:
    _sec(db_session, OLD, "KOTAKBANK")
    _sec(db_session, NEW, "KOTAKBANK")
    db_session.add(IsinLink(old_isin=OLD, new_isin=NEW, effective_date=EX, reason="ISIN_CHANGE",
                            source="NSE_BHAVCOPY"))
    db_session.flush()
    assert chain_as_of(db_session, NEW, _d(13)) == [NEW]
    assert chain_as_of(db_session, NEW, _d(14)) == [OLD, NEW]


def test_start_trims_the_window(db_session: Session) -> None:
    _sec(db_session, A)
    _bars(db_session, A, {_d(12): 1000.0, _d(13): 1010.0, _d(14): 204.0})
    _action(db_session, A, EX, "SPLIT", 0.2)
    df = adjusted_prices(db_session, A, _d(14), start=_d(13))
    assert df["close"].tolist() == pytest.approx([202.0, 204.0])


@pytest.mark.skip(
    reason="Pending a paid Tapetide plan: compare adjusted closes for a 20-stock sample with "
    "Tapetide's split/bonus-adjusted closes (within 0.01%; Phase 1a gate check 2). Enable this "
    "test, record the responses as fixtures and remove this skip once the plan is active."
)
def test_adjusted_closes_match_tapetide_sample() -> None:
    raise AssertionError("not implemented until the Tapetide plan is active")
