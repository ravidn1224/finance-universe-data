"""Environment-driven configuration for the pipeline.

Every path and tunable lives here so the scripts, the tests and the GitHub
Actions workflow all agree on where files are and how the API is called.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CACHE_FILE = Path(os.environ.get("CACHE_FILE") or ROOT / "cache_av.json")
TICKERS_FILE = Path(os.environ.get("TICKERS_FILE") or ROOT / "clean_tickers.txt")
MASTER_FILE = Path(os.environ.get("MASTER_FILE") or ROOT / "master_stocks.csv")

#: The browsable directory the Google Sheet downloads and pastes verbatim.
UNIVERSE_FILE = Path(os.environ.get("UNIVERSE_FILE") or ROOT / "universe.csv")

#: Yahoo prices, market caps, P/Es and 150-day averages for the whole universe.
#: Kept between runs purely as a safety net: Yahoo is unofficial, and merging
#: onto the previous file means an outage shows stale numbers rather than
#: blanking two columns of the sheet.
MARKET_FILE = Path(os.environ.get("MARKET_FILE") or ROOT / "market_data.json")

OVERVIEW_URL = "https://www.alphavantage.co/query"

#: GICS sectors for index members, which are stricter than any vendor's.
SP500_CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

#: Columns of ``master_stocks.csv``. Consumers depend on this exact order, so
#: new fields are appended before ``last_updated`` rather than inserted.
MASTER_COLUMNS = (
    "symbol",
    "name",
    "sector",
    "industry",
    "marketCap",
    "price",
    "peRatio",
    "ma150",
    "last_updated",
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def api_key() -> str:
    """Return the Alpha Vantage key from the environment."""
    key = (
        os.environ.get("ALPHAVANTAGE_API_KEY")
        or os.environ.get("API_KEY")
        or ""
    ).strip()
    if not key:
        raise ConfigError(
            "No Alpha Vantage API key found. Set ALPHAVANTAGE_API_KEY. In GitHub "
            "Actions add it as a repository secret and expose it via `env:` -- "
            "never commit it, since a key in git history is a leaked key."
        )
    return key


@dataclass(frozen=True)
class FetchSettings:
    """Tunables for a cache-filling run."""

    #: Hard cap on API calls attempted in one run; the free tier allows 25/day.
    max_calls: int = 25
    #: Seconds to wait between calls; the free tier also throttles per minute.
    sleep_seconds: float = 12.0
    #: Seconds to wait for a single HTTP response.
    timeout_seconds: float = 30.0
    #: Retries for transport-level failures (connection resets, 5xx).
    max_retries: int = 3
    #: How old a fundamentals record may get before it is re-fetched. Prices
    #: and market caps refresh daily via Yahoo, so Alpha Vantage only has to
    #: keep up with names, sectors and industries, which rarely change.
    refresh_after_days: int = 180
    #: Spend the budget only on missing symbols, never on refreshing.
    fill_only: bool = False
    #: Symbols recorded as non-existent are skipped unless this is set.
    retry_missing: bool = False
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "FetchSettings":
        return cls(
            max_calls=_env_int("MAX_CALLS", 25, minimum=1),
            sleep_seconds=_env_float("SLEEP_SECONDS", 12.0),
            timeout_seconds=_env_float("HTTP_TIMEOUT", 30.0, minimum=1.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            refresh_after_days=_env_int("REFRESH_AFTER_DAYS", 180, minimum=1),
            fill_only=_env_flag("FILL_ONLY"),
            retry_missing=_env_flag("RETRY_MISSING"),
            api_key=api_key(),
        )
