"""Nifty index constituent CSV downloads from niftyindices.com.

The URL pattern (verified 2026-11-11):
  https://niftyindices.com/IndexConstituent/ind_nifty500list.csv
  https://niftyindices.com/IndexConstituent/ind_nifty50list.csv
  ...etc.

Each CSV has columns: Company Name, Industry, Symbol, Series, ISIN Code
"""

import csv
import io
import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ConfigDict

from equitypulse.sources.nse import BROWSER_HEADERS, RETRYABLE_STATUS, NseFetchError, NseFormatError

_BASE_URL = "https://niftyindices.com/IndexConstituent"

# Map index name to file name on niftyindices.com.
INDEX_FILES = {
    "NIFTY500": "ind_nifty500list.csv",
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTYMIDCAP150": "ind_niftymidcap150list.csv",
    "NIFTYSMALLCAP250": "ind_niftysmallcap250list.csv",
}

SOURCE = "NIFTYINDICES"


class IndexConstituent(BaseModel):
    model_config = ConfigDict(frozen=True)

    isin: str
    symbol: str
    name: str
    industry: str | None


def fetch_index_csv(
    index_name: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return raw CSV text for the given index."""
    filename = INDEX_FILES.get(index_name)
    if filename is None:
        raise ValueError(f"Unknown index: {index_name!r}")
    url = f"{_BASE_URL}/{filename}"
    own_client = True
    http = httpx.Client(headers=BROWSER_HEADERS, timeout=30.0)
    try:
        last_problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = http.get(url, headers=BROWSER_HEADERS)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(
                        f"niftyindices.com {index_name} returned HTTP {response.status_code}"
                    )
                last_problem = f"HTTP {response.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
        raise NseFetchError(
            f"niftyindices.com {index_name} failed after {attempts} attempts ({last_problem})"
        )
    finally:
        if own_client:
            http.close()


def parse_index_csv(text: str, index_name: str) -> list[IndexConstituent]:
    """Parse a niftyindices.com constituent CSV. Raises NseFormatError on bad data."""
    text = text.strip()
    if not text:
        raise NseFormatError(f"{index_name} constituent CSV is empty")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise NseFormatError(f"{index_name} constituent CSV has no headers")
    fields = {f.strip() for f in reader.fieldnames}
    if "ISIN Code" not in fields:
        raise NseFormatError(f"{index_name} constituent CSV missing 'ISIN Code' column")
    rows: list[IndexConstituent] = []
    seen: set[str] = set()
    for raw in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in raw.items()}
        isin = row.get("ISIN Code", "")
        if not isin or len(isin) != 12:
            continue
        if isin in seen:
            raise NseFormatError(f"{index_name} constituent CSV duplicate ISIN: {isin}")
        seen.add(isin)
        rows.append(
            IndexConstituent(
                isin=isin,
                symbol=row.get("Symbol", ""),
                name=row.get("Company Name", ""),
                industry=row.get("Industry") or None,
            )
        )
    if not rows:
        raise NseFormatError(f"{index_name} constituent CSV is empty")
    return rows
