"""NSE (nseindia.com) downloads.

Holiday list: `GET https://www.nseindia.com/api/holiday-master?type=trading&year=YYYY`
(verified 2026-11-11). It returns JSON keyed by segment ("CM" = capital market / equities,
"FO", "CD", "CBM", ...), each a list of {"tradingDate": "26-Jan-2026", "weekDay",
"description", ...}. Notes from the recorded responses:
- Without `year` it returns the current year; with `year` it returns that year (2024+ checked).
- The list can contain trading sessions, not only closures (2024: "Special Live Trading" on a
  Saturday), so those are flagged as sessions rather than holidays.
- Weekend special sessions that NSE does not list (e.g. Budget-day Saturday 2025-02-01) and
  Muhurat trading go in config/calendar_overrides.yaml.

Equity master: `GET https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`
(verified 2026-11-11). Returns a CSV with headers:
  SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER,
  FACE VALUE
Note: some headers have leading/trailing spaces. All listed securities (EQ, BE, BZ, SM, …)
appear; this file does NOT contain delivery data. The newer UDiFF bhavcopy has ISIN but no
delivery; `sec_bhavdata_full_DDMMYYYY.csv` has delivery but no ISIN — T6 uses both.
"""

import csv
import datetime as dt
import io
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

NSE_HOLIDAY_MASTER_URL = "https://www.nseindia.com/api/holiday-master"
EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
EQUITY_SEGMENT = "CM"
_ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")

# NSE rejects obvious bots; a browser-like request is the documented workaround.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SESSION_PATTERN = re.compile(r"live trading|muhurat", re.IGNORECASE)


class NseFetchError(RuntimeError):
    """NSE could not be reached or refused the request."""


class NseFormatError(ValueError):
    """NSE answered, but not with what we expect; never guess around it."""


class NseHoliday(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    description: str
    is_session: bool  # True: the entry announces a trading session, not a closure


def fetch_holiday_master(
    year: int,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Raw holiday-master JSON for `year`. Retries network errors and 429/5xx with
    exponential backoff; any other 4xx fails at once (retrying won't fix it)."""
    params = {"type": "trading", "year": str(year)}
    own_client = client is None
    http = client or httpx.Client(headers=BROWSER_HEADERS, timeout=20.0)
    try:
        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = http.get(NSE_HOLIDAY_MASTER_URL, params=params, headers=BROWSER_HEADERS)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise NseFetchError("NSE holiday-master did not return JSON") from exc
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(f"NSE holiday-master returned HTTP {response.status_code}")
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
        raise NseFetchError(f"NSE holiday-master failed after {attempts} attempts ({last_problem})")
    finally:
        if own_client:
            http.close()


class EquityRow(BaseModel):
    """One row from EQUITY_L.csv."""

    model_config = ConfigDict(frozen=True)

    isin: str
    symbol: str
    name: str
    series: str
    listing_date: dt.date | None


def fetch_equity_list(
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Raw CSV text of EQUITY_L.csv. Same retry logic as the holiday-master fetch."""
    own_client = client is None
    http = client or httpx.Client(headers=BROWSER_HEADERS, timeout=30.0)
    try:
        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = http.get(EQUITY_LIST_URL, headers=BROWSER_HEADERS)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(f"NSE EQUITY_L.csv returned HTTP {response.status_code}")
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
        raise NseFetchError(f"NSE EQUITY_L.csv failed after {attempts} attempts ({last_problem})")
    finally:
        if own_client:
            http.close()


def parse_equity_list(text: str) -> list[EquityRow]:
    """Parse EQUITY_L.csv text into a list of EquityRow.

    Strips leading/trailing spaces from headers and values, validates ISINs, rejects
    duplicates and empty files.
    """
    text = text.strip()
    if not text:
        raise NseFormatError("EQUITY_L.csv is empty")
    reader = csv.DictReader(io.StringIO(text))
    # Strip spaces from field names (NSE headers have inconsistent spacing).
    if reader.fieldnames is None:
        raise NseFormatError("EQUITY_L.csv has no headers")
    clean_fields = [f.strip() for f in reader.fieldnames]
    required = {"SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER", "DATE OF LISTING"}
    missing = required - set(clean_fields)
    if missing:
        raise NseFormatError(f"EQUITY_L.csv missing columns: {missing}")

    rows: list[EquityRow] = []
    seen_isins: set[str] = set()
    for raw in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in raw.items()}
        isin = row.get("ISIN NUMBER", "")
        if not _ISIN_RE.match(isin):
            raise NseFormatError(f"EQUITY_L.csv invalid ISIN: {isin!r}")
        if isin in seen_isins:
            raise NseFormatError(f"EQUITY_L.csv duplicate ISIN: {isin}")
        seen_isins.add(isin)
        date_str = row.get("DATE OF LISTING", "")
        try:
            listing_date: dt.date | None = dt.datetime.strptime(date_str, "%d-%b-%Y").date()
        except ValueError:
            if date_str:
                raise NseFormatError(f"EQUITY_L.csv bad listing_date: {date_str!r}") from None
            listing_date = None
        rows.append(
            EquityRow(
                isin=isin,
                symbol=row["SYMBOL"],
                name=row["NAME OF COMPANY"],
                series=row["SERIES"],
                listing_date=listing_date,
            )
        )
    if not rows:
        raise NseFormatError("EQUITY_L.csv is empty")
    return rows


def parse_trading_holidays(
    payload: dict[str, Any], year: int, segment: str = EQUITY_SEGMENT
) -> list[NseHoliday]:
    """Equity-segment entries for `year`, validated: wrong year, empty list, bad dates or
    duplicates raise rather than produce a quietly wrong calendar."""
    rows = payload.get(segment) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise NseFormatError(f"holiday-master has no '{segment}' entries for {year}")
    entries: dict[dt.date, NseHoliday] = {}
    for row in rows:
        try:
            day = dt.datetime.strptime(str(row["tradingDate"]).strip(), "%d-%b-%Y").date()
            description = str(row["description"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise NseFormatError(f"unreadable holiday-master row: {row!r}") from exc
        if day.year != year:
            raise NseFormatError(f"holiday-master for {year} contains {day}; wrong year returned")
        if day in entries:
            raise NseFormatError(f"duplicate holiday-master date {day}")
        entries[day] = NseHoliday(
            date=day, description=description, is_session=bool(_SESSION_PATTERN.search(description))
        )
    return sorted(entries.values(), key=lambda e: e.date)
