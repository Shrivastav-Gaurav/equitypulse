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
import zipfile
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


# ── Bhavcopy (daily prices) ───────────────────────────────────────────────────
#
# Two files are needed per trading day because NSE splits the data:
#   UDiFF format  — has ISIN + all prices; no delivery data.
#   sec_bhavdata_full — has SYMBOL + delivery %; no ISIN.
# The loader joins them on SYMBOL (after filtering for the EQ series).


def BHAVCOPY_UDIFF_URL(date: dt.date) -> str:  # noqa: N802
    """UDiFF bhavcopy zip URL for `date` (e.g. 2026-11-11 → ...20261111...)."""
    d = date.strftime("%Y%m%d")
    return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip"


def BHAVCOPY_DELIVERY_URL(date: dt.date) -> str:  # noqa: N802
    """sec_bhavdata_full CSV URL for `date`."""
    d = date.strftime("%d%m%Y")
    return f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"


class UDiffRow(BaseModel):
    """One EQ row from the UDiFF bhavcopy CSV."""

    model_config = ConfigDict(frozen=True)

    isin: str
    symbol: str
    date: dt.date
    series: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None
    volume: int
    traded_value: float | None


class DeliveryRow(BaseModel):
    """Delivery data from sec_bhavdata_full for one stock."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    delivery_qty: int | None
    delivery_pct: float | None  # stored as fraction 0..1


def _fetch_with_retry(
    url: str,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "",
) -> httpx.Response:
    """GET `url`, retrying on network errors and 429/5xx. Returns the Response on 200."""
    own_client = client is None
    http = client or httpx.Client(headers=BROWSER_HEADERS, timeout=30.0)
    try:
        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = http.get(url, headers=BROWSER_HEADERS)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(f"NSE {label} returned HTTP {response.status_code}")
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
        raise NseFetchError(f"NSE {label} failed after {attempts} attempts ({last_problem})")
    finally:
        if own_client:
            http.close()


def _fetch_bhavcopy_udiff_bytes(
    date: dt.date,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Raw zip bytes for the UDiFF bhavcopy. Exposed for testing the URL construction."""
    url = BHAVCOPY_UDIFF_URL(date)
    response = _fetch_with_retry(
        url, client=client, attempts=attempts, backoff_seconds=backoff_seconds,
        sleep=sleep, label=f"UDiFF bhavcopy {date}"
    )
    return response.content


def fetch_bhavcopy_udiff(
    date: dt.date,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """CSV text from the UDiFF bhavcopy zip for `date`."""
    raw = _fetch_bhavcopy_udiff_bytes(
        date, client=client, attempts=attempts, backoff_seconds=backoff_seconds, sleep=sleep
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise NseFetchError(f"UDiFF bhavcopy zip for {date} contains no CSV")
        return zf.read(names[0]).decode("utf-8")


def fetch_bhavcopy_delivery(
    date: dt.date,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """CSV text of sec_bhavdata_full for `date`."""
    url = BHAVCOPY_DELIVERY_URL(date)
    response = _fetch_with_retry(
        url, client=client, attempts=attempts, backoff_seconds=backoff_seconds,
        sleep=sleep, label=f"delivery bhavcopy {date}"
    )
    return response.text


def parse_bhavcopy_udiff(text: str) -> list[UDiffRow]:
    """Parse UDiFF CSV; return only EQ-series rows. Raises NseFormatError on bad data."""
    text = text.strip().lstrip("﻿")  # strip BOM if present
    if not text:
        raise NseFormatError("UDiFF bhavcopy CSV is empty")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise NseFormatError("UDiFF bhavcopy has no headers")
    fields = {f.strip() for f in reader.fieldnames}
    required = {"ISIN", "TckrSymb", "SctySrs", "TradDt", "OpnPric", "HghPric", "LwPric",
                "ClsPric", "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal"}
    missing = required - fields
    if missing:
        raise NseFormatError(f"UDiFF bhavcopy missing columns: {missing}")

    rows: list[UDiffRow] = []
    seen: set[str] = set()
    for raw in reader:
        row = {k.strip(): v.strip() for k, v in raw.items()}
        if row.get("SctySrs") != "EQ":
            continue
        isin = row.get("ISIN", "")
        if not _ISIN_RE.match(isin):
            raise NseFormatError(f"UDiFF bhavcopy invalid ISIN: {isin!r}")
        if isin in seen:
            raise NseFormatError(f"UDiFF bhavcopy duplicate ISIN: {isin}")
        seen.add(isin)
        try:
            date = dt.date.fromisoformat(row["TradDt"])
        except ValueError:
            raise NseFormatError(f"UDiFF bhavcopy bad date: {row['TradDt']!r}") from None
        try:
            prev_close = float(row["PrvsClsgPric"]) if row.get("PrvsClsgPric") else None
            traded_value = float(row["TtlTrfVal"]) if row.get("TtlTrfVal") else None
            rows.append(
                UDiffRow(
                    isin=isin,
                    symbol=row["TckrSymb"],
                    date=date,
                    series=row["SctySrs"],
                    open=float(row["OpnPric"]),
                    high=float(row["HghPric"]),
                    low=float(row["LwPric"]),
                    close=float(row["ClsPric"]),
                    prev_close=prev_close,
                    volume=int(row["TtlTradgVol"]),
                    traded_value=traded_value,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NseFormatError(f"UDiFF bhavcopy bad row for {isin}: {exc}") from exc
    if not rows:
        raise NseFormatError("UDiFF bhavcopy CSV is empty")
    return rows


def parse_bhavcopy_delivery(text: str) -> dict[str, DeliveryRow]:
    """Parse sec_bhavdata_full CSV; return dict keyed by SYMBOL for EQ rows."""
    text = text.strip()
    if not text:
        raise NseFormatError("sec_bhavdata_full CSV is empty")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise NseFormatError("sec_bhavdata_full has no headers")
    result: dict[str, DeliveryRow] = {}
    for raw in reader:
        row = {k.strip(): v.strip() for k, v in raw.items()}
        if row.get("SERIES") != "EQ":
            continue
        symbol = row.get("SYMBOL", "")
        if not symbol:
            continue
        deliv_qty_str = row.get("DELIV_QTY", "-")
        deliv_pct_str = row.get("DELIV_PER", "-")
        try:
            delivery_qty = int(deliv_qty_str) if deliv_qty_str not in ("-", "") else None
            delivery_pct_raw = (
                float(deliv_pct_str) if deliv_pct_str not in ("-", "") else None
            )
            delivery_pct = delivery_pct_raw / 100.0 if delivery_pct_raw is not None else None
        except ValueError as exc:
            raise NseFormatError(
                f"sec_bhavdata_full bad delivery for {symbol}: {exc}"
            ) from exc
        result[symbol] = DeliveryRow(
            symbol=symbol, delivery_qty=delivery_qty, delivery_pct=delivery_pct
        )
    return result
