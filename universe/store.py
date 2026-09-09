"""Persistence for the symbol overview cache.

The cache is the expensive asset in this project: the Alpha Vantage free tier
allows 25 calls per day, so a full universe takes weeks to assemble. Every
write is therefore atomic and additive -- nothing here ever drops a previously
fetched record.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

Entry = dict[str, Any]
Cache = dict[str, Entry]

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"

#: Fields copied from an API record into the cache and published to the CSV.
#: ``peRatio`` comes from Alpha Vantage; ``ma150`` is filled by the daily Yahoo
#: quote refresh. Both are blank until their source has run for a symbol.
DATA_FIELDS = ("symbol", "name", "sector", "industry", "marketCap", "price", "peRatio", "ma150")

#: The subset of :data:`DATA_FIELDS` that Alpha Vantage supplies, so refetching
#: an overview can actually fill them. ``marketCap``, ``price`` and ``ma150``
#: are excluded: those come from the Yahoo refresh.
OVERVIEW_FIELDS = ("name", "sector", "industry", "peRatio")

#: Stand-in fetch time for records written before timestamps were tracked, so
#: they sort as the stalest and get refreshed first.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def make_entry(
    symbol: str,
    *,
    name: str = "",
    sector: str = "",
    industry: str = "",
    market_cap: str = "",
    price: str = "",
    pe_ratio: str = "",
    status: str = STATUS_OK,
    fetched_at: Optional[str] = None,
    shares_outstanding: str = "",
) -> Entry:
    entry = {
        "symbol": symbol,
        "name": name,
        "sector": sector,
        "industry": industry,
        "marketCap": market_cap,
        "price": price,
        "peRatio": pe_ratio,
        "status": status,
        "fetched_at": fetched_at or utc_now_iso(),
    }
    if shares_outstanding:
        entry["sharesOutstanding"] = shares_outstanding
    return entry


def entry_status(entry: Mapping[str, Any]) -> str:
    """Status of a cache entry, defaulting to ``ok`` for legacy records."""
    return str(entry.get("status") or STATUS_OK)


def is_usable(entry: Mapping[str, Any]) -> bool:
    return entry_status(entry) == STATUS_OK


def needs_backfill(entry: Mapping[str, Any]) -> bool:
    """Whether an entry predates a field this version stores.

    Tests for the *key*, not a value. Every entry written by
    :func:`make_entry` carries all of :data:`OVERVIEW_FIELDS`, so an absent key
    means the record was cached before the field existed, while a present but
    empty one means Alpha Vantage was asked and had nothing -- a company with
    no earnings has no P/E, and re-asking every run would burn the whole daily
    budget on it forever.

    Without this a newly added column stays blank until each record happens to
    age past the refresh threshold, which for a 180-day threshold means months.
    """
    return is_usable(entry) and any(field not in entry for field in OVERVIEW_FIELDS)


def fetched_at(entry: Mapping[str, Any]) -> datetime:
    """When an entry was fetched; legacy records without a stamp read as epoch."""
    raw = entry.get("fetched_at")
    if not raw:
        return _EPOCH
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_days(entry: Mapping[str, Any], *, now: Optional[datetime] = None) -> float:
    return ((now or utc_now()) - fetched_at(entry)).total_seconds() / 86400.0


def last_updated(entry: Mapping[str, Any]) -> str:
    """Most recent change to a row, from either data source.

    Fundamentals come from Alpha Vantage (``fetched_at``) and prices from
    Yahoo (``quoted_at``); the published timestamp reflects whichever moved
    last.
    """
    stamps = [str(entry.get(key) or "") for key in ("fetched_at", "quoted_at")]
    return max(stamps)


def load_cache(path: Path) -> Cache:
    """Load a cache file, returning an empty cache when it is absent or empty."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return {str(key): dict(value) for key, value in data.items()}


def save_cache(path: Path, cache: Cache) -> None:
    """Write the cache atomically with sorted keys for reviewable diffs."""
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
            json.dump(cache, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _freshness(entry: Mapping[str, Any]) -> tuple[int, datetime]:
    """Sort key deciding which of two records for a symbol to keep.

    A successful record always beats a ``not_found`` one; ties are broken by
    fetch time.
    """
    return (1 if is_usable(entry) else 0, fetched_at(entry))


def merge_caches(base: Cache, *updates: Cache) -> Cache:
    """Combine caches without losing data, newest usable record winning."""
    merged: Cache = dict(base)
    for update in updates:
        for symbol, entry in update.items():
            existing = merged.get(symbol)
            if existing is None or _freshness(entry) >= _freshness(existing):
                merged[symbol] = entry
    return merged


def to_master_row(entry: Mapping[str, Any]) -> dict[str, str]:
    """Project a cache entry onto the published CSV schema.

    Bookkeeping fields such as ``status`` stay out of the CSV so the published
    schema never shifts under consumers.
    """
    usable = is_usable(entry)
    return {
        field: str(entry.get(field, "") or "") if usable or field == "symbol" else ""
        for field in DATA_FIELDS
    }
