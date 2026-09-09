"""Ticker symbol normalisation, listing downloads and chunking."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import requests

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

#: Up to five letters, optionally with a single-letter share class (BRK-B).
#: Longer suffixes encode warrants (``-WS``) and preferred series (``-PA``).
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")

#: Fifth-letter codes for warrants, rights and units on NASDAQ symbols.
_NON_COMMON_SUFFIXES = frozenset("WRU")

#: Security-name designations that are not common stock. Matched on word
#: boundaries so real companies such as Wright, Brightcove or Unit Corporation
#: are not mistaken for rights, warrants or units.
_NON_COMMON_NAME_RE = re.compile(
    r"\b(warrants?|rights?|units?|preferred|debentures?|notes?|subordinated)\b",
    re.IGNORECASE,
)

#: Parenthesised prose ("each representing the right to receive ...") describes
#: an ADR's terms rather than the issue type, so it is stripped before matching.
_PARENTHETICAL_RE = re.compile(r"\([^()]*\)")


def normalize_symbol(symbol: str) -> str:
    """Canonical form of a ticker, used as the cache key everywhere.

    Class separators are folded to a dash so ``BRK.B`` and ``BRK-B`` cannot end
    up as two different cache entries.
    """
    return symbol.strip().upper().replace(".", "-")


def is_common_stock(symbol: str, security_name: str = "") -> bool:
    """Whether a listing looks like ordinary common stock (ADRs included).

    Warrants, rights, units and preferred issues carry no useful company
    overview, so they are dropped before they burn API quota.
    """
    symbol = normalize_symbol(symbol)
    if not _SYMBOL_RE.match(symbol):
        return False

    # On NASDAQ only a fifth letter encodes the issue type; applying this to
    # shorter tickers would wrongly drop names such as FOUR or TOUR.
    if "-" not in symbol and len(symbol) == 5 and symbol[-1] in _NON_COMMON_SUFFIXES:
        return False

    # NASDAQ names read "<company> - <issue type>"; only the trailing
    # designation decides the issue type, so "Unit Corporation - Common Stock"
    # stays while "Foo Corp - Units" goes.
    name = _PARENTHETICAL_RE.sub(" ", security_name)
    designation = name.rsplit(" - ", 1)[-1] if " - " in name else name
    return not _NON_COMMON_NAME_RE.search(designation)


def parse_listing(text: str) -> pd.DataFrame:
    """Parse a pipe-delimited NASDAQ Trader listing file.

    The files end with a ``File Creation Time`` trailer that must not be read
    as a ticker row.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and lines[-1].lower().startswith("file creation time"):
        lines.pop()
    if not lines:
        raise ValueError("Listing file was empty")

    frame = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str)
    frame.columns = [str(col).strip() for col in frame.columns]
    return frame.fillna("")


def extract_symbols(frame: pd.DataFrame) -> list[tuple[str, str]]:
    """Return ``(symbol, security_name)`` pairs from a parsed listing."""
    symbol_col = next(
        (col for col in ("Symbol", "ACT Symbol", "NASDAQ Symbol") if col in frame),
        None,
    )
    if symbol_col is None:
        raise ValueError(f"No symbol column in listing; got {list(frame.columns)}")

    if "Test Issue" in frame:
        frame = frame[frame["Test Issue"].str.strip().str.upper() != "Y"]

    name_col = "Security Name" if "Security Name" in frame else None
    names = frame[name_col] if name_col else [""] * len(frame)
    return [
        (str(sym).strip(), str(name).strip())
        for sym, name in zip(frame[symbol_col], names)
        if str(sym).strip()
    ]


def download_listing(url: str, *, timeout: float = 30.0) -> pd.DataFrame:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return parse_listing(response.text)


def build_universe(listings: Iterable[pd.DataFrame]) -> list[str]:
    """Merge listing files into a sorted, de-duplicated common-stock universe."""
    keep: set[str] = set()
    for frame in listings:
        for symbol, name in extract_symbols(frame):
            if is_common_stock(symbol, name):
                keep.add(normalize_symbol(symbol))
    return sorted(keep)


def read_symbols(path: Path) -> list[str]:
    """Read a newline-delimited symbol file, ignoring blanks and comments."""
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as handle:
        return [
            normalize_symbol(line)
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]


def write_symbols(path: Path, symbols: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(f"{symbol}\n" for symbol in symbols)
