#!/usr/bin/env python3
"""Refresh ``clean_tickers.txt`` from the official NASDAQ Trader listings.

Combines the NASDAQ and other-listed files, drops test issues, warrants,
rights, units and preferred shares, and writes the sorted common-stock
universe.
"""

from __future__ import annotations

import sys
from typing import Sequence

import requests

from universe import config, log, symbols


def main(argv: Sequence[str] | None = None) -> int:
    log.info("Downloading exchange listings")

    try:
        listings = [
            symbols.download_listing(symbols.NASDAQ_LISTED_URL),
            symbols.download_listing(symbols.OTHER_LISTED_URL),
        ]
    except requests.RequestException as exc:
        log.error(f"Failed to download listings: {exc}")
        return 1
    except ValueError as exc:
        log.error(f"Failed to parse listings: {exc}")
        return 1

    universe = symbols.build_universe(listings)
    if not universe:
        log.error("Refusing to write an empty ticker universe")
        return 1

    previous: set[str] = set()
    if config.TICKERS_FILE.exists():
        previous = set(symbols.read_symbols(config.TICKERS_FILE))

    symbols.write_symbols(config.TICKERS_FILE, universe)

    added = sorted(set(universe) - previous)
    removed = sorted(previous - set(universe))
    log.success(f"Wrote {len(universe)} symbols to {config.TICKERS_FILE}")
    if previous:
        log.info(f"{len(added)} added, {len(removed)} removed")
        if added:
            log.detail(f"  added: {', '.join(added[:10])}{' ...' if len(added) > 10 else ''}")
        if removed:
            log.detail(
                f"  removed: {', '.join(removed[:10])}{' ...' if len(removed) > 10 else ''}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
