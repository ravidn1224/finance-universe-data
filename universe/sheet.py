"""Assemble the browsable ticker universe that the Google Sheet displays.

This work used to live in Apps Script, which had to call ``api.nasdaq.com``
from Google's servers. That host frequently stalls for those clients, and
Apps Script offers no request timeout, so the build regularly sat in a single
fetch until it hit the hard six-minute script limit and died. A CI runner
reaches the screener reliably and has no such deadline, so the assembly is
done here and the spreadsheet is left with one job: download a finished CSV
and paste it into a sheet.

The filtering and exchange rules below deliberately mirror what the sheet used
to do itself, so the published file is a drop-in replacement for that logic.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import store

#: NYSE-family exchange codes in otherlisted.txt, mapped to display names.
#: These venues are always included.
OTHER_EXCHANGE_CODES = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca"}

#: The remaining otherlisted.txt venues list almost nothing but funds, so a row
#: is kept only when it is an S&P 500 member -- without this, index members
#: such as CBOE would be missing from the universe entirely.
SP500_ONLY_EXCHANGE_CODES = {"Z": "Cboe", "V": "IEX"}

#: Prefixes GOOGLEFINANCE expects per exchange. Venues absent here are quoted
#: by bare symbol, which resolves fine for unambiguous US listings.
GF_EXCHANGE_PREFIX = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE American": "NYSEAMERICAN",
    "NYSE Arca": "NYSEARCA",
}

#: Symbols GOOGLEFINANCE can address: 1-5 letters with an optional class
#: suffix (BRK.B). Excludes preferred shares (ABR$D) and similar notations
#: that the listing files carry but no quote source will price.
VALID_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")

#: Columns of ``universe.csv``, in the order the sheet writes them.
SHEET_COLUMNS = (
    "symbol",
    "gf_ticker",
    "name",
    "exchange",
    "type",
    "sp500",
    "sector",
    "industry",
    "market_cap",
    "price",
    "pe",
    "sma150",
    "pct_above_sma",
)

#: Marks an S&P 500 member. The sheet filters on this exact glyph.
SP500_MARK = "\u2713"


def display_symbol(raw: object) -> str:
    """Canonical *display* form of a ticker, e.g. ``BRK.B``.

    The exchange files and GOOGLEFINANCE both use a dot for share classes,
    while Yahoo and the screener use a dash or slash. The sheet shows and
    quotes the dotted form, so that is what gets published; matching against
    the cache still goes through :func:`symbols.normalize_symbol`.
    """
    return str(raw or "").strip().upper().replace("-", ".").replace("/", ".")


def to_number(value: object) -> Optional[float]:
    """Parse a possibly display-formatted number, or ``None`` if it is not one.

    The screener formats figures for humans (``"$146.85"``, ``"1,234"``), and
    a zero market cap means "not reported" rather than a worthless company, so
    zero is treated as missing to keep it out of numeric filters.
    """
    text = re.sub(r"[$,\s%]", "", str(value if value is not None else "")).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if number == 0 else number


def parse_sp500(csv_text: str) -> dict[str, dict[str, str]]:
    """Parse the S&P 500 constituents CSV into a GICS lookup keyed by symbol."""
    import csv
    import io

    lookup: dict[str, dict[str, str]] = {}
    reader = csv.reader(io.StringIO(csv_text))
    next(reader, None)  # header: Symbol, Security, GICS Sector, GICS Sub-Industry

    for row in reader:
        if len(row) < 4:
            continue
        symbol = display_symbol(row[0])
        if symbol:
            lookup[symbol] = {
                "sector": row[2].strip(),
                "sub_industry": row[3].strip(),
            }
    return lookup


def parse_screener(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index screener rows by display symbol, keeping the fields the sheet shows."""
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = display_symbol(row.get("symbol"))
        if not symbol:
            continue
        lookup[symbol] = {
            "sector": str(row.get("sector") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "market_cap": to_number(row.get("marketCap")),
            "price": to_number(row.get("lastsale")),
        }
    return lookup


def enrichment_from_cache(cache: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project the Alpha Vantage/Yahoo cache onto display-symbol keys.

    This is the only source for P/E and the 150-day average; it covers the
    curated set the pipeline tracks rather than the whole market.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for key, entry in cache.items():
        if not store.is_usable(entry):
            continue
        symbol = display_symbol(entry.get("symbol") or key)
        if not symbol:
            continue
        lookup[symbol] = {
            "sector": str(entry.get("sector") or "").strip(),
            "industry": str(entry.get("industry") or "").strip(),
            "market_cap": to_number(entry.get("marketCap")),
            "price": to_number(entry.get("price")),
            "pe": to_number(entry.get("peRatio")),
            "sma150": to_number(entry.get("ma150")),
        }
    return lookup


def merge_enrichment(
    screener: Mapping[str, Mapping[str, Any]],
    pipeline: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Combine the two detail sources.

    The screener wins on the fields it carries because it covers the whole
    market, while the pipeline covers a curated subset. P/E and the 150-day
    average exist only in the pipeline, so they always come from there.
    """
    merged: dict[str, dict[str, Any]] = {}

    for symbol in set(screener) | set(pipeline):
        from_screener = screener.get(symbol) or {}
        from_pipeline = pipeline.get(symbol) or {}

        def pick(field: str) -> Any:
            value = from_screener.get(field)
            return value if value not in (None, "") else from_pipeline.get(field)

        merged[symbol] = {
            "sector": pick("sector") or "",
            "industry": pick("industry") or "",
            "market_cap": pick("market_cap"),
            "price": pick("price"),
            "pe": from_pipeline.get("pe"),
            "sma150": from_pipeline.get("sma150"),
        }
    return merged


def _listing_rows(text: str) -> list[list[str]]:
    """Split a pipe-delimited listing file into field rows, minus the trailer."""
    rows = []
    for line in text.splitlines()[1:]:
        if not line.strip() or line.startswith("File Creation Time"):
            continue
        rows.append(line.split("|"))
    return rows


def _build_row(
    symbol: str,
    name: str,
    exchange: str,
    is_etf: bool,
    member: Optional[Mapping[str, str]],
    extra: Optional[Mapping[str, Any]],
) -> list[Any]:
    """Compose one sheet row in :data:`SHEET_COLUMNS` order."""
    extra = extra or {}

    # GICS from the index list wins where it exists; it is the stricter
    # taxonomy. The market-wide sources fill in everything else.
    sector = (member or {}).get("sector") or extra.get("sector") or ""
    industry = (member or {}).get("sub_industry") or extra.get("industry") or ""

    price = extra.get("price")
    sma150 = extra.get("sma150")
    # Precomputed so the sheet never has to hold a formula for it. This is the
    # whole reason the column can exist at all at this scale.
    pct_above = (
        price / sma150 - 1
        if isinstance(price, float) and isinstance(sma150, float) and sma150 > 0
        else None
    )

    prefix = GF_EXCHANGE_PREFIX.get(exchange)
    return [
        symbol,
        f"{prefix}:{symbol}" if prefix else symbol,
        name,
        exchange,
        "ETF" if is_etf else "Stock",
        SP500_MARK if member else "",
        sector,
        industry,
        extra.get("market_cap"),
        price,
        extra.get("pe"),
        sma150,
        pct_above,
    ]


def parse_nasdaq_listed(
    text: str,
    sp500: Mapping[str, Mapping[str, str]],
    enrichment: Mapping[str, Mapping[str, Any]],
) -> list[list[Any]]:
    """Rows from nasdaqlisted.txt.

    Columns: Symbol|Security Name|Market Category|Test Issue|Financial Status|
    Round Lot|ETF|NextShares
    """
    rows = []
    for fields in _listing_rows(text):
        if len(fields) < 7 or fields[3].strip() == "Y":
            continue
        symbol = display_symbol(fields[0])
        if not VALID_SYMBOL.match(symbol):
            continue
        rows.append(
            _build_row(
                symbol,
                fields[1].strip(),
                "NASDAQ",
                fields[6].strip() == "Y",
                sp500.get(symbol),
                enrichment.get(symbol),
            )
        )
    return rows


def parse_other_listed(
    text: str,
    sp500: Mapping[str, Mapping[str, str]],
    enrichment: Mapping[str, Mapping[str, Any]],
) -> list[list[Any]]:
    """Rows from otherlisted.txt, keeping NYSE-family venues.

    Columns: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|
    Test Issue|NASDAQ Symbol
    """
    rows = []
    for fields in _listing_rows(text):
        if len(fields) < 7 or fields[6].strip() == "Y":
            continue
        symbol = display_symbol(fields[0])
        if not VALID_SYMBOL.match(symbol):
            continue

        member = sp500.get(symbol)
        code = fields[2].strip()
        exchange = OTHER_EXCHANGE_CODES.get(code)
        if exchange is None:
            if not member:
                continue
            exchange = SP500_ONLY_EXCHANGE_CODES.get(code, "Other")

        rows.append(
            _build_row(
                symbol,
                fields[1].strip(),
                exchange,
                fields[4].strip() == "Y",
                member,
                enrichment.get(symbol),
            )
        )
    return rows


def build_rows(
    nasdaq_text: str,
    other_text: str,
    sp500: Mapping[str, Mapping[str, str]],
    enrichment: Mapping[str, Mapping[str, Any]],
    *,
    stocks_only: bool = False,
) -> list[list[Any]]:
    """Merge both listing files into the sorted rows the sheet displays."""
    rows = parse_nasdaq_listed(nasdaq_text, sp500, enrichment)
    rows += parse_other_listed(other_text, sp500, enrichment)
    if stocks_only:
        rows = [row for row in rows if row[4] == "Stock"]
    rows.sort(key=lambda row: row[0])
    return rows


def coverage(rows: Sequence[Sequence[Any]]) -> dict[str, int]:
    """Count how many rows each source could actually fill, for the run log."""
    index = {name: position for position, name in enumerate(SHEET_COLUMNS)}
    fields = ("sp500", "sector", "market_cap", "price", "pe", "sma150")
    return {
        field: sum(1 for row in rows if row[index[field]] not in (None, ""))
        for field in fields
    }
