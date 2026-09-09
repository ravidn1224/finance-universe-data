"""Market data for every symbol in the published universe.

The universe is ~11,000 listings, while the Alpha Vantage cache that used to
supply P/E and the 150-day average covers ~900 of them: the free tier allows 25
calls a day, so filling the rest that way would take over a year. Those two
columns therefore arrived in the sheet at 0% and 7% coverage respectively, and
no amount of waiting was going to change that.

Yahoo answers both questions in bulk instead, through two endpoints with very
different shapes:

* the *quote* endpoint takes a thousand symbols per request, so the whole
  universe is a dozen requests and about fifteen seconds. It carries the
  trailing P/E, the last price and the market cap.
* the *chart* endpoint is one request per symbol, so a year of history for the
  universe takes a few minutes at modest concurrency. Only it can give a true
  150-day mean -- the quote endpoint publishes 50- and 200-day averages, and
  neither is the window the sheet asks for.

Yahoo is unofficial and undocumented. Every failure here is reported rather
than raised, and callers merge onto the previous file, so a bad day leaves the
sheet showing yesterday's numbers instead of blanking a column.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import quotes

#: The bulk quote endpoint. Measured at a thousand symbols per request with no
#: throttling; the per-symbol quote endpoint rate-limits aggressively, but this
#: one is a single call for the whole batch.
QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"

#: Symbols per quote request.
QUOTE_BATCH = 1000

#: Courtesy pause between quote requests.
QUOTE_PAUSE_SECONDS = 0.3

#: Trading days in the long average the sheet publishes.
SMA_WINDOW = quotes.MA_150_WINDOW

#: Fields read from a quote record, mapped to the names used here.
QUOTE_FIELDS = {
    "price": "regularMarketPrice",
    "market_cap": "marketCap",
    "pe": "trailingPE",
}

#: Below this, a trailing P/E is an artefact rather than a cheap stock. A ratio
#: of 0.004 says the company earns 250 times its share price, which happens
#: when a reverse split restates the price but not the earnings per share --
#: several serial reverse-splitters produce one. They also display as "0.00",
#: which reads like a real zero and sorts to the top of a P/E filter.
MIN_PLAUSIBLE_PE = 0.1


@dataclass(frozen=True)
class MarketRow:
    """Everything Yahoo knows about one symbol, all fields optional."""

    price: Optional[float] = None
    market_cap: Optional[float] = None
    pe: Optional[float] = None
    sma150: Optional[float] = None

    def merged_over(self, previous: "MarketRow") -> "MarketRow":
        """This row, falling back to ``previous`` wherever Yahoo said nothing.

        A symbol Yahoo skipped today keeps yesterday's figure rather than going
        blank, which matters because a single failed batch would otherwise wipe
        a thousand rows of the sheet.
        """
        return MarketRow(
            price=self.price if self.price is not None else previous.price,
            market_cap=(
                self.market_cap if self.market_cap is not None else previous.market_cap
            ),
            pe=self.pe if self.pe is not None else previous.pe,
            sma150=self.sma150 if self.sma150 is not None else previous.sma150,
        )


#: Fetches one batch of quote records from Yahoo.
QuoteFetcher = Callable[[Sequence[str]], list[Mapping[str, Any]]]


def yahoo_symbol(display: str) -> str:
    """Yahoo's form of a display ticker: ``BRK.B`` becomes ``BRK-B``."""
    return str(display or "").strip().upper().replace(".", "-")


