"""NSE price and delivery files before the UDiFF format (verified live 2026-11-23 on 2019-01-02,
2021-06-15 and 2024-07-05).

Prices: content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip
  SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,
  (TIMESTAMP is DD-MON-YYYY, or DD-Mon-YY in some 2020 files; a trailing comma gives an
  empty last column; ISIN present since 2019.)
Delivery: `sec_bhavdata_full` where it exists (it is missing for 2019); otherwise the
  security-wise delivery position file archives/equities/mto/MTO_{DDMMYYYY}.DAT:
    4 header lines (the second, "10,MTO,DDMMYYYY,...", carries the date; the third repeats it
    as "Trade Date <02-JAN-2019>", misspelt "rade Date" in some files), then rows
    20,<sr no>,<symbol>,<series>,<qty traded>,<deliverable qty>,<% deliverable>
  It covers the rolling settlement only. BE/BZ rows get no delivery figure from either file
  (sec_bhavdata_full prints "-" for them), so their delivery is left empty, never guessed.
The two delivery files agree where both exist (tests/test_nse_legacy.py).
"""

import csv
import datetime as dt
import io
import re
import zipfile

from equitypulse.sources.nse import (
    PRICE_SERIES,
    DeliveryRow,
    NseFetchError,
    NseFormatError,
    UDiffRow,
    _fetch_with_retry,
)

UDIFF_START = dt.date(2024, 7, 8)  # first date the price loader reads the UDiFF file
_ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
_TRADE_DATE = re.compile(r"T?rade Date <(\d{2}-[A-Z]{3}-\d{4})>")  # NSE sometimes drops the T
_RECORD_10 = re.compile(r"^10,MTO,(\d{8}),")
_REQUIRED = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY",
             "TOTTRDVAL", "TIMESTAMP", "ISIN"}


def LEGACY_BHAVCOPY_URL(date: dt.date) -> str:  # noqa: N802
    mon = date.strftime("%b").upper()
    return (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{date.year}/{mon}/"
        f"cm{date.strftime('%d')}{mon}{date.year}bhav.csv.zip"
    )


def MTO_URL(date: dt.date) -> str:  # noqa: N802
    return f"https://nsearchives.nseindia.com/archives/equities/mto/MTO_{date.strftime('%d%m%Y')}.DAT"


def fetch_legacy_bhavcopy(date: dt.date) -> str:
    raw = _fetch_with_retry(LEGACY_BHAVCOPY_URL(date), label=f"legacy bhavcopy {date}").content
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise NseFetchError(f"legacy bhavcopy zip for {date} contains no CSV")
            return zf.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise NseFetchError(f"legacy bhavcopy for {date} is not a zip file") from exc


def fetch_mto(date: dt.date) -> str:
    return _fetch_with_retry(MTO_URL(date), label=f"MTO delivery {date}").text


def _legacy_date(raw: str) -> dt.date:
    """TIMESTAMP is usually 02-JAN-2019; some files (e.g. 2020-07-13) write 13-Jul-20."""
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(raw.title(), fmt).date()
        except ValueError:
            continue
    raise NseFormatError(f"legacy bhavcopy bad TIMESTAMP {raw!r}")


def parse_legacy_bhavcopy(text: str, expected_date: dt.date) -> list[UDiffRow]:
    """PRICE_SERIES rows (EQ/BE/BZ), one per ISIN, EQ winning if an ISIN is in two series."""
    reader = csv.DictReader(io.StringIO(text.strip().lstrip("﻿")))
    fields = {f.strip() for f in (reader.fieldnames or []) if f}
    if missing := _REQUIRED - fields:
        raise NseFormatError(f"legacy bhavcopy missing columns: {sorted(missing)}")
    by_isin: dict[str, UDiffRow] = {}
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        series = row["SERIES"]
        if series not in PRICE_SERIES:
            continue
        isin = row["ISIN"]
        if not _ISIN_RE.match(isin):
            raise NseFormatError(f"legacy bhavcopy invalid ISIN: {isin!r}")
        date = _legacy_date(row["TIMESTAMP"])
        if date != expected_date:
            raise NseFormatError(f"legacy bhavcopy for {expected_date} has rows dated {date}")
        prior = by_isin.get(isin)
        if prior is not None:
            if prior.series == series:
                raise NseFormatError(f"legacy bhavcopy duplicate ISIN: {isin}")
            if prior.series == "EQ":
                continue
        try:
            by_isin[isin] = UDiffRow(
                isin=isin, symbol=row["SYMBOL"], date=date, series=series,
                open=float(row["OPEN"]), high=float(row["HIGH"]), low=float(row["LOW"]),
                close=float(row["CLOSE"]),
                prev_close=float(row["PREVCLOSE"]) if row["PREVCLOSE"] else None,
                volume=int(row["TOTTRDQTY"]),
                traded_value=float(row["TOTTRDVAL"]) if row["TOTTRDVAL"] else None,
            )
        except ValueError as exc:
            raise NseFormatError(f"legacy bhavcopy bad row for {isin}: {exc}") from exc
    if not by_isin:
        raise NseFormatError("legacy bhavcopy has no EQ/BE/BZ rows")
    return list(by_isin.values())


def _mto_date(header: list[str]) -> dt.date:
    for line in header:
        if m := _RECORD_10.match(line.strip()):
            return dt.datetime.strptime(m.group(1), "%d%m%Y").date()
    for line in header:
        if m := _TRADE_DATE.search(line):
            return dt.datetime.strptime(m.group(1).title(), "%d-%b-%Y").date()
    raise NseFormatError("MTO file has no date header")


def parse_mto(text: str, expected_date: dt.date) -> dict[tuple[str, str], DeliveryRow]:
    """Delivery keyed by (symbol, series), like parse_bhavcopy_delivery."""
    lines = text.strip().splitlines()
    trade_date = _mto_date(lines[:5])
    if trade_date != expected_date:
        raise NseFormatError(f"MTO file for {expected_date} is dated {trade_date}")
    out: dict[tuple[str, str], DeliveryRow] = {}
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7 or parts[0] != "20" or parts[3] not in PRICE_SERIES:
            continue
        symbol, series = parts[2], parts[3]
        try:
            qty = int(parts[5])
            pct = float(parts[6]) / 100.0
        except ValueError:
            qty, pct = None, None  # "NA" and similar: no figure, never a guess
        out[(symbol, series)] = DeliveryRow(
            symbol=symbol, series=series, delivery_qty=qty, delivery_pct=pct
        )
    if not out:
        raise NseFormatError("MTO file has no delivery rows")
    return out


def price_file_exists(date: dt.date) -> bool:
    """Whether NSE published a price file for `date` (the format that date is loaded from)."""
    from equitypulse.sources.nse import BHAVCOPY_UDIFF_URL, NseFileMissing

    url = LEGACY_BHAVCOPY_URL(date) if date < UDIFF_START else BHAVCOPY_UDIFF_URL(date)
    try:
        _fetch_with_retry(url, label=f"price file {date}")
    except NseFileMissing:
        return False
    return True
