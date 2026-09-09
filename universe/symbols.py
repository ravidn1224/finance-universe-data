"""Ticker symbol normalisation, listing downloads and chunking."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

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

#: The designation word each match maps to, for callers that want the issue
#: type rather than a yes/no. Debentures and subordinated issues are debt and
#: are published under one heading, since the distinction does not change what
#: anyone screening equities would do with the row.
_DESIGNATION_TYPES = {
    "warrant": "Warrant",
    "right": "Right",
    "unit": "Unit",
    "preferred": "Preferred",
    "note": "Note",
    "debenture": "Note",
    "subordinated": "Note",
}

#: Parenthesised prose ("each representing the right to receive ...") describes
#: an ADR's terms rather than the issue type, so it is stripped before matching.
_PARENTHETICAL_RE = re.compile(r"\([^()]*\)")

#: Some listings leave the parenthetical unterminated, as America Movil's does
#: with "(each representing the right to receive twenty (20) Series B Shares".
#: Stripping only balanced pairs would leave "right" behind and drop a liquid
#: ADR as a rights issue, so an unclosed group runs to the end of the name.
_UNCLOSED_PARENTHETICAL_RE = re.compile(r"\([^()]*$")

#: The same prose without brackets: "American Depositary Shares, each
#: representing one unit". What follows describes the terms, not the issue, and
#: Banco Santander Brasil is an ordinary ADR rather than a SPAC unit. A real
#: unit names itself before this clause ("Units, each consisting of one share
#: and one warrant"), so truncating here keeps those.
_TERMS_CLAUSE_RE = re.compile(r",\s*each\b.*$", re.IGNORECASE | re.DOTALL)

#: Master limited partnerships trade as "Common Units Representing Limited
#: Partner Interests" -- Energy Transfer and MPLX among them, both well over
#: $50B. They are ordinary listed equity and belong with the stocks, not with
#: the SPAC units they share a word with.
_PARTNERSHIP_RE = re.compile(r"\b(common units?|limited partner)\b", re.IGNORECASE)


def normalize_symbol(symbol: str) -> str:
    """Canonical form of a ticker, used as the cache key everywhere.

    Class separators are folded to a dash so ``BRK.B``, ``BRK/B`` (the form the
    NASDAQ screener uses) and ``BRK-B`` cannot end up as separate cache entries.
    """
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def security_designation(security_name: str) -> Optional[str]:
    """The issue type a listing's name declares, or ``None`` for common stock.

    Returns one of ``Warrant``, ``Right``, ``Unit``, ``Preferred`` or ``Note``.
    Exchange listings carry these as ordinary five-letter tickers with no other
    marking, so the security name is the only reliable way to tell a SPAC's
    warrant from the SPAC itself.
    """
    # NASDAQ names read "<company> - <issue type>"; only the trailing
    # designation decides the issue type, so "Unit Corporation - Common Stock"
    # stays while "Foo Corp - Units" goes.
    name = _PARENTHETICAL_RE.sub(" ", security_name)
    name = _UNCLOSED_PARENTHETICAL_RE.sub(" ", name)
    name = _TERMS_CLAUSE_RE.sub(" ", name)
    designation = name.rsplit(" - ", 1)[-1] if " - " in name else name

    if _PARTNERSHIP_RE.search(designation):
        return None

    match = _NON_COMMON_NAME_RE.search(designation)
    if not match:
        return None
    return _DESIGNATION_TYPES[match.group(1).lower().rstrip("s")]


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

    return security_designation(security_name) is None


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

    # Both listing files flag funds explicitly, which is the only reliable way
    # to spot them: an ETF carries an ordinary ticker and a name with none of
    # the warrant or unit wording, so SPY and QQQ would otherwise pass as
    # common stock -- and rank near the top of any liquidity ranking.
    if "ETF" in frame:
        frame = frame[frame["ETF"].str.strip().str.upper() != "Y"]

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
