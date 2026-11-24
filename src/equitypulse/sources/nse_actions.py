"""NSE corporate actions and symbol changes (both free, verified live 2026-11-23).

Corporate actions: GET /api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY
&to_date=DD-MM-YYYY returns a JSON list (a full year in one call, ~1,900 rows). Fields used:
symbol, isin, series, exDate ("14-Jan-2026"), subject. Two quirks matter:
  - `symbol` is the company's CURRENT symbol, even for actions before a rename (ACUTAAS's
    25-Apr-2025 split is listed as ACUTAAS although it traded as AMIORG until 02-Jun-2025);
  - `isin` may be the new, the old or an older ISIN (KOTAKBANK's 2026 split carries
    INE237A01010, neither of the two ISINs that traded around it).
So actions are resolved to ISINs by issuer code and symbol, not by the `isin` field.

Only these subject shapes change the share count of the equity and carry a factor (after
normalising spacing, and splitting combined records such as "Bonus 1:2/Face Value Split ..."):
  "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
  "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share"
  "Bonus 1:10"
Anything else that mentions a split, bonus or consolidation (e.g. "Scheme Of Arrangement -
Bonus Ncrps 4:1", a bonus of preference shares) is kept as OTHER with no factor, never guessed.

Symbol changes: nsearchives.nseindia.com/content/equities/symbolchange.csv
  NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE   (e.g. ...,AMIORG,ACUTAAS,02-JUN-2025)
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

from equitypulse.sources.nse import BROWSER_HEADERS, RETRYABLE_STATUS, NseFetchError, NseFormatError

SOURCE_ACTIONS = "NSE_CORP_ACTIONS"
SOURCE_SYMBOLS = "NSE_SYMBOL_CHANGE"

_HOME = "https://www.nseindia.com/"
_ACTIONS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions?index=equities"
    "&from_date={start}&to_date={end}"
)
SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"

_FACE_VALUE = re.compile(
    r"^(?:Face Value Split \(Sub-Division\)(?: -)?|Consolidation Of Equity Shares)"
    r" From R[se] ([\d.]+)(?:/-)? Per Share To R[se] ([\d.]+)(?:/-)? Per Share$",
    re.IGNORECASE,
)
_BONUS = re.compile(r"^Bonus (\d+):(\d+)$", re.IGNORECASE)
_MENTIONS_SHARE_CHANGE = re.compile(r"split|bonus|consolidat", re.IGNORECASE)


class ParsedAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    isin: str
    ex_date: dt.date
    action_type: str  # SPLIT, BONUS, RIGHTS, DEMERGER, OTHER
    adj_factor: float | None  # multiply prices before ex_date by this; None = not adjusted
    subject: str


class SymbolChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    company: str
    old_symbol: str
    new_symbol: str
    effective: dt.date


def _normalise(subject: str) -> str:
    """Spacing and punctuation variants seen in 2018-2024 records, nothing else:
    "Rs10/-" and "Rs 10 /-" -> "Rs 10/-"; "Bonus- 1:2" and "Bonus 1: 1" -> "Bonus 1:2"."""
    s = " ".join(subject.split())
    s = re.sub(r"\s*/\s*-", "/-", s)
    s = re.sub(r"\bR([se])\.?\s*(?=\d)", r"R\1 ", s)
    s = re.sub(r"^Bonus\s*-\s*(?=\d)", "Bonus ", s, flags=re.IGNORECASE)
    return re.sub(r"(\d+)\s*:\s*(\d+)", r"\1:\2", s)


def parse_subject(subject: str) -> list[tuple[str, float | None]]:
    """Every action in a subject; [] when nothing is kept (dividends etc.).

    One record can hold several actions separated by "/" ("Bonus 1:2/Face Value Split ...",
    "Bonus 1:1 /Dividend- Rs 29 Per Share"); a "/-" is part of an amount, not a separator.
    A face-value change from F_old to F_new multiplies the share count by F_old / F_new, so
    earlier prices are multiplied by F_new / F_old (a consolidation gives a factor above 1).
    A bonus of a new shares for every b held multiplies prices by b / (a + b).
    """
    out = []
    for part in re.split(r"/(?!-)", _normalise(subject)):
        parsed = _parse_one(part.strip())
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_one(s: str) -> tuple[str, float | None] | None:
    if m := _FACE_VALUE.match(s):
        old, new = float(m.group(1)), float(m.group(2))
        if old <= 0 or new <= 0 or old == new:
            return "OTHER", None
        return "SPLIT", new / old
    if m := _BONUS.match(s):
        a, b = int(m.group(1)), int(m.group(2))
        return ("BONUS", b / (a + b)) if a > 0 and b > 0 else ("OTHER", None)
    if s.lower().startswith("rights"):
        return "RIGHTS", None
    if "demerger" in s.lower():
        return "DEMERGER", None
    if _MENTIONS_SHARE_CHANGE.search(s):
        return "OTHER", None
    return None


def parse_corporate_actions(rows: list[dict[str, Any]]) -> tuple[list[ParsedAction], int]:
    """Kept actions and the count of rows skipped (dividends, interest, buy-backs...)."""
    if not isinstance(rows, list):
        raise NseFormatError(f"corporate actions payload is not a list: {type(rows).__name__}")
    out: list[ParsedAction] = []
    skipped = 0
    for row in rows:
        try:
            subject = str(row["subject"])
            symbol = str(row["symbol"]).strip()
            isin = str(row["isin"]).strip()
            ex_raw = str(row["exDate"]).strip()
        except KeyError as exc:
            raise NseFormatError(f"corporate action row missing {exc}") from exc
        parsed = parse_subject(subject)
        if not parsed or ex_raw in ("", "-"):
            skipped += 1
            continue
        try:
            ex_date = dt.datetime.strptime(ex_raw, "%d-%b-%Y").date()
        except ValueError as exc:
            raise NseFormatError(f"corporate action bad exDate {ex_raw!r}") from exc
        for action_type, factor in parsed:
            out.append(ParsedAction(symbol=symbol, isin=isin, ex_date=ex_date,
                                    action_type=action_type, adj_factor=factor, subject=subject))
    return out, skipped


def parse_symbol_changes(text: str, *, min_rows: int = 100) -> list[SymbolChange]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [f.strip() for f in (reader.fieldnames or [])]
    need = {"NAME OF THE COMPANY", "OLD SYMBOL", "NEW SYMBOL", "EFFECTIVE DATE"}
    if not need <= set(fields):
        raise NseFormatError(f"symbolchange.csv missing columns: {sorted(need - set(fields))}")
    out: list[SymbolChange] = []
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        old, new = row["OLD SYMBOL"], row["NEW SYMBOL"]
        if not old or not new or old == new:
            continue
        try:
            effective = dt.datetime.strptime(row["EFFECTIVE DATE"].title(), "%d-%b-%Y").date()
        except ValueError as exc:
            raise NseFormatError(f"symbolchange.csv bad date {row['EFFECTIVE DATE']!r}") from exc
        out.append(SymbolChange(company=row["NAME OF THE COMPANY"], old_symbol=old,
                                new_symbol=new, effective=effective))
    if len(out) < min_rows:
        raise NseFormatError(f"symbolchange.csv has only {len(out)} rows; refusing a short file")
    return out


def _get(
    url: str,
    *,
    warm: tuple[str, ...],
    accept: str,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    with httpx.Client(headers=BROWSER_HEADERS, timeout=60.0, follow_redirects=True) as client:
        for page in warm:  # session cookies; best-effort
            try:
                client.get(page)
            except httpx.TransportError:
                pass
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                r = client.get(url, headers={**BROWSER_HEADERS, "Accept": accept})
            except httpx.TransportError as exc:
                last = f"network error: {exc.__class__.__name__}"
            else:
                if r.status_code == 200:
                    return r
                if r.status_code not in RETRYABLE_STATUS:
                    raise NseFetchError(f"NSE {url} returned HTTP {r.status_code}")
                last = f"HTTP {r.status_code}"
            if attempt < attempts:
                sleep(backoff_seconds * 2 ** (attempt - 1))
    raise NseFetchError(f"NSE {url} failed after {attempts} attempts ({last})")


def fetch_corporate_actions(start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    url = ACTIONS_URL.format(start=start.strftime("%d-%m-%Y"), end=end.strftime("%d-%m-%Y"))
    r = _get(url, warm=(_HOME, _ACTIONS_PAGE), accept="application/json")
    try:
        data = r.json()
    except ValueError as exc:
        raise NseFetchError(f"NSE corporate actions {start}..{end} did not return JSON") from exc
    if not isinstance(data, list):
        raise NseFormatError(f"corporate actions payload is not a list: {type(data).__name__}")
    return data


def fetch_symbol_changes() -> str:
    return _get(SYMBOL_CHANGES_URL, warm=(_HOME,), accept="text/csv").content.decode("utf-8-sig")
