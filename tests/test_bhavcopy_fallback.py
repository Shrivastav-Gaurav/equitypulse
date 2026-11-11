"""T8: Bhavcopy fallback — when NSE fetch fails, use Tapetide get_price_history.

The fallback result carries degraded=True so callers can mark the audit row DEGRADED.
"""

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from equitypulse.db.models import PriceDaily, Security
from equitypulse.prices import SOURCE_TAPETIDE, LoadPricesResult, load_bhavcopy
from equitypulse.sources.nse import NseFetchError

DATE = dt.date(2026, 11, 11)
_NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)

# A minimal Tapetide get_price_history response for one ISIN.
_TAPETIDE_RESPONSE = {
    "isin": "INE002A01018",
    "data": [
        {
            "date": "2026-11-11",
            "open": 2800.0,
            "high": 2850.0,
            "low": 2750.0,
            "close": 2820.0,
            "prev_close": 2790.0,
            "volume": 5000000,
            "traded_value": 14100000000.0,
        }
    ],
}


def _failing_fetch_udiff(date: dt.date) -> str:
    raise NseFetchError("NSE is down")


def _failing_fetch_delivery(date: dt.date) -> str:
    raise NseFetchError("NSE is down")


def _mock_tapetide_caller(tool: str, args: dict) -> dict:
    return _TAPETIDE_RESPONSE


def _seed_active_security(session: Session) -> None:
    session.add(
        Security(
            isin="INE002A01018",
            symbol="RELIANCE",
            name="Reliance Industries",
            series="EQ",
            status="ACTIVE",
            source="TEST",
            as_of=DATE,
        )
    )
    session.flush()


def test_fallback_used_when_nse_fails(db_session: Session) -> None:
    _seed_active_security(db_session)
    result = load_bhavcopy(
        db_session,
        DATE,
        fetch_udiff=_failing_fetch_udiff,
        fetch_delivery=_failing_fetch_delivery,
        now=_NOW,
        tapetide_caller=_mock_tapetide_caller,
    )
    db_session.commit()
    assert isinstance(result, LoadPricesResult)
    assert result.degraded is True
    assert result.inserted >= 0  # Tapetide may or may not have data for this date


def test_fallback_inserts_price_row(db_session: Session) -> None:
    _seed_active_security(db_session)
    result = load_bhavcopy(
        db_session,
        DATE,
        fetch_udiff=_failing_fetch_udiff,
        fetch_delivery=_failing_fetch_delivery,
        now=_NOW,
        tapetide_caller=_mock_tapetide_caller,
    )
    db_session.commit()
    assert result.degraded is True
    row = db_session.get(PriceDaily, ("INE002A01018", DATE))
    assert row is not None
    assert row.source == SOURCE_TAPETIDE
    assert row.close == pytest.approx(2820.0)
    assert row.delivery_pct is None  # no delivery data from Tapetide


def test_fallback_marks_degraded_true(db_session: Session) -> None:
    _seed_active_security(db_session)
    result = load_bhavcopy(
        db_session,
        DATE,
        fetch_udiff=_failing_fetch_udiff,
        fetch_delivery=_failing_fetch_delivery,
        now=_NOW,
        tapetide_caller=_mock_tapetide_caller,
    )
    assert result.degraded is True


def test_no_fallback_when_nse_succeeds(db_session: Session) -> None:
    from tests.conftest import FIXTURES

    udiff_csv = (FIXTURES / "nse" / "bhavcopy_udiff_20261111.csv").read_text(encoding="utf-8")
    delivery_csv = (FIXTURES / "nse" / "bhavcopy_delivery_20261111.csv").read_text(encoding="utf-8")

    result = load_bhavcopy(
        db_session,
        DATE,
        fetch_udiff=lambda _d: udiff_csv,
        fetch_delivery=lambda _d: delivery_csv,
        now=_NOW,
        tapetide_caller=_mock_tapetide_caller,  # should not be called
    )
    db_session.commit()
    assert result.degraded is False
