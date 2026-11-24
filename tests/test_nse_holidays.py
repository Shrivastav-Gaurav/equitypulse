"""NSE holiday-master fetch and parse. HTTP is mocked with respx; payloads are real responses
recorded from https://www.nseindia.com/api/holiday-master?type=trading&year=YYYY."""

import datetime as dt

import httpx
import pytest
import respx

from equitypulse.sources.nse import (
    NSE_HOLIDAY_MASTER_URL,
    NseFetchError,
    NseFormatError,
    fetch_holiday_master,
    parse_trading_holidays,
)
from tests.conftest import load_json_fixture


def no_sleep(_seconds: float) -> None:
    pass


# --- parse -------------------------------------------------------------------------------


def test_parse_2026_equity_segment() -> None:
    entries = parse_trading_holidays(
        load_json_fixture("nse/holiday_master_trading_2026.json"), 2026
    )
    assert len(entries) == 20
    by_date = {e.date: e for e in entries}
    assert by_date[dt.date(2026, 1, 26)].description == "Republic Day"
    assert by_date[dt.date(2026, 11, 10)].description == "Diwali-Balipratipada"
    assert not any(e.is_session for e in entries)


def test_parse_uses_equity_segment_not_others() -> None:
    payload = load_json_fixture("nse/holiday_master_trading_2026.json")
    entries = parse_trading_holidays(payload, 2026)
    # CBM (bonds) has extra closures that are not equity holidays.
    assert len(payload["CBM"]) != len(payload["CM"])
    assert len(entries) == len(payload["CM"])


def test_parse_flags_special_live_trading_as_session_not_holiday() -> None:
    entries = parse_trading_holidays(
        load_json_fixture("nse/holiday_master_trading_2024.json"), 2024
    )
    special = next(e for e in entries if e.date == dt.date(2024, 3, 2))
    assert special.description == "Special Live Trading"
    assert special.is_session


def test_parse_rejects_payload_for_another_year() -> None:
    payload = load_json_fixture("nse/holiday_master_trading_2025.json")
    with pytest.raises(NseFormatError, match="2026"):
        parse_trading_holidays(payload, 2026)


@pytest.mark.parametrize(
    "payload",
    [{}, {"CM": []}, {"CM": [{"tradingDate": "not-a-date", "description": "x"}]}],
    ids=["no-CM", "empty-CM", "bad-date"],
)
def test_parse_rejects_malformed_payload(payload: dict) -> None:
    with pytest.raises(NseFormatError):
        parse_trading_holidays(payload, 2026)


def test_parse_rejects_duplicate_dates() -> None:
    row = {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day"}
    with pytest.raises(NseFormatError, match="duplicate"):
        parse_trading_holidays({"CM": [row, row]}, 2026)


# --- fetch -------------------------------------------------------------------------------


@respx.mock
def test_fetch_sends_year_and_browser_headers() -> None:
    payload = load_json_fixture("nse/holiday_master_trading_2026.json")
    route = respx.get(NSE_HOLIDAY_MASTER_URL).mock(return_value=httpx.Response(200, json=payload))
    assert fetch_holiday_master(2026, sleep=no_sleep) == payload
    request = route.calls.last.request
    assert request.url.params["type"] == "trading"
    assert request.url.params["year"] == "2026"
    assert "Mozilla" in request.headers["user-agent"]


@respx.mock
def test_fetch_retries_server_errors_then_succeeds() -> None:
    route = respx.get(NSE_HOLIDAY_MASTER_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"CM": []})]
    )
    assert fetch_holiday_master(2026, sleep=no_sleep) == {"CM": []}
    assert route.call_count == 2


@respx.mock
def test_fetch_does_not_retry_client_errors() -> None:
    route = respx.get(NSE_HOLIDAY_MASTER_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NseFetchError, match="404"):
        fetch_holiday_master(2026, sleep=no_sleep)
    assert route.call_count == 1


@respx.mock
def test_fetch_gives_up_after_repeated_network_errors() -> None:
    route = respx.get(NSE_HOLIDAY_MASTER_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(NseFetchError, match="3 attempts"):
        fetch_holiday_master(2026, attempts=3, sleep=no_sleep)
    assert route.call_count == 3


@respx.mock
def test_fetch_rejects_non_json() -> None:
    respx.get(NSE_HOLIDAY_MASTER_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(NseFetchError, match="JSON"):
        fetch_holiday_master(2026, sleep=no_sleep)


# --- redirect to www.nseindia.in (since Nov 2026) -----------------------------------------

_IN_URL = "https://www.nseindia.in/api/holiday-master"
_LOCATION = "https://www.nseindia.in/api/holiday-master?type=trading&year=2021"


@respx.mock
def test_fetch_follows_nses_redirect_to_nseindia_in() -> None:
    """Recorded: www.nseindia.com answers 301 to the same path on www.nseindia.in."""
    payload = load_json_fixture("nse/holiday_master_trading_2021.json")
    com = respx.get(NSE_HOLIDAY_MASTER_URL).mock(
        return_value=httpx.Response(301, headers={"location": _LOCATION})
    )
    dot_in = respx.get(_IN_URL).mock(return_value=httpx.Response(200, json=payload))
    assert fetch_holiday_master(2021, sleep=no_sleep) == payload
    assert com.call_count == 1 and dot_in.call_count == 1
    assert dot_in.calls.last.request.url.params["year"] == "2021"
    assert len(parse_trading_holidays(payload, 2021)) == 18


@pytest.mark.parametrize(
    "location",
    [
        "https://www.nseindia.evil.example/api/holiday-master?type=trading&year=2021",
        "https://nseindia.in/api/holiday-master?type=trading&year=2021",  # bare host: not allowed
        "https://www.nseindia.in/api/other?type=trading&year=2021",
        "http://www.nseindia.in/api/holiday-master?type=trading&year=2021",
    ],
)
@respx.mock
def test_fetch_refuses_a_redirect_anywhere_else(location: str) -> None:
    respx.get(NSE_HOLIDAY_MASTER_URL).mock(
        return_value=httpx.Response(301, headers={"location": location})
    )
    with pytest.raises(NseFetchError, match="unexpected address"):
        fetch_holiday_master(2021, sleep=no_sleep)


@respx.mock
def test_fetch_refuses_a_redirect_loop() -> None:
    respx.get(NSE_HOLIDAY_MASTER_URL).mock(
        return_value=httpx.Response(301, headers={"location": _LOCATION})
    )
    respx.get(_IN_URL).mock(
        return_value=httpx.Response(301, headers={"location": NSE_HOLIDAY_MASTER_URL})
    )
    with pytest.raises(NseFetchError, match="more than"):
        fetch_holiday_master(2021, sleep=no_sleep)
