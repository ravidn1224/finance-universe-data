#!/usr/bin/env python3
"""Build ``master_stocks.csv`` from the cached company overviews.

Every ticker in the universe gets a row; symbols not yet cached are emitted
with blank fields so the file shape stays stable while the cache fills up.
"""

from __future__ import annotations

import sys
from typing import Sequence

import pandas as pd

from universe import config, log, store, symbols


def title_case(value: str) -> str:
    """Normalise Alpha Vantage's shouty sector/industry labels."""
    return value.title() if value else ""


def build_frame(tickers: Sequence[str], cache: store.Cache) -> pd.DataFrame:
    rows = []
    hits = 0
    for raw in tickers:
        symbol = symbols.normalize_symbol(raw)
        entry = cache.get(symbol)
        if entry is not None and store.is_usable(entry):
            row = store.to_master_row(entry)
            # Report when this row's data last moved rather than when the file
            # was built, so an unchanged dataset produces an unchanged CSV.
            row["last_updated"] = store.last_updated(entry)
            hits += 1
        else:
            row = store.to_master_row({"symbol": symbol, "status": store.STATUS_NOT_FOUND})
            row["last_updated"] = ""
        row["symbol"] = symbol
        rows.append(row)

    frame = pd.DataFrame(rows, columns=list(config.MASTER_COLUMNS))
    frame["sector"] = frame["sector"].map(title_case)
    frame["industry"] = frame["industry"].map(title_case)

    coverage = (hits / len(rows) * 100) if rows else 0.0
    log.info(f"Cache coverage: {hits}/{len(rows)} symbols ({coverage:.1f}%)")
    return frame


def main(argv: Sequence[str] | None = None) -> int:
    log.info("Building master CSV")

    cache = store.load_cache(config.CACHE_FILE)
    if not cache:
        log.warn(f"{config.CACHE_FILE} is missing or empty; rows will be blank")

    try:
        tickers = symbols.read_symbols(config.TICKERS_FILE)
    except FileNotFoundError:
        log.error(f"Ticker file not found: {config.TICKERS_FILE}")
        log.error("Run `python update_tickers.py` first.")
        return 1

    frame = build_frame(tickers, cache)
    config.MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(config.MASTER_FILE, index=False, lineterminator="\n")

    log.success(f"Wrote {len(frame)} rows to {config.MASTER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
