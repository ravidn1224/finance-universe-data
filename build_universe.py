#!/usr/bin/env python3
"""Publish ``universe.csv``: the browsable ticker directory for the sheet.

Every NASDAQ, NYSE, NYSE American and NYSE Arca listing, flagged with S&P 500
membership and enriched with sector, market cap, price, P/E and the 150-day
average. The Google Sheet downloads this one file and writes it straight to a
tab, which is the whole point: the spreadsheet no longer calls the exchange
directories or the NASDAQ screener itself, so its build cannot stall on a slow
third-party host and die at the six-minute Apps Script limit.

    python build_universe.py
    python build_universe.py --stocks-only --dry-run
"""

from __future__ import annotations

import argparse
import csv
import sys
from typing import Sequence

import requests

from universe import config, liquidity, log, sheet, store, symbols


def fetch_text(url: str, *, timeout: float = 30.0) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def format_cell(value: object) -> object:
    """Render one cell compactly.

    Missing numbers become empty cells -- blank beats zero here, because the
    sheet filters these columns numerically and a zero would read as a real
    value. Floats are trimmed rather than written at full binary precision,
    which keeps the published file (and the sheet's parse of it) small.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 6)
    return value


def write_universe(path, rows: Sequence[Sequence[object]]) -> None:
    """Write the CSV the sheet downloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(sheet.SHEET_COLUMNS)
        for row in rows:
            writer.writerow([format_cell(value) for value in row])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stocks-only",
        action="store_true",
        help="Exclude ETFs. No free source classifies funds by sector, so "
        "their rows are largely empty.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report the result without writing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log.info("Building the ticker universe")

    try:
        nasdaq_text = fetch_text(symbols.NASDAQ_LISTED_URL)
        other_text = fetch_text(symbols.OTHER_LISTED_URL)
    except requests.RequestException as exc:
        log.error(f"Could not download the exchange listings: {exc}")
        return 1

    # The index list is small and reliable; without it the S&P column and the
    # stricter GICS sectors would both be missing, so a failure here is fatal.
    try:
        sp500 = sheet.parse_sp500(fetch_text(config.SP500_CONSTITUENTS_URL))
    except requests.RequestException as exc:
        log.error(f"Could not download the S&P 500 constituents: {exc}")
        return 1
    log.info(f"{len(sp500)} S&P 500 members")

    # Both enrichment sources are optional: the universe still builds without
    # them, just with more blanks, which beats failing outright.
    screener_rows = []
    try:
        screener_rows = liquidity.fetch_screener_rows()
    except (requests.RequestException, ValueError) as exc:
        log.warn(f"Screener unavailable ({exc}); sectors and caps will be sparser")
    if screener_rows:
        log.info(f"Screener returned {len(screener_rows)} rows")
    else:
        log.warn("Screener gave nothing usable")

    cache = store.load_cache(config.CACHE_FILE)
    if not cache:
        log.warn(f"{config.CACHE_FILE} is empty; no P/E or 150-day averages")

    enrichment = sheet.merge_enrichment(
        sheet.parse_screener(screener_rows),
        sheet.enrichment_from_cache(cache),
    )

    rows = sheet.build_rows(
        nasdaq_text, other_text, sp500, enrichment, stocks_only=args.stocks_only
    )
    if not rows:
        log.error("The listings produced no rows; refusing to publish")
        return 1

    filled = sheet.coverage(rows)
    log.info(
        f"{len(rows)} rows  |  "
        + "  ".join(f"{name} {count}" for name, count in filled.items())
    )

    if args.dry_run:
        log.info("Dry run: nothing written")
        for row in rows[:5]:
            log.detail(f"  {row}")
        return 0

    write_universe(config.UNIVERSE_FILE, rows)
    log.success(f"Wrote {len(rows)} rows to {config.UNIVERSE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