def _to_float(value: object) -> Optional[float]:
    """A finite, non-zero float, or ``None``.

    Yahoo reports an unknown market cap as ``0`` rather than omitting it, and a
    zero P/E means "no earnings to divide by" -- neither is a real measurement,
    and both would sort to the top of a numeric filter in the sheet.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return None if number == 0 else number


def _plausible_pe(value: object) -> Optional[float]:
    """A trailing P/E, or ``None`` if it is too small to be a real one.

    Applied on the way in *and* on the way back off disk. Filtering only at
    fetch time is not enough: a rejected value becomes ``None``, and ``None`` is
    exactly what :meth:`MarketRow.merged_over` replaces with yesterday's figure,
    so the artefact would be restored from the saved file every run.
    """
    number = _to_float(value)
    if number is None or number < MIN_PLAUSIBLE_PE:
        return None
    return number


def yahoo_quote_fetcher(batch: Sequence[str]) -> list[Mapping[str, Any]]:
    """Fetch one batch from the quote endpoint.

    Goes through yfinance's session because Yahoo requires a cookie and crumb
    on this endpoint, and that library already negotiates and refreshes them.
    """
    from yfinance.data import YfData

    payload = YfData().get_raw_json(QUOTE_URL, params={"symbols": ",".join(batch)})
    result = (payload or {}).get("quoteResponse", {}).get("result") or []
    return [record for record in result if isinstance(record, Mapping)]


def fetch_quote_fields(
    symbols: Sequence[str],
    *,
    batch_size: int = QUOTE_BATCH,
    fetcher: Optional[QuoteFetcher] = None,
    pause_seconds: float = QUOTE_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_batch: Optional[Callable[[int, int, int], None]] = None,
) -> dict[str, dict[str, Optional[float]]]:
    """Price, market cap and P/E for ``symbols``, keyed by display symbol.

    Symbols are sent in Yahoo's dashed form and mapped back on the way out, so
    callers only ever deal in the dotted form the sheet publishes.
    """
    fetch = fetcher or yahoo_quote_fetcher
    by_yahoo = {yahoo_symbol(symbol): symbol for symbol in symbols if symbol}
    ordered = list(by_yahoo)
    found: dict[str, dict[str, Optional[float]]] = {}

    for index, start in enumerate(range(0, len(ordered), max(1, batch_size))):
        batch = ordered[start : start + max(1, batch_size)]
        try:
            records = fetch(batch)
        except Exception:
            # One refused batch must not cost the other ten thousand symbols.
            records = []

        for record in records:
            display = by_yahoo.get(yahoo_symbol(record.get("symbol")))
            if not display:
                continue
            fields = {
                name: _to_float(record.get(key)) for name, key in QUOTE_FIELDS.items()
            }
            fields["pe"] = _plausible_pe(record.get(QUOTE_FIELDS["pe"]))
            found[display] = fields

        if on_batch:
            on_batch(index + 1, len(batch), len(found))
        if pause_seconds and start + batch_size < len(ordered):
            sleep(pause_seconds)

    return found


#: Passes over the symbol list. Yahoo throttles the chart endpoint partway
#: through a run of this size, and a throttled symbol is indistinguishable from
#: one with no history -- both come back empty. Each pass retries only what is
#: still missing, so a throttled symbol gets another chance while one with no
#: history fails again cheaply.
#:
#: Three is a compromise, not a cure. Coverage also compounds across days,
#: because every run merges onto the saved file rather than replacing it.
SMA_ATTEMPTS = 3

#: Pause before re-attempting the symbols a pass did not answer, to let
#: whatever rate limit was hit decay.
SMA_RETRY_PAUSE_SECONDS = 30.0

#: Pause between batches within a pass. Cheap insurance: the throttle costs a
#: whole batch of two hundred symbols, so a fraction of a second to avoid
#: tripping it pays for itself many times over.
SMA_BATCH_PAUSE_SECONDS = 0.5


def fetch_sma(
    symbols: Sequence[str],
    *,
    window: int = SMA_WINDOW,
    batch_size: int = quotes.DEFAULT_BATCH_SIZE,
    threads: int = quotes.DEFAULT_THREADS,
    downloader: Optional[quotes.Downloader] = None,
    attempts: int = SMA_ATTEMPTS,
    retry_pause_seconds: float = SMA_RETRY_PAUSE_SECONDS,
    batch_pause_seconds: float = SMA_BATCH_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_batch: Optional[Callable[[int, int, int], None]] = None,
    on_attempt: Optional[Callable[[int, int], None]] = None,
) -> dict[str, float]:
    """The ``window``-day mean close per symbol, keyed by display symbol.

    Symbols with less history than the window are absent rather than carrying a
    short-window guess, so a company that listed last month shows a blank
    instead of an average that means something different from every other row.
    """
    download = downloader or partial(quotes.yahoo_downloader, threads=threads)
    by_yahoo = {yahoo_symbol(symbol): symbol for symbol in symbols if symbol}
    found: dict[str, float] = {}
    batches_done = 0

    for attempt in range(max(1, attempts)):
        pending = [
            yahoo for yahoo, display in by_yahoo.items() if display not in found
        ]
        if not pending:
            break
        if attempt and retry_pause_seconds:
            sleep(retry_pause_seconds)
        if on_attempt:
            on_attempt(attempt + 1, len(pending))

        for start in range(0, len(pending), max(1, batch_size)):
            batch = pending[start : start + max(1, batch_size)]
            try:
                closes = download(batch)
            except Exception:
                closes = {}

            for yahoo in batch:
                average = quotes.moving_average(closes.get(yahoo) or [], window)
                if average is not None:
                    found[by_yahoo[yahoo]] = average

            batches_done += 1
            if on_batch:
                on_batch(batches_done, len(batch), len(found))
            if batch_pause_seconds and start + batch_size < len(pending):
                sleep(batch_pause_seconds)

    return found


def build_market_data(
    quote_fields: Mapping[str, Mapping[str, Optional[float]]],
    sma: Mapping[str, float],
) -> dict[str, MarketRow]:
    """Combine the two Yahoo passes into one row per symbol."""
    rows: dict[str, MarketRow] = {}
    for symbol in set(quote_fields) | set(sma):
        fields = quote_fields.get(symbol) or {}
        rows[symbol] = MarketRow(
            price=fields.get("price"),
            market_cap=fields.get("market_cap"),
            pe=fields.get("pe"),
            sma150=sma.get(symbol),
        )
    return rows


def merge_over_previous(
    fresh: Mapping[str, MarketRow], previous: Mapping[str, MarketRow]
) -> dict[str, MarketRow]:
    """Fresh data with yesterday's values filling any gap in it."""
    merged = {symbol: MarketRow() for symbol in set(fresh) | set(previous)}
    for symbol in merged:
        merged[symbol] = fresh.get(symbol, MarketRow()).merged_over(
            previous.get(symbol, MarketRow())
        )
    return merged


