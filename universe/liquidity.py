"""Rank the listed universe by how heavily each symbol trades.

The exchange listings carry ~11,500 common stocks, but Alpha Vantage's 25
calls per day can only keep a fraction of them supplied with fundamentals.
Ranking by average daily dollar volume keeps the list matched to that budget
and lets it maintain itself: newly active companies rise into it, and dormant
or delisted ones fall out.

Ranking every listed symbol through Yahoo means one chart request each, which
takes minutes. So this runs in two stages: NASDAQ's screener returns the latest
session's price and volume for every listed stock in a single request, which is
enough to shortlist the plausible contenders, and only that shortlist is then
measured properly against Yahoo's multi-week average. A single session is too
noisy to rank on directly -- it agrees with the monthly average on ~91% of a
top-1000 -- but it is more than accurate enough to decide who is in contention.
"""

from __future__ import annotations

import time
import warnings
from functools import partial
from typing import Callable, Iterable, Mapping, Optional, Sequence

import requests

#: Tickers per batch.
DEFAULT_BATCH_SIZE = 200

#: Yahoo's chart endpoint is per symbol, so a batch is really one request each
#: and running them sequentially is what makes a full sweep slow. Four workers
#: is ~6x faster; more brings no further gain, so this stays at the lowest
#: setting that saturates the throughput.
DEFAULT_THREADS = 4

#: History used to average dollar volume. Long enough to smooth a quiet week,
#: short enough to keep the weekly job quick.
HISTORY_PERIOD = "1mo"

#: Pause between requests. The listing sweep makes far more calls than the
#: daily price refresh, and Yahoo starts returning 429s when pushed hard.
DEFAULT_PAUSE_SECONDS = 1.0

#: A symbol already in the list survives until it falls this far past the
#: cut-off, so names hovering near the boundary do not flip in and out weekly.
RETENTION_FACTOR = 1.2

#: Every listed US stock with its latest price, volume and market cap, in one
#: response. Undocumented, so treat a failure here as routine and fall back.
SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SCREENER_PARAMS = {"tableonly": "true", "limit": "25000", "download": "true"}

#: api.nasdaq.com rejects requests without a browser-like agent.
SCREENER_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

#: The screener normally lists ~7,000 stocks. A far smaller response means it
#: is degraded, and shortlisting from it would silently drop real contenders.
MIN_SCREENER_ROWS = 3000

#: How many contenders to measure per slot kept. One quiet session must not
#: push a genuinely liquid name out of contention, so this leaves 2x headroom.
SHORTLIST_FACTOR = 2.0

#: Returns ``{symbol: average daily dollar volume}`` for a batch.
Downloader = Callable[[Sequence[str]], dict[str, float]]


