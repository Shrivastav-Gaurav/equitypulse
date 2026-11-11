"""NSE surveillance lists (ASM, GSM, ESM) from the daily regulatory indicator file.

Source: NSE's official `REG1_IND{DDMMYY}.csv` in the archives (verified live 2026-11-11):
  https://nsearchives.nseindia.com/content/cm/REG1_IND111126.csv
One row per scrip, keyed by SYMBOL (no ISIN). Columns used:
  GSM, Long_Term_Additional_Surveillance_Measure (Long Term ASM),
  Short_Term_Additional_Surveillance_Measure (Short Term ASM), ESM
Each holds a stage code; `100` means "not on this list". GSM stage 0 is a real stage.
The file is published after the close, so today's file may not exist yet.

The earlier `/api/reportsmf?index=...` endpoints do not exist (HTTP 404).
"""

import csv
import datetime as dt
import io
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from equitypulse.sources.nse import BROWSER_HEADERS, RETRYABLE_STATUS, NseFetchError, NseFormatError

SOURCE = "NSE_REG_IND"
NOT_LISTED = "100"

_NSE_HOME = "https://www.nseindia.com"
REG_IND_URL = "https://nsearchives.nseindia.com/content/cm/REG1_IND{ddmmyy}.csv"

_COL_SYMBOL = "Symbol"
_COL_SERIES = "Series"
_COL_GSM = "GSM"
_COL_ASM_LONG = "Long_Term_Additional_Surveillance_Measure (Long Term ASM)"
_COL_ASM_SHORT = "Short_Term_Additional_Surveillance_Measure (Short Term ASM)"
_COL_ESM = "ESM"
_REQUIRED = (_COL_SYMBOL, _COL_SERIES, _COL_GSM, _COL_ASM_LONG, _COL_ASM_SHORT, _COL_ESM)
_MIN_ROWS = 1000  # the real file has ~3,200 rows; far fewer means a truncated download


class SurveillanceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    list_type: str  # "ASM", "GSM", or "ESM"
    stage: str


def reg_ind_url(as_of: dt.date) -> str:
    return REG_IND_URL.format(ddmmyy=as_of.strftime("%d%m%y"))


def fetch_reg_ind(
    as_of: dt.date,
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Download the regulatory indicator CSV for `as_of`; retries network errors and 5xx."""
    url = reg_ind_url(as_of)
    with httpx.Client(headers=BROWSER_HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            client.get(_NSE_HOME)  # session cookie; best-effort
        except httpx.TransportError:
            pass
        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = client.get(url)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    return response.content.decode("utf-8-sig")
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(f"NSE {url} returned HTTP {response.status_code}")
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
    raise NseFetchError(f"NSE {url} failed after {attempts} attempts ({last_problem})")


def parse_reg_ind(text: str, *, min_rows: int = _MIN_ROWS) -> list[dict[str, str]]:
    """Keep only the columns we use. Raises NseFormatError on a missing column or short file."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [f.strip() for f in (reader.fieldnames or [])]
    missing = [c for c in _REQUIRED if c not in fields]
    if missing:
        raise NseFormatError(f"REG1_IND is missing columns: {missing}")
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        if not row.get(_COL_SYMBOL):
            continue
        rows.append({c: row.get(c, "") for c in _REQUIRED})
    if len(rows) < min_rows:
        raise NseFormatError(f"REG1_IND has only {len(rows)} rows; refusing a truncated file")
    return rows


@lru_cache(maxsize=4)
def _rows_for(as_of: dt.date) -> tuple[dict[str, str], ...]:
    return tuple(parse_reg_ind(fetch_reg_ind(as_of)))


def fetch_surveillance(list_type: str, as_of: dt.date) -> list[dict[str, str]]:
    """Raw rows of the regulatory file for `as_of` (cached per date within a process)."""
    if list_type not in ("ASM", "GSM", "ESM"):
        raise ValueError(f"Unknown surveillance list type: {list_type!r}")
    return list(_rows_for(as_of))


def parse_surveillance(rows: list[dict[str, Any]], list_type: str) -> list[SurveillanceEntry]:
    """Entries for one list. ASM combines long- and short-term stages, e.g. "LT1", "LT4+ST1"."""
    entries: list[SurveillanceEntry] = []
    for row in rows:
        symbol = str(row.get(_COL_SYMBOL, "")).strip()
        if not symbol:
            continue
        if list_type == "GSM":
            stage = str(row.get(_COL_GSM, NOT_LISTED)).strip()
        elif list_type == "ESM":
            stage = str(row.get(_COL_ESM, NOT_LISTED)).strip()
        elif list_type == "ASM":
            parts = []
            for prefix, col in (("LT", _COL_ASM_LONG), ("ST", _COL_ASM_SHORT)):
                code = str(row.get(col, NOT_LISTED)).strip()
                if code and code != NOT_LISTED:
                    parts.append(f"{prefix}{code}")
            stage = "+".join(parts)
        else:
            raise ValueError(f"Unknown surveillance list type: {list_type!r}")
        if stage and stage != NOT_LISTED:
            entries.append(SurveillanceEntry(symbol=symbol, list_type=list_type, stage=stage))
    return entries