def load(path: Path) -> dict[str, MarketRow]:
    """Read a saved market-data file, treating absence as empty."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(symbol): MarketRow(
            price=_to_float(fields.get("price")),
            market_cap=_to_float(fields.get("market_cap")),
            pe=_plausible_pe(fields.get("pe")),
            sma150=_to_float(fields.get("sma150")),
        )
        for symbol, fields in raw.items()
        if isinstance(fields, Mapping)
    }


def save(path: Path, rows: Mapping[str, MarketRow]) -> None:
    """Write market data atomically, sorted so the daily diff is reviewable."""
    import os
    import tempfile

    payload = {
        symbol: {
            key: value
            for key, value in (
                ("price", row.price),
                ("market_cap", row.market_cap),
                ("pe", row.pe),
                ("sma150", row.sma150),
            )
            if value is not None
        }
        for symbol, row in sorted(rows.items())
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def as_enrichment(rows: Mapping[str, MarketRow]) -> dict[str, dict[str, Any]]:
    """Shape market data the way :func:`universe.sheet.merge_enrichment` reads it."""
    return {
        symbol: {
            "price": row.price,
            "market_cap": row.market_cap,
            "pe": row.pe,
            "sma150": row.sma150,
        }
        for symbol, row in rows.items()
    }


def coverage(rows: Mapping[str, MarketRow]) -> dict[str, int]:
    """Count how many symbols carry each field, for the run log."""
    return {
        field: sum(1 for row in rows.values() if getattr(row, field) is not None)
        for field in ("price", "market_cap", "pe", "sma150")
    }