def _parse_number(value: object) -> float:
    """Read the screener's ``"$146.85"`` / ``"1,603,231"`` formatting."""
    text = str(value or "").replace("$", "").replace(",", "").strip()
    if not text or text in {"--", "N/A"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_screener_rows(*, timeout: int = 30) -> list[dict]:
    """Every listed US stock in one request, as the screener returns it.

    Each row carries the latest price and volume plus sector, industry and
    market cap, so this single response serves both the liquidity ranking and
    the published universe. Returns an empty list when the response looks
    truncated, which callers must treat as "the screener is unavailable"
    rather than "these symbols do not exist".
    """
    response = requests.get(
        SCREENER_URL,
        params=SCREENER_PARAMS,
        headers=SCREENER_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    rows = (response.json().get("data") or {}).get("rows") or []
    return list(rows) if len(rows) >= MIN_SCREENER_ROWS else []


def screener_dollar_volumes(*, timeout: int = 30) -> dict[str, float]:
    """Latest session's dollar volume for every listed US stock, in one request.

    Returns an empty mapping when the screener is unavailable or looks
    truncated, which callers must treat as "shortlisting unavailable" rather
    than "these symbols do not trade".
    """
    from . import symbols as symbols_module

    volumes: dict[str, float] = {}
    for row in fetch_screener_rows(timeout=timeout):
        traded = _parse_number(row.get("lastsale")) * _parse_number(row.get("volume"))
        if traded > 0:
            volumes[symbols_module.normalize_symbol(str(row.get("symbol", "")))] = traded
    return volumes


def shortlist(
    candidates: Sequence[str],
    screened: Mapping[str, float],
    *,
    limit: int,
    keep: Iterable[str] = (),
    factor: float = SHORTLIST_FACTOR,
) -> list[str]:
    """The most active ``limit * factor`` candidates by the screener's reading.

    Candidates the screener does not price are dropped: it covers listed common
    stock, so absence means the symbol is delisted or is not ordinary equity.

    Symbols in ``keep`` are measured even when they rank below the cut, since
    :func:`select_universe` can only apply its retention rule to symbols that
    were actually measured.
    """
    ranked = sorted(
        (symbol for symbol in candidates if screened.get(symbol, 0) > 0),
        key=lambda symbol: (-screened[symbol], symbol),
    )
    chosen = ranked[: max(limit, int(limit * factor))]

    taken = set(chosen)
    priced = set(ranked)
    chosen.extend(sorted(s for s in set(keep) & priced if s not in taken))
    return chosen


def yahoo_dollar_volumes(
    batch: Sequence[str], *, threads: int = DEFAULT_THREADS
) -> dict[str, float]:
    """Average daily dollar volume for a batch of symbols."""
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

    closes, volumes = frame["Close"], frame["Volume"]
    result: dict[str, float] = {}

    # A single-ticker request comes back as a Series rather than a frame.
    if len(batch) == 1 and closes.ndim == 1:
        traded = (closes * volumes).dropna()
        if not traded.empty:
            result[batch[0]] = float(traded.mean())
        return result

    for symbol in closes.columns:
        traded = (closes[symbol] * volumes[symbol]).dropna()
        if not traded.empty:
            value = float(traded.mean())
            if value > 0:
                result[str(symbol)] = value
    return result


def fetch_dollar_volumes(
    symbols: Sequence[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    threads: int = DEFAULT_THREADS,
    downloader: Optional[Downloader] = None,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, float]:
    """Average daily dollar volume for every symbol Yahoo can price.

    Symbols that fail are simply absent from the result; a batch that errors
    does not abort the sweep, since one bad request should not discard the
    rankings for everything else.
    """
    download = downloader or partial(yahoo_dollar_volumes, threads=threads)
    volumes: dict[str, float] = {}
    batches = [
        list(symbols[start : start + batch_size])
        for start in range(0, len(symbols), max(1, batch_size))
    ]

    for index, batch in enumerate(batches):
        try:
            volumes.update(download(batch))
        except Exception:
            pass
        if on_progress:
            on_progress(index + 1, len(batches))
        if pause_seconds and index < len(batches) - 1:
            sleep(pause_seconds)

    return volumes


def select_universe(
    candidates: Sequence[str],
    volumes: Mapping[str, float],
    *,
    limit: int,
    existing: Iterable[str] = (),
) -> list[str]:
    """Choose the most heavily traded symbols, damping week-to-week churn.

    A symbol enters the list on reaching the top ``limit`` and is kept while it
    stays within ``RETENTION_FACTOR`` of that cut-off, so borderline names do
    not oscillate. Symbols Yahoo could not price are excluded: without a price
    they would only ever produce an empty row.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    ranked = sorted(
        (sym for sym in candidates if volumes.get(sym, 0) > 0),
        key=lambda sym: (-volumes[sym], sym),
    )
    retained = set(existing)
    keep_until = int(limit * RETENTION_FACTOR)

    chosen = [
        symbol
        for position, symbol in enumerate(ranked)
        if position < limit or (symbol in retained and position < keep_until)
    ]
    return sorted(chosen)
