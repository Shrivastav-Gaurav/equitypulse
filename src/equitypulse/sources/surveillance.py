"""NSE surveillance list downloads (ASM, GSM, ESM).

API endpoints (verified 2026-11-11):
  ASM long-term:   GET /api/reportsmf?index=shortterm  (despite name, includes LT-ASM)
  ASM short-term:  same endpoint, different listname values
  GSM:             GET /api/reportsmf?index=gsm
  ESM:             GET /api/reportsmf?index=esm

All three return a JSON array; each element has at minimum:
  ISIN, symbol, listname (or list_name), stage (GSM only), companyName

NSE requires a valid session cookie for JSON responses. The fetch helper first
hits the homepage to get a session cookie, then hits the data endpoint.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from equitypulse.sources.nse import BROWSER_HEADERS, RETRYABLE_STATUS, NseFetchError, NseFormatError

SOURCE = "NSE_SURVEILLANCE"

# Endpoints verified 2026-11-11.
_NSE_BASE = "https://www.nseindia.com"
_ENDPOINTS = {
    "ASM": f"{_NSE_BASE}/api/reportsmf?index=shortterm",
    "GSM": f"{_NSE_BASE}/api/reportsmf?index=gsm",
    "ESM": f"{_NSE_BASE}/api/reportsmf?index=esm",
}
_ISIN_FIELD_CANDIDATES = ("ISIN", "isin", "Isin")
_STAGE_FIELD_CANDIDATES = ("stage", "Stage", "stageId")


class SurveillanceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    isin: str
    symbol: str
    list_type: str  # "ASM", "GSM", or "ESM"
    stage: str | None


def _extract_isin(row: dict[str, Any]) -> str | None:
    for key in _ISIN_FIELD_CANDIDATES:
        v = row.get(key)
        if v and isinstance(v, str) and len(v.strip()) == 12:
            return v.strip()
    return None


def _extract_stage(row: dict[str, Any]) -> str | None:
    for key in _STAGE_FIELD_CANDIDATES:
        v = row.get(key)
        if v is not None:
            s = str(v).strip()
            return s if s else None
    return None


def _fetch_with_session(
    url: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Any]:
    """Fetch a JSON array from an NSE API endpoint, using a session cookie."""
    with httpx.Client(headers=BROWSER_HEADERS, timeout=30.0, follow_redirects=True) as client:
        # Warm up the session.
        try:
            client.get(_NSE_BASE)
        except httpx.TransportError:
            pass  # best-effort; the main request may still work

        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                json_headers = {**BROWSER_HEADERS, "Accept": "application/json"}
                response = client.get(url, headers=json_headers)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise NseFetchError(
                            f"NSE surveillance {url} did not return JSON"
                        ) from exc
                    if isinstance(data, list):
                        return data
                    # Sometimes the API wraps in a dict.
                    if isinstance(data, dict):
                        for key in ("data", "result", "records"):
                            if isinstance(data.get(key), list):
                                return data[key]
                    raise NseFetchError(f"NSE surveillance {url} unexpected shape: {type(data)}")
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(
                        f"NSE surveillance {url} returned HTTP {response.status_code}"
                    )
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
        raise NseFetchError(
            f"NSE surveillance {url} failed after {attempts} attempts ({last_problem})"
        )


def fetch_surveillance(
    list_type: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Any]:
    """Return the raw JSON array for `list_type` ("ASM", "GSM", or "ESM")."""
    url = _ENDPOINTS.get(list_type)
    if url is None:
        raise ValueError(f"Unknown surveillance list type: {list_type!r}")
    return _fetch_with_session(url, attempts=attempts, backoff_seconds=backoff_seconds, sleep=sleep)


def parse_surveillance(rows: list[Any], list_type: str) -> list[SurveillanceEntry]:
    """Parse a raw surveillance JSON array into typed entries.

    Skips rows with no ISIN (non-equity instruments). Raises NseFormatError
    if the entire list is empty.
    """
    if not isinstance(rows, list):
        raise NseFormatError(f"Surveillance {list_type} payload is not a list: {type(rows)}")
    entries: list[SurveillanceEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        isin = _extract_isin(row)
        if not isin:
            continue
        symbol = str(row.get("symbol") or row.get("Symbol") or "").strip()
        stage = _extract_stage(row)
        entries.append(
            SurveillanceEntry(isin=isin, symbol=symbol, list_type=list_type, stage=stage)
        )
    return entries
