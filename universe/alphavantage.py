"""Alpha Vantage client with explicit rate-limit semantics.

Alpha Vantage answers with HTTP 200 for every outcome, encoding failures in the
JSON body instead. Treating all of those as "no data" makes a run grind through
the whole ticker list against an exhausted quota, so responses are classified
into distinct outcomes and the caller decides whether to skip, back off or stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config, store


class Outcome(str, Enum):
    """Result of a single overview request."""

    OK = "ok"
    #: The symbol is unknown to Alpha Vantage; never worth retrying.
    NOT_FOUND = "not_found"
    #: Per-minute call frequency exceeded; retry after a pause.
    THROTTLED = "throttled"
    #: Daily quota for this key is gone; the run must stop.
    QUOTA_EXHAUSTED = "quota_exhausted"
    #: Transport or protocol failure.
    ERROR = "error"


@dataclass(frozen=True)
class FetchResult:
    outcome: Outcome
    symbol: str
    entry: Optional[store.Entry] = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


def mask_key(key: str) -> str:
    """Redact an API key so it can be safely logged."""
    if len(key) <= 4:
        return "*" * len(key)
    return f"{key[:2]}{'*' * (len(key) - 4)}{key[-2:]}"


def classify(payload: Mapping[str, Any], symbol: str) -> FetchResult:
    """Map a decoded Alpha Vantage body onto an :class:`Outcome`."""
    if payload.get("Symbol"):
        return FetchResult(
            outcome=Outcome.OK,
            symbol=symbol,
            entry=store.make_entry(
                symbol,
                name=str(payload.get("Name", "") or ""),
                sector=str(payload.get("Sector", "") or ""),
                industry=str(payload.get("Industry", "") or ""),
                market_cap=str(payload.get("MarketCapitalization", "") or ""),
                price=str(payload.get("50DayMovingAverage", "") or ""),
            ),
        )

    note = str(payload.get("Note", "") or "")
    if note:
        return FetchResult(Outcome.THROTTLED, symbol, message=note)

    information = str(payload.get("Information", "") or "")
    if information:
        lowered = information.lower()
        if "per day" in lowered or "rate limit" in lowered:
            return FetchResult(Outcome.QUOTA_EXHAUSTED, symbol, message=information)
        if "premium" in lowered:
            return FetchResult(Outcome.ERROR, symbol, message=information)
        return FetchResult(Outcome.THROTTLED, symbol, message=information)

    error_message = str(payload.get("Error Message", "") or "")
    if error_message:
        return FetchResult(Outcome.NOT_FOUND, symbol, message=error_message)

    if not payload:
        return FetchResult(Outcome.NOT_FOUND, symbol, message="Empty response")

    return FetchResult(
        Outcome.ERROR, symbol, message=f"Unrecognised response: {sorted(payload)[:5]}"
    )


class AlphaVantageClient:
    """Thin, retrying wrapper around the OVERVIEW endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_key:
            raise config.ConfigError("An Alpha Vantage API key is required")
        self._api_key = api_key
        self._timeout = timeout
        self._session = session or self._build_session(max_retries)

    @staticmethod
    def _build_session(max_retries: int) -> requests.Session:
        session = requests.Session()
        # Only transport-level failures are retried here; quota responses come
        # back as HTTP 200 and are handled by `classify`.
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "finance-universe-data/1.0"})
        return session

    def fetch_overview(self, symbol: str) -> FetchResult:
        params = {
            "function": "OVERVIEW",
            "symbol": symbol,
            "apikey": self._api_key,
        }
        try:
            response = self._session.get(
                config.OVERVIEW_URL, params=params, timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            return FetchResult(Outcome.ERROR, symbol, message=f"Malformed JSON: {exc}")
        except requests.RequestException as exc:
            return FetchResult(Outcome.ERROR, symbol, message=str(exc))

        if not isinstance(payload, Mapping):
            return FetchResult(
                Outcome.ERROR, symbol, message=f"Unexpected payload type {type(payload)}"
            )
        return classify(payload, symbol)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "AlphaVantageClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
