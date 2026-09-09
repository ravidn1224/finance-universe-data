#!/usr/bin/env python3
"""Spend the daily Alpha Vantage budget on company fundamentals.

Missing symbols are fetched first; the remaining budget refreshes the stalest
entries so nothing stays frozen. Prices and market caps are not fetched here --
``update_quotes.py`` refreshes those for the whole universe daily, which the
25-calls-per-day free tier could never do.

    ALPHAVANTAGE_API_KEY=... python cache.py
    ALPHAVANTAGE_API_KEY=... python cache.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Sequence

from universe import config, log, store, symbols
from universe.alphavantage import AlphaVantageClient, FetchResult, Outcome, mask_key

#: Pause applied when the API reports a per-minute frequency violation.
THROTTLE_BACKOFF_SECONDS = 60.0


@dataclass
class RunSummary:
    """Outcome tallies for a single run."""

    attempted: int = 0
    fetched: int = 0
    refreshed: int = 0
    not_found: int = 0
    errors: int = 0
    stopped_early: bool = False
    stop_reason: str = ""
    entries: store.Cache = field(default_factory=dict)


def plan_work(
    tickers: Sequence[str],
    cache: store.Cache,
    settings: config.FetchSettings,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[str], list[str]]:
    """Split the universe into symbols to fetch and symbols to refresh.

    Returns ``(missing, stale)``. Missing symbols come first when the budget is
    applied, because coverage matters more than freshness.
    """
    now = now or store.utc_now()
    missing: list[str] = []
    stale: list[tuple[float, str]] = []
    seen: set[str] = set()

    for raw in tickers:
        symbol = symbols.normalize_symbol(raw)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        entry = cache.get(symbol)
        if entry is None:
            missing.append(symbol)
            continue
        if not store.is_usable(entry):
            # A symbol Alpha Vantage does not know is only retried on request.
            if settings.retry_missing:
                missing.append(symbol)
            continue
        age = store.age_days(entry, now=now)
        if not settings.fill_only and age >= settings.refresh_after_days:
            stale.append((age, symbol))

    stale.sort(key=lambda item: (-item[0], item[1]))
    return missing, [symbol for _, symbol in stale]


def fill_cache(
    tickers: Sequence[str],
    cache: store.Cache,
    client: AlphaVantageClient,
    settings: config.FetchSettings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Optional[datetime] = None,
) -> RunSummary:
    """Fetch and refresh overviews within the configured call budget."""
    summary = RunSummary()
    missing, stale = plan_work(tickers, cache, settings, now=now)
    budget = (missing + stale)[: settings.max_calls]

    log.info(
        f"{len(missing)} missing, {len(stale)} stale "
        f"(older than {settings.refresh_after_days}d); "
        f"spending {len(budget)} of {settings.max_calls} call(s)"
    )
    if not budget:
        return summary

    known = set(missing)
    for index, symbol in enumerate(budget):
        kind = "fetch" if symbol in known else "refresh"
        log.detail(f"[{index + 1}/{len(budget)}] {kind} {symbol}")
        result = client.fetch_overview(symbol)
        summary.attempted += 1

        if result.outcome is Outcome.THROTTLED:
            log.warn(f"  Rate limited, backing off {THROTTLE_BACKOFF_SECONDS:.0f}s")
            sleep(THROTTLE_BACKOFF_SECONDS)
            result = client.fetch_overview(symbol)
            summary.attempted += 1

        if _record(result, summary, is_new=symbol in known):
            break

        if index < len(budget) - 1:
            sleep(settings.sleep_seconds)

    return summary


def _record(result: FetchResult, summary: RunSummary, *, is_new: bool) -> bool:
    """Apply a fetch result to the summary. Returns ``True`` to stop the run."""
    if result.outcome is Outcome.OK and result.entry is not None:
        summary.entries[result.symbol] = result.entry
        if is_new:
            summary.fetched += 1
        else:
            summary.refreshed += 1
        log.success(f"  {result.symbol}: {result.entry.get('name') or 'no name'}")
        return False

    if result.outcome is Outcome.NOT_FOUND:
        # Remembered so the symbol stops consuming quota on later runs.
        summary.entries[result.symbol] = store.make_entry(
            result.symbol, status=store.STATUS_NOT_FOUND
        )
        summary.not_found += 1
        log.warn(f"  {result.symbol}: no data ({result.message or 'unknown symbol'})")
        return False

    if result.outcome is Outcome.QUOTA_EXHAUSTED:
        summary.stopped_early = True
        summary.stop_reason = result.message or "daily API quota exhausted"
        log.error(f"  Daily quota exhausted: {summary.stop_reason}")
        return True

    if result.outcome is Outcome.THROTTLED:
        summary.stopped_early = True
        summary.stop_reason = "still rate limited after backoff"
        log.error(f"  {summary.stop_reason}; stopping")
        return True

    summary.errors += 1
    log.error(f"  {result.symbol}: {result.message}")
    return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-calls", type=int, help="Cap on API calls for this run")
    parser.add_argument("--sleep", type=float, help="Seconds to wait between calls")
    parser.add_argument(
        "--refresh-after-days",
        type=int,
        help="Refresh cached entries older than this many days",
    )
    parser.add_argument(
        "--fill-only",
        action="store_true",
        help="Only fetch missing symbols; never refresh cached ones",
    )
    parser.add_argument(
        "--retry-missing",
        action="store_true",
        help="Re-query symbols previously recorded as having no data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fetched without spending any quota",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        tickers = symbols.read_symbols(config.TICKERS_FILE)
    except FileNotFoundError:
        log.error(f"Ticker file not found: {config.TICKERS_FILE}")
        log.error("Run `python update_tickers.py` first.")
        return 1

    cache = store.load_cache(config.CACHE_FILE)

    if args.dry_run:
        settings = config.FetchSettings(
            max_calls=args.max_calls or 25,
            refresh_after_days=args.refresh_after_days or 180,
            fill_only=args.fill_only,
            retry_missing=args.retry_missing,
        )
        missing, stale = plan_work(tickers, cache, settings)
        log.info(f"{len(tickers)} tickers, {len(cache)} cached")
        log.info(f"would fetch {len(missing)} missing, refresh {len(stale)} stale")
        for symbol in (missing + stale)[: settings.max_calls]:
            log.detail(f"  {symbol}")
        return 0

    try:
        env = config.FetchSettings.from_env()
    except config.ConfigError as exc:
        log.error(str(exc))
        return 2

    settings = config.FetchSettings(
        max_calls=args.max_calls or env.max_calls,
        sleep_seconds=env.sleep_seconds if args.sleep is None else args.sleep,
        timeout_seconds=env.timeout_seconds,
        max_retries=env.max_retries,
        refresh_after_days=args.refresh_after_days or env.refresh_after_days,
        fill_only=args.fill_only or env.fill_only,
        retry_missing=args.retry_missing or env.retry_missing,
        api_key=env.api_key,
    )

    log.info(
        f"{len(tickers)} tickers, {len(cache)} cached, "
        f"key {mask_key(settings.api_key)}"
    )

    with AlphaVantageClient(
        settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    ) as client:
        summary = fill_cache(tickers, cache, client, settings)

    if summary.entries:
        merged = store.merge_caches(cache, summary.entries)
        store.save_cache(config.CACHE_FILE, merged)
        log.success(
            f"Fetched {summary.fetched}, refreshed {summary.refreshed}, "
            f"no-data {summary.not_found}, errors {summary.errors} "
            f"in {summary.attempted} call(s); cache holds {len(merged)} symbols"
        )
    else:
        log.warn("No records written this run")

    if summary.stopped_early:
        log.warn(f"Run stopped early: {summary.stop_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
