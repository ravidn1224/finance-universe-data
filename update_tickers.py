#!/usr/bin/env python3
"""Rebuild ``clean_tickers.txt`` from the official exchange listings.

Takes every common stock on the NASDAQ Trader listing files, ranks them by
average daily dollar volume, and keeps the most heavily traded. The list then
maintains itself: newly active companies rise into it and delisted or dormant
ones drop out, while its size stays matched to the Alpha Vantage budget that
has to supply fundamentals for it.

    python update_tickers.py --dry-run
    python update_tickers.py --limit 1000
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

import requests

from universe import config, liquidity, log, symbols

DEFAULT_LIMIT = 1000

#: The rebuilt list may not fall below this fraction of the smaller of the
#: current list and the target. Yahoo returning little or nothing must leave
#: the existing universe alone rather than truncate it.
SAFETY_FLOOR = 0.8


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"How many symbols to keep (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=liquidity.DEFAULT_BATCH_SIZE,
        help=f"Tickers per batch (default: {liquidity.DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=liquidity.DEFAULT_THREADS,
        help=(
            "Concurrent downloads within a batch "
            f"(default: {liquidity.DEFAULT_THREADS}); lower it if Yahoo throttles"
        ),
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=liquidity.DEFAULT_PAUSE_SECONDS,
        help="Seconds between batches, to stay under Yahoo's rate limit",
    )
    parser.add_argument(
        "--no-shortlist",
        action="store_true",
        help="Measure every listed symbol against Yahoo instead of shortlisting "
        "first. Much slower, and only useful if the screener looks wrong.",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Rank on the screener's latest session alone, skipping the Yahoo "
        "average. Seconds rather than a minute, but noisier.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report the result without writing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write even if the result trips the shrinkage guard",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1:
        log.error("--limit must be at least 1")
        return 2

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

    candidates = symbols.build_universe(listings)
    if not candidates:
        log.error("Listings produced no common stocks; refusing to continue")
        return 1

    existing: list[str] = []
    if config.TICKERS_FILE.exists():
        existing = symbols.read_symbols(config.TICKERS_FILE)

    log.info(f"{len(candidates)} listed common stock(s)")

    screened: dict[str, float] = {}
    if not args.no_shortlist:
        try:
            screened = liquidity.screener_dollar_volumes()
        except requests.RequestException as exc:
            log.warn(f"Screener unavailable ({exc})")
        except ValueError as exc:
            log.warn(f"Could not parse the screener response ({exc})")
        if screened:
            log.info(f"Screener priced {len(screened)} symbol(s) in one request")
        else:
            log.warn("Screener gave nothing usable; measuring the full listing")

    if args.quick:
        if not screened:
            log.error("--quick needs the screener, which returned nothing usable")
            return 1
        eligible = set(candidates)
        volumes: dict[str, float] = {
            symbol: traded
            for symbol, traded in screened.items()
            if symbol in eligible
        }
        pool = candidates
    else:
        pool = (
            liquidity.shortlist(
                candidates, screened, limit=args.limit, keep=existing
            )
            if screened
            else list(candidates)
        )
        batches = -(-len(pool) // max(1, args.batch_size))
        log.info(
            f"Measuring {len(pool)} contender(s) over {batches} batch(es) of "
            f"{args.batch_size}, {args.threads} at a time"
        )

        def progress(done: int, total: int) -> None:
            if done % 10 == 0 or done == total:
                log.detail(f"  {done}/{total} batches")

        volumes = liquidity.fetch_dollar_volumes(
            pool,
            batch_size=args.batch_size,
            pause_seconds=args.pause,
            threads=args.threads,
            on_progress=progress,
        )
        log.info(f"Got volume data for {len(volumes)} of {len(pool)} symbol(s)")

    selected = liquidity.select_universe(
        pool, volumes, limit=args.limit, existing=existing
    )

    floor = int(min(len(existing) or args.limit, args.limit) * SAFETY_FLOOR)
    if len(selected) < floor and not args.force:
        log.error(
            f"Only {len(selected)} symbol(s) selected, below the safety floor of "
            f"{floor}. Yahoo likely rate-limited the run; leaving "
            f"{config.TICKERS_FILE.name} unchanged. Use --force to override."
        )
        return 1

    added = sorted(set(selected) - set(existing))
    removed = sorted(set(existing) - set(selected))

    if args.dry_run:
        log.info(f"Dry run: would keep {len(selected)} symbol(s)")
        log.info(f"  {len(added)} added, {len(removed)} removed")
        if added:
            log.detail(f"  added: {', '.join(added[:15])}{' ...' if len(added) > 15 else ''}")
        if removed:
            log.detail(f"  removed: {', '.join(removed[:15])}{' ...' if len(removed) > 15 else ''}")
        return 0

    symbols.write_symbols(config.TICKERS_FILE, selected)
    log.success(f"Wrote {len(selected)} symbols to {config.TICKERS_FILE}")
    if existing:
        log.info(f"{len(added)} added, {len(removed)} removed")
        if added:
            log.detail(f"  added: {', '.join(added[:15])}{' ...' if len(added) > 15 else ''}")
        if removed:
            log.detail(f"  removed: {', '.join(removed[:15])}{' ...' if len(removed) > 15 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
