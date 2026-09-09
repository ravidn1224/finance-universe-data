"""Bulk market data from Yahoo Finance.

Alpha Vantage's free tier costs one call per symbol against a 25/day budget,
which suits fundamentals that rarely change but is far too slow for prices.

Yahoo is used through its *chart* endpoint via ``yfinance.download``. The chart
endpoint is per symbol, so a batch is really one request each; what matters is
that it tolerates modest concurrency, unlike the per-symbol *quote* endpoint,
which trips Yahoo's rate limiter (HTTP 429) after a few hundred calls and then
fails everything, bulk endpoint included, for a considerable while.

Yahoo is an unofficial, undocumented source that can change or fail without
notice. Failures are therefore reported rather than raised, and callers keep
the previously cached value instead of overwriting it with a blank.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from functools import partial
from typing import Callable, Iterable, Mapping, Optional, Sequence

#: Trading days used for the moving average, matching Alpha Vantage's
#: ``50DayMovingAverage`` that the ``price`` column has always held.
MA_WINDOW = 50

#: Tickers per batch, keeping memory and any single failure bounded.
DEFAULT_BATCH_SIZE = 200

#: Concurrent downloads within a batch. Four is ~6x faster than sequential;
#: higher settings measured no faster, so this is the gentlest setting that
#: still saturates throughput.
DEFAULT_THREADS = 4

#: History fetched per batch, comfortably more than MA_WINDOW trading days.
HISTORY_PERIOD = "6mo"

#: Returns a ``{symbol: [closing prices, oldest first]}`` mapping for a batch.
Downloader = Callable[[Sequence[str]], dict[str, list[float]]]


@dataclass(frozen=True)
class Quote:
    """Market data for one symbol, formatted as the cache stores it."""

    symbol: str
    #: The 50-day moving average, matching the existing ``price`` column.
    price: str
    #: Empty unless shares outstanding is known, so a cached value survives.
    market_cap: str = ""


def _format_price(value: float) -> str:
    return f"{value:.2f}"


def yahoo_downloader(
    batch: Sequence[str], *, threads: int = DEFAULT_THREADS
) -> dict[str, list[float]]:
    """Download closing prices for a batch of symbols."""
    import yfinance

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        frame = yfinance.download(
            list(batch),
            period=HISTORY_PERIOD,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=threads,
            group_by="column",
        )

    if frame is None or frame.empty:
        return {}

    closes = frame["Close"]
    # A single-ticker request returns a plain Series rather than a frame.
    if len(batch) == 1 and closes.ndim == 1:
        series = closes.dropna()
        return {batch[0]: [float(v) for v in series]} if not series.empty else {}

    result: dict[str, list[float]] = {}
    for symbol in closes.columns:
        series = closes[symbol].dropna()
        if not series.empty:
            result[str(symbol)] = [float(v) for v in series]
    return result


def moving_average(closes: Sequence[float], window: int = MA_WINDOW) -> Optional[float]:
    """Mean of the last ``window`` closes, or ``None`` without enough history."""
    if len(closes) < window:
        return None
    tail = closes[-window:]
    return sum(tail) / len(tail)


def _market_cap(entry: Mapping[str, object], last_close: float) -> str:
    """Market cap as shares outstanding times the latest close.

    Share counts move only on buybacks and issuance, so the figure Alpha
    Vantage supplies stays accurate between refreshes and gives an exact
    result. Rescaling the previous market cap by the price ratio was tried
    instead and drifted badly -- the stored cap is spot-derived while the
    stored price is a 50-day average, so the two do not divide cleanly.

    Returns an empty string when shares outstanding is unknown, which leaves
    the cached market cap untouched rather than publishing a guess.
    """
    try:
        shares = float(str(entry.get("sharesOutstanding") or ""))
    except ValueError:
        return ""
    if shares <= 0 or last_close <= 0:
        return ""
    return str(int(round(shares * last_close)))


def fetch_quotes(
    symbols: Sequence[str],
    cache: Mapping[str, Mapping[str, object]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    threads: int = DEFAULT_THREADS,
    downloader: Optional[Downloader] = None,
) -> tuple[dict[str, Quote], list[str]]:
    """Fetch quotes for ``symbols`` in bulk.

    Returns ``(quotes, failed)``. A symbol fails when Yahoo returns no usable
    history, so the caller can leave its cached values untouched.
    """
    if not symbols:
        return {}, []

    download = downloader or partial(yahoo_downloader, threads=threads)
    quotes: dict[str, Quote] = {}
    failed: list[str] = []

    for start in range(0, len(symbols), max(1, batch_size)):
        batch = list(symbols[start : start + batch_size])
        try:
            closes = download(batch)
        except Exception:
            # One bad batch must not lose the symbols in every other batch.
            failed.extend(batch)
            continue

        for symbol in batch:
            series = closes.get(symbol) or []
            average = moving_average(series)
            if average is None:
                failed.append(symbol)
                continue
            quotes[symbol] = Quote(
                symbol=symbol,
                price=_format_price(average),
                # Market cap follows the latest close, not the average, so it
                # keeps its conventional meaning.
                market_cap=_market_cap(cache.get(symbol) or {}, series[-1]),
            )

    return quotes, failed


def apply_quotes(cache: dict, quotes: Iterable[Quote], *, stamp: str) -> int:
    """Write quotes onto existing cache entries, returning how many changed.

    Only the volatile fields are touched; a blank value never overwrites a
    populated one, so a partial Yahoo response cannot erase good data.
    """
    changed = 0
    for quote in quotes:
        entry = cache.get(quote.symbol)
        if entry is None:
            continue
        updated = False
        for field, value in (("price", quote.price), ("marketCap", quote.market_cap)):
            if value and str(entry.get(field, "")) != value:
                entry[field] = value
                updated = True
        if updated:
            entry["quoted_at"] = stamp
            changed += 1
    return changed
