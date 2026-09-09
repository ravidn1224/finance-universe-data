#!/usr/bin/env python3
"""Refresh prices and market caps for the whole universe from Yahoo Finance.

Unlike the Alpha Vantage step this has no meaningful daily budget, so every
cached symbol is updated on every run -- the volatile columns become genuinely
daily rather than cycling once a month.

    python update_quotes.py
    python update_quotes.py --dry-run --limit 20
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from universe import config, log, quotes, store, symbols


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--batch-size",
        type=int,
        default=quotes.DEFAULT_BATCH_SIZE,
        help=f"Tickers per batch (default: {quotes.DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=quotes.DEFAULT_THREADS,
        help=(
            "Concurrent downloads within a batch "
            f"(default: {quotes.DEFAULT_THREADS}); lower it if Yahoo throttles"
        ),
    )
    parser.add_argument("--limit", type=int, help="Only process the first N symbols")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report without writing the cache",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    cache = store.load_cache(config.CACHE_FILE)
    if not cache:
        log.error(f"{config.CACHE_FILE} is empty; run cache.py first")
        return 1

    try:
        universe = symbols.read_symbols(config.TICKERS_FILE)
    except FileNotFoundError:
        log.error(f"Ticker file not found: {config.TICKERS_FILE}")
        return 1

    # Only symbols with fundamentals are worth quoting: a quote alone would
    # produce a row with a price but no company name.
    targets = [
        sym for sym in universe if sym in cache and store.is_usable(cache[sym])
    ]
    if args.limit:
        targets = targets[: args.limit]

    batches = -(-len(targets) // max(1, args.batch_size))
    log.info(
        f"Fetching quotes for {len(targets)} symbol(s) in {batches} batch(es), "
        f"{args.threads} download(s) at a time"
    )
    found, failed = quotes.fetch_quotes(
        targets, cache, batch_size=args.batch_size, threads=args.threads
    )

    if failed:
        log.warn(f"{len(failed)} symbol(s) returned no quote: {', '.join(failed[:10])}"
                 f"{' ...' if len(failed) > 10 else ''}")

    if args.dry_run:
        log.info(f"Dry run: {len(found)} quote(s) fetched, cache not written")
        for quote in list(found.values())[:10]:
            log.detail(f"  {quote.symbol}: price={quote.price} cap={quote.market_cap}")
        return 0

    changed = quotes.apply_quotes(cache, found.values(), stamp=store.utc_now_iso())
    if changed:
        store.save_cache(config.CACHE_FILE, cache)
        log.success(f"Updated {changed} of {len(targets)} symbol(s) in {config.CACHE_FILE}")
    else:
        log.info("No quote changes; cache left untouched")

    return 0


if __name__ == "__main__":
    sys.exit(main())
