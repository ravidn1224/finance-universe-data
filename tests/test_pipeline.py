"""Tests for symbol handling, cache persistence and the fetch/refresh loop."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import build_universe
import cache as cache_cli
from universe import config, liquidity, market, quotes, sheet, store, symbols
from universe.alphavantage import (
    FetchResult,
    Outcome,
    classify,
    clean_number,
    mask_key,
)

# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  brk.b ", "BRK-B"), ("aapl", "AAPL"), ("MKC.V", "MKC-V")],
)
def test_normalize_symbol(raw: str, expected: str) -> None:
    assert symbols.normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        ("AAPL", "Apple Inc. - Common Stock"),
        # Four-letter tickers ending in U/W/R are ordinary shares, not warrants.
        ("FOUR", "Shift4 Payments Inc. - Class A Common Stock"),
        ("TOUR", "Tuniu Corporation - Common Stock"),
        ("F", "Ford Motor Company - Common Stock"),
        # ADRs are ordinary equity exposure and must survive the filter.
        ("BABA", "Alibaba Group Holding Limited - American Depositary Shares"),
        ("BTI", "British American Tobacco p.l.c. - American Depositary Shares"),
        # Share classes normalise to a dash and stay in the universe.
        ("BRK.B", "Berkshire Hathaway Inc. - Class B Common Stock"),
        # Company names that merely contain "unit" or "right" are not issues.
        ("UNTC", "Unit Corporation - Common Stock"),
        ("WRT", "Wright Investors Service - Common Stock"),
        # Parenthesised ADR terms describe the ratio, not the issue type.
        (
            "AMX",
            "America Movil, S.A.B. de C.V. American Depositary Shares "
            "(each representing the right to receive twenty Series L Shares)",
        ),
        # Verbatim from otherlisted.txt: the parenthetical is never closed, so
        # naive stripping leaves "right" behind and drops a liquid ADR.
        (
            "AMX",
            "America Movil, S.A.B. de C.V. American Depositary Shares (each "
            "representing the right to receive twenty (20) Series B Shares",
        ),
    ],
)
def test_common_stock_is_kept(symbol: str, name: str) -> None:
    assert symbols.is_common_stock(symbol, name)


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        ("ABCDW", "Some Corp - Warrant"),
        ("ABCDR", "Some Corp - Rights"),
        ("ABCDU", "Some Corp - Units"),
        ("ABC-PA", "Some Corp - 6.5% Series A Preferred"),
        ("ABCD-WS", "Some Corp - Warrants"),
        ("ABC$", "Weird"),
        ("", ""),
        ("AAPL", "Apple Inc - Warrants expiring 2030"),
        ("BAC-PB", "Bank of America - Depositary Shares representing Preferred"),
    ],
)
def test_non_common_stock_is_dropped(symbol: str, name: str) -> None:
    assert not symbols.is_common_stock(symbol, name)


def test_parse_listing_drops_trailer_row() -> None:
    text = (
        "Symbol|Security Name|Test Issue\n"
        "AAPL|Apple Inc. - Common Stock|N\n"
        "ZZZT|Nasdaq Test Security|Y\n"
        "File Creation Time: 0521202516:30\n"
    )
    frame = symbols.parse_listing(text)
    assert len(frame) == 2

    universe = symbols.build_universe([frame])
    assert universe == ["AAPL"]  # test issue and trailer both excluded


def test_build_universe_excludes_etfs() -> None:
    # An ETF has an ordinary ticker and a name with no warrant or unit wording,
    # so only the listing's own flag distinguishes it from common stock. Left
    # in, SPY and QQQ would outrank nearly every real company on volume.
    text = (
        "Symbol|Security Name|Test Issue|ETF\n"
        "AAPL|Apple Inc. - Common Stock|N|N\n"
        "QQQ|Invesco QQQ Trust, Series 1|N|Y\n"
        "SPY|State Street SPDR S&P 500 ETF Trust|N|Y\n"
    )

    universe = symbols.build_universe([symbols.parse_listing(text)])

    assert universe == ["AAPL"]


# --------------------------------------------------------------------------
# Cache store
# --------------------------------------------------------------------------


def test_save_cache_is_atomic_and_sorted(tmp_path) -> None:
    path = tmp_path / "cache.json"
    store.save_cache(path, {"MSFT": store.make_entry("MSFT"), "AAPL": store.make_entry("AAPL")})

    assert list(json.loads(path.read_text()).keys()) == ["AAPL", "MSFT"]
    assert not list(tmp_path.glob("*.tmp"))


def test_load_cache_tolerates_missing_and_empty(tmp_path) -> None:
    assert store.load_cache(tmp_path / "absent.json") == {}
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert store.load_cache(empty) == {}


def test_merge_preserves_existing_records() -> None:
    existing = {"AAPL": store.make_entry("AAPL", name="Apple")}
    incoming = {"MSFT": store.make_entry("MSFT", name="Microsoft")}

    merged = store.merge_caches(existing, incoming)

    assert set(merged) == {"AAPL", "MSFT"}
    assert merged["AAPL"]["name"] == "Apple"


def test_merge_prefers_data_over_a_not_found_record() -> None:
    good = store.make_entry("AAPL", name="Apple")
    missing = store.make_entry("AAPL", status=store.STATUS_NOT_FOUND)

    assert store.merge_caches({"AAPL": good}, {"AAPL": missing})["AAPL"]["name"] == "Apple"
    assert store.merge_caches({"AAPL": missing}, {"AAPL": good})["AAPL"]["name"] == "Apple"


def test_refreshed_record_replaces_the_older_one() -> None:
    old = store.make_entry("AAPL", price="100", fetched_at="2020-01-01T00:00:00+00:00")
    new = store.make_entry("AAPL", price="200", fetched_at="2026-01-01T00:00:00+00:00")

    assert store.merge_caches({"AAPL": old}, {"AAPL": new})["AAPL"]["price"] == "200"
    assert store.merge_caches({"AAPL": new}, {"AAPL": old})["AAPL"]["price"] == "200"


def test_legacy_entries_are_usable_but_read_as_maximally_stale() -> None:
    legacy = {"symbol": "AAPL", "name": "Apple"}

    assert store.is_usable(legacy)
    # No timestamp means unknown age, so it queues for refresh ahead of others.
    assert store.age_days(legacy) > 10_000


def test_master_row_hides_bookkeeping_fields() -> None:
    row = store.to_master_row(store.make_entry("AAPL", name="Apple", price="1.5"))

    assert set(row) == set(store.DATA_FIELDS)
    assert row["name"] == "Apple"

    blank = store.to_master_row(store.make_entry("XYZ", status=store.STATUS_NOT_FOUND))
    assert blank["symbol"] == "XYZ"
    assert blank["name"] == ""


# --------------------------------------------------------------------------
# API response classification
# --------------------------------------------------------------------------


def test_classify_success() -> None:
    result = classify({"Symbol": "AAPL", "Name": "Apple Inc", "Sector": "TECH"}, "AAPL")

    assert result.outcome is Outcome.OK
    assert result.entry is not None
    assert result.entry["name"] == "Apple Inc"


def test_classify_captures_pe_ratio() -> None:
    result = classify({"Symbol": "AAPL", "Name": "Apple", "PERatio": "31.5"}, "AAPL")

    assert result.entry is not None
    assert result.entry["peRatio"] == "31.5"
    assert store.to_master_row(result.entry)["peRatio"] == "31.5"


def test_classify_treats_missing_pe_ratio_as_blank() -> None:
    # Alpha Vantage sends the literal string "None" for a company with no P/E.
    result = classify({"Symbol": "BRK-A", "Name": "Berkshire", "PERatio": "None"}, "BRK-A")

    assert result.entry is not None
    assert result.entry["peRatio"] == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("31.5", "31.5"), ("None", ""), ("-", ""), ("N/A", ""), ("", ""), (None, "")],
)
def test_clean_number_drops_placeholders(raw: object, expected: str) -> None:
    assert clean_number(raw) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, Outcome.NOT_FOUND),
        ({"Error Message": "Invalid API call"}, Outcome.NOT_FOUND),
        ({"Note": "call frequency is 5 calls per minute"}, Outcome.THROTTLED),
        (
            {"Information": "Our standard API rate limit is 25 requests per day"},
            Outcome.QUOTA_EXHAUSTED,
        ),
        ({"Information": "This is a premium endpoint"}, Outcome.ERROR),
        ({"Unexpected": "shape"}, Outcome.ERROR),
    ],
)
def test_classify_failure_modes(payload: dict, expected: Outcome) -> None:
    assert classify(payload, "AAPL").outcome is expected


def test_mask_key_hides_the_secret() -> None:
    masked = mask_key("ABCDEFGHIJKLMNOP")
    assert masked.startswith("AB") and masked.endswith("OP")
    assert "CDEFGHIJKLMN" not in masked
    assert mask_key("xy") == "**"


# --------------------------------------------------------------------------
# Planning: what the daily budget gets spent on
# --------------------------------------------------------------------------


def _settings(**overrides) -> config.FetchSettings:
    overrides.setdefault("max_calls", 10)
    overrides.setdefault("refresh_after_days", 30)
    return config.FetchSettings(sleep_seconds=0.0, api_key="test-key", **overrides)


def _aged(symbol: str, days: float) -> store.Entry:
    stamp = (store.utc_now() - timedelta(days=days)).isoformat(timespec="seconds")
    return store.make_entry(symbol, name=symbol, fetched_at=stamp)


def test_plan_prioritises_missing_symbols() -> None:
    cache = {"OLD": _aged("OLD", 400)}

    missing, stale = cache_cli.plan_work(["NEW", "OLD"], cache, _settings())

    assert missing == ["NEW"]
    assert stale == ["OLD"]


def test_plan_refreshes_stalest_first() -> None:
    cache = {"A": _aged("A", 40), "B": _aged("B", 400), "C": _aged("C", 100)}

    _, stale = cache_cli.plan_work(["A", "B", "C"], cache, _settings())

    assert stale == ["B", "C", "A"]


def test_plan_leaves_fresh_entries_alone() -> None:
    cache = {"A": _aged("A", 5), "B": _aged("B", 29)}

    missing, stale = cache_cli.plan_work(["A", "B"], cache, _settings())

    assert missing == [] and stale == []


def test_plan_treats_legacy_entries_as_stale() -> None:
    # Entries written before timestamps existed must not be frozen forever.
    cache = {"A": {"symbol": "A", "name": "A"}}

    _, stale = cache_cli.plan_work(["A"], cache, _settings())

    assert stale == ["A"]


def test_fill_only_suppresses_refreshing() -> None:
    cache = {"A": _aged("A", 400)}

    missing, stale = cache_cli.plan_work(["A", "B"], cache, _settings(fill_only=True))

    assert missing == ["B"] and stale == []


def test_plan_backfills_entries_that_predate_a_field() -> None:
    # A record cached before peRatio existed would otherwise keep its blank
    # until it aged past the refresh threshold -- months of an empty column.
    incomplete = _aged("OLD", 1)
    del incomplete["peRatio"]
    cache = {"OLD": incomplete, "FRESH": _aged("FRESH", 1)}

    _, stale = cache_cli.plan_work(["FRESH", "OLD"], cache, _settings())

    assert stale == ["OLD"]


def test_backfill_outranks_merely_stale_entries() -> None:
    incomplete = _aged("NEWISH", 1)
    del incomplete["peRatio"]
    cache = {"NEWISH": incomplete, "ANCIENT": _aged("ANCIENT", 900)}

    _, stale = cache_cli.plan_work(["ANCIENT", "NEWISH"], cache, _settings())

    assert stale == ["NEWISH", "ANCIENT"]


def test_a_company_with_no_pe_is_not_refetched_forever() -> None:
    # Alpha Vantage reports "None" for companies without earnings, stored as a
    # blank. That is an answer, not a gap, so it must not consume quota daily.
    cache = {"A": _aged("A", 1)}
    assert cache["A"]["peRatio"] == ""

    _, stale = cache_cli.plan_work(["A"], cache, _settings())

    assert stale == []


def test_fill_only_also_suppresses_backfilling() -> None:
    incomplete = _aged("A", 1)
    del incomplete["peRatio"]

    _, stale = cache_cli.plan_work(["A"], {"A": incomplete}, _settings(fill_only=True))

    assert stale == []


def test_plan_skips_known_missing_symbols_unless_asked() -> None:
    cache = {"X": store.make_entry("X", status=store.STATUS_NOT_FOUND)}

    assert cache_cli.plan_work(["X"], cache, _settings())[0] == []
    assert cache_cli.plan_work(["X"], cache, _settings(retry_missing=True))[0] == ["X"]


def test_plan_deduplicates_and_normalises() -> None:
    missing, _ = cache_cli.plan_work(["aapl", "AAPL", "brk.b"], {}, _settings())

    assert missing == ["AAPL", "BRK-B"]


# --------------------------------------------------------------------------
# Fetch loop
# --------------------------------------------------------------------------


class FakeClient:
    """Replays queued results and records the symbols requested."""

    def __init__(self, results: list[FetchResult]) -> None:
        self._results = list(results)
        self.requested: list[str] = []

    def fetch_overview(self, symbol: str) -> FetchResult:
        self.requested.append(symbol)
        if not self._results:
            raise AssertionError(f"Unexpected extra request for {symbol}")
        return self._results.pop(0)


def _ok(symbol: str) -> FetchResult:
    return FetchResult(Outcome.OK, symbol, entry=store.make_entry(symbol, name=symbol))


def test_a_fully_cached_universe_still_does_work() -> None:
    # The previous pipeline skipped every cached ticker, so once the universe
    # was complete the daily run fetched nothing and the data froze.
    cache = {"A": _aged("A", 400), "B": _aged("B", 400)}
    client = FakeClient([_ok("A"), _ok("B")])

    summary = cache_cli.fill_cache(["A", "B"], cache, client, _settings(), sleep=lambda _: None)

    assert summary.refreshed == 2
    assert summary.fetched == 0


def test_fill_cache_stops_immediately_on_quota_exhaustion() -> None:
    client = FakeClient(
        [_ok("A"), FetchResult(Outcome.QUOTA_EXHAUSTED, "B", message="25 per day")]
    )

    summary = cache_cli.fill_cache(["A", "B", "C", "D"], {}, client, _settings(), sleep=lambda _: None)

    # The old loop kept calling for every remaining ticker after the limit hit.
    assert client.requested == ["A", "B"]
    assert summary.fetched == 1
    assert summary.stopped_early


def test_fill_cache_respects_the_call_budget() -> None:
    client = FakeClient([_ok(sym) for sym in "AB"])

    summary = cache_cli.fill_cache(list("ABCDE"), {}, client, _settings(max_calls=2), sleep=lambda _: None)

    assert summary.attempted == 2
    assert len(summary.entries) == 2


def test_fill_cache_remembers_symbols_with_no_data() -> None:
    client = FakeClient([FetchResult(Outcome.NOT_FOUND, "A", message="Empty response")])

    summary = cache_cli.fill_cache(["A"], {}, client, _settings(), sleep=lambda _: None)

    assert summary.not_found == 1
    assert summary.entries["A"]["status"] == store.STATUS_NOT_FOUND
    # A recorded miss must not be re-queried on the next run.
    assert cache_cli.plan_work(["A"], summary.entries, _settings())[0] == []


def test_fill_cache_retries_once_after_a_throttle() -> None:
    client = FakeClient([FetchResult(Outcome.THROTTLED, "A", message="5 per minute"), _ok("A")])
    slept: list[float] = []

    summary = cache_cli.fill_cache(["A"], {}, client, _settings(), sleep=slept.append)

    assert summary.fetched == 1
    assert cache_cli.THROTTLE_BACKOFF_SECONDS in slept


def test_fill_cache_continues_past_a_transport_error() -> None:
    client = FakeClient([FetchResult(Outcome.ERROR, "A", message="boom"), _ok("B")])

    summary = cache_cli.fill_cache(["A", "B"], {}, client, _settings(), sleep=lambda _: None)

    assert summary.errors == 1
    assert summary.fetched == 1
    assert not summary.stopped_early


# --------------------------------------------------------------------------
# Quotes (Yahoo). The fetcher is injected so tests never touch the network.
# --------------------------------------------------------------------------


def _closes(value: float, count: int = quotes.MA_WINDOW) -> list[float]:
    return [value] * count


def test_moving_average_needs_a_full_window() -> None:
    assert quotes.moving_average(_closes(10.0)) == 10.0
    assert quotes.moving_average([10.0] * (quotes.MA_WINDOW - 1)) is None


def test_fetch_quotes_formats_like_the_cache() -> None:
    cache = {
        "AAPL": store.make_entry("AAPL", price="100", shares_outstanding="10")
    }
    series = _closes(110.0)[:-1] + [120.0]  # 50d average 110.2, last close 120

    found, failed = quotes.fetch_quotes(
        ["AAPL"], cache, downloader=lambda batch: {"AAPL": series}
    )

    assert failed == []
    assert found["AAPL"].price == "110.20"
    # Market cap is shares x latest close, not shares x the average.
    assert found["AAPL"].market_cap == "1200"


def test_market_cap_is_left_alone_without_shares_outstanding() -> None:
    # Publishing a guess would be worse than keeping the last known figure.
    cache = {"AAPL": store.make_entry("AAPL", price="100", market_cap="1000")}

    found, _ = quotes.fetch_quotes(
        ["AAPL"], cache, downloader=lambda batch: {"AAPL": _closes(110.0)}
    )

    assert found["AAPL"].price == "110.00"
    assert found["AAPL"].market_cap == ""

    quotes.apply_quotes(cache, found.values(), stamp="now")
    assert cache["AAPL"]["marketCap"] == "1000"


def test_fetch_quotes_computes_the_150_day_average() -> None:
    # A full year of history is enough for both the 50- and 150-day windows.
    series = _closes(100.0, count=quotes.MA_150_WINDOW)

    found, failed = quotes.fetch_quotes(
        ["AAPL"], {}, downloader=lambda batch: {"AAPL": series}
    )

    assert failed == []
    assert found["AAPL"].ma_150 == "100.00"


def test_fetch_quotes_leaves_ma150_blank_without_a_full_window() -> None:
    # Enough history for the 50-day price but not the 150-day long average.
    series = _closes(100.0, count=quotes.MA_WINDOW)

    found, _ = quotes.fetch_quotes(
        ["AAPL"], {}, downloader=lambda batch: {"AAPL": series}
    )

    assert found["AAPL"].price == "100.00"
    assert found["AAPL"].ma_150 == ""


def test_fetch_quotes_batches_the_universe() -> None:
    seen: list[int] = []

    def downloader(batch):
        seen.append(len(batch))
        return {s: _closes(5.0) for s in batch}

    symbols_in = [f"S{i}" for i in range(450)]
    found, failed = quotes.fetch_quotes(symbols_in, {}, batch_size=200, downloader=downloader)

    # 450 symbols must cost 3 requests, not 450.
    assert seen == [200, 200, 50]
    assert len(found) == 450 and failed == []


def test_one_failed_batch_does_not_lose_the_others() -> None:
    def downloader(batch):
        if "BAD" in batch:
            raise RuntimeError("rate limited")
        return {s: _closes(5.0) for s in batch}

    found, failed = quotes.fetch_quotes(
        ["BAD", "GOOD"], {}, batch_size=1, downloader=downloader
    )

    assert failed == ["BAD"]
    assert "GOOD" in found


def test_fetch_quotes_reports_symbols_without_enough_history() -> None:
    found, failed = quotes.fetch_quotes(
        ["ZZZZ"], {}, downloader=lambda batch: {"ZZZZ": [1.0, 2.0]}
    )

    assert found == {} and failed == ["ZZZZ"]


# --------------------------------------------------------------------------
# Liquidity ranking: which tickers make the universe
# --------------------------------------------------------------------------


class _FakeResponse:
    """Stands in for the screener response in tests."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_selects_the_most_traded_symbols() -> None:
    volumes = {"A": 100.0, "B": 500.0, "C": 300.0, "D": 50.0}

    kept = liquidity.select_universe(list("ABCD"), volumes, limit=2)

    assert kept == ["B", "C"]  # sorted output, chosen by volume


def test_symbols_without_volume_are_excluded() -> None:
    # No price data means the row could only ever be blank.
    volumes = {"A": 100.0, "B": 0.0}

    assert liquidity.select_universe(["A", "B", "GONE"], volumes, limit=10) == ["A"]


def test_incumbents_survive_just_past_the_cutoff() -> None:
    # Hysteresis: a name hovering at the boundary should not flip weekly.
    volumes = {f"S{i}": float(100 - i) for i in range(20)}
    candidates = list(volumes)

    fresh = liquidity.select_universe(candidates, volumes, limit=10)
    assert "S11" not in fresh

    incumbent = liquidity.select_universe(
        candidates, volumes, limit=10, existing=["S11"]
    )
    assert "S11" in incumbent


def test_incumbents_are_dropped_once_far_enough_down() -> None:
    volumes = {f"S{i}": float(100 - i) for i in range(20)}

    kept = liquidity.select_universe(list(volumes), volumes, limit=10, existing=["S19"])

    assert "S19" not in kept


def test_shortlist_keeps_headroom_above_the_limit() -> None:
    screened = {f"S{i}": float(100 - i) for i in range(50)}

    picked = liquidity.shortlist(list(screened), screened, limit=10, factor=2.0)

    # Twice the limit, most active first, so one quiet session cannot push a
    # genuinely liquid name out of contention.
    assert picked == [f"S{i}" for i in range(20)]


def test_shortlist_measures_incumbents_below_the_cut() -> None:
    screened = {f"S{i}": float(100 - i) for i in range(50)}

    picked = liquidity.shortlist(
        list(screened), screened, limit=5, factor=2.0, keep=["S40"]
    )

    # Retention in select_universe can only apply to symbols we measured.
    assert "S40" in picked


def test_shortlist_drops_symbols_the_screener_cannot_price() -> None:
    screened = {"A": 100.0}

    picked = liquidity.shortlist(["A", "DELISTED"], screened, limit=10, keep=["DELISTED"])

    assert picked == ["A"]


def test_screener_ignores_a_truncated_response(monkeypatch) -> None:
    rows = [{"symbol": "A", "lastsale": "$10.00", "volume": "100"}]
    monkeypatch.setattr(
        liquidity.requests, "get", lambda *a, **k: _FakeResponse({"data": {"rows": rows}})
    )

    # Too few rows to shortlist from: better to sweep than to silently drop.
    assert liquidity.screener_dollar_volumes() == {}


def test_screener_parses_prices_volumes_and_class_shares(monkeypatch) -> None:
    rows = [{"symbol": "BRK/B", "lastsale": "$1,234.50", "volume": "2,000"}]
    rows += [
        {"symbol": f"S{i}", "lastsale": "$1.00", "volume": "1"}
        for i in range(liquidity.MIN_SCREENER_ROWS)
    ]
    rows.append({"symbol": "HALTED", "lastsale": "$5.00", "volume": "--"})
    monkeypatch.setattr(
        liquidity.requests, "get", lambda *a, **k: _FakeResponse({"data": {"rows": rows}})
    )

    volumes = liquidity.screener_dollar_volumes()

    assert volumes["BRK-B"] == 1234.50 * 2000
    assert "HALTED" not in volumes  # no volume means nothing to rank on


def test_fetch_dollar_volumes_batches_and_survives_a_bad_batch() -> None:
    seen: list[int] = []

    def downloader(batch):
        seen.append(len(batch))
        if "BAD" in batch:
            raise RuntimeError("rate limited")
        return {s: 1000.0 for s in batch}

    symbols_in = [f"S{i}" for i in range(250)] + ["BAD"]
    volumes = liquidity.fetch_dollar_volumes(
        symbols_in, batch_size=200, downloader=downloader, sleep=lambda _: None
    )

    assert seen == [200, 51]
    # The failed batch is skipped without losing the successful one.
    assert len(volumes) == 200
    assert "BAD" not in volumes


def test_fetch_dollar_volumes_paces_requests() -> None:
    slept: list[float] = []
    liquidity.fetch_dollar_volumes(
        [f"S{i}" for i in range(5)],
        batch_size=1,
        pause_seconds=2.0,
        downloader=lambda batch: {s: 1.0 for s in batch},
        sleep=slept.append,
    )

    # A pause between each request, but none after the last.
    assert slept == [2.0, 2.0, 2.0, 2.0]


def test_alpha_vantage_records_shares_outstanding() -> None:
    # Captured so the daily quote refresh can compute market cap exactly.
    result = classify(
        {"Symbol": "AAPL", "Name": "Apple", "SharesOutstanding": "14594180000"}, "AAPL"
    )

    assert result.entry is not None
    assert result.entry["sharesOutstanding"] == "14594180000"
    # It must not reach the published CSV.
    assert "sharesOutstanding" not in store.to_master_row(result.entry)


def test_apply_quotes_updates_only_volatile_fields() -> None:
    cache = {"AAPL": store.make_entry("AAPL", name="Apple", price="1", market_cap="2")}

    changed = quotes.apply_quotes(
        cache,
        [quotes.Quote("AAPL", price="3.00", market_cap="4")],
        stamp="2026-09-09T00:00:00+00:00",
    )

    assert changed == 1
    assert cache["AAPL"]["price"] == "3.00"
    assert cache["AAPL"]["marketCap"] == "4"
    assert cache["AAPL"]["name"] == "Apple"  # fundamentals untouched
    assert cache["AAPL"]["quoted_at"] == "2026-09-09T00:00:00+00:00"


def test_apply_quotes_writes_the_150_day_average() -> None:
    cache = {"AAPL": store.make_entry("AAPL", name="Apple", price="1")}

    changed = quotes.apply_quotes(
        cache,
        [quotes.Quote("AAPL", price="3.00", market_cap="4", ma_150="2.50")],
        stamp="now",
    )

    assert changed == 1
    assert cache["AAPL"]["ma150"] == "2.50"


def test_apply_quotes_never_blanks_existing_data() -> None:
    # A partial Yahoo response must not erase a good cached value.
    cache = {"AAPL": store.make_entry("AAPL", price="1", market_cap="2")}

    quotes.apply_quotes(
        cache, [quotes.Quote("AAPL", price="", market_cap="")], stamp="now"
    )

    assert cache["AAPL"]["price"] == "1"
    assert cache["AAPL"]["marketCap"] == "2"
    assert "quoted_at" not in cache["AAPL"]


def test_apply_quotes_ignores_symbols_without_fundamentals() -> None:
    cache: dict = {}

    assert quotes.apply_quotes(cache, [quotes.Quote("NEW", "1.00", "2")], stamp="now") == 0
    assert cache == {}


def test_unchanged_quotes_do_not_restamp_the_row() -> None:
    # Keeps identical data from producing a daily commit.
    cache = {"AAPL": store.make_entry("AAPL", price="3.00", market_cap="4")}

    changed = quotes.apply_quotes(
        cache, [quotes.Quote("AAPL", price="3.00", market_cap="4")], stamp="now"
    )

    assert changed == 0
    assert "quoted_at" not in cache["AAPL"]


def test_last_updated_reports_the_most_recent_source() -> None:
    entry = store.make_entry("AAPL", fetched_at="2026-01-01T00:00:00+00:00")
    assert store.last_updated(entry) == "2026-01-01T00:00:00+00:00"

    entry["quoted_at"] = "2026-09-09T00:00:00+00:00"
    assert store.last_updated(entry) == "2026-09-09T00:00:00+00:00"


# --------------------------------------------------------------------------
# Published universe (the CSV the Google Sheet renders verbatim)
# --------------------------------------------------------------------------

NASDAQ_HEADER = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot|ETF|NextShares"
)
OTHER_HEADER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|"
    "Test Issue|NASDAQ Symbol"
)

COL = {name: index for index, name in enumerate(sheet.SHEET_COLUMNS)}


def _nasdaq_file(*rows: str) -> str:
    return "\n".join((NASDAQ_HEADER, *rows, "File Creation Time: 09092026"))


def _other_file(*rows: str) -> str:
    return "\n".join((OTHER_HEADER, *rows, "File Creation Time: 09092026"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("brk-b", "BRK.B"), ("BRK/B", "BRK.B"), ("BRK.B", "BRK.B"), (" aapl ", "AAPL")],
)
def test_display_symbol_uses_the_dotted_class_form(raw: str, expected: str) -> None:
    # The sheet and GOOGLEFINANCE both want dots; Yahoo and the screener do not.
    assert sheet.display_symbol(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("$146.85", 146.85), ("1,234", 1234.0), ("12", 12.0), ("", None), ("N/A", None)],
)
def test_to_number_reads_display_formatting(raw: str, expected) -> None:
    assert sheet.to_number(raw) == expected


def test_to_number_treats_zero_as_not_reported() -> None:
    # A zero market cap means "not disclosed", not "worth nothing"; publishing
    # it would corrupt numeric filters on the sheet.
    assert sheet.to_number("0") is None


def test_nasdaq_listing_becomes_a_sheet_row() -> None:
    text = _nasdaq_file("AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N")

    rows = sheet.parse_nasdaq_listed(text, {}, {})

    assert len(rows) == 1
    assert rows[0][COL["symbol"]] == "AAPL"
    assert rows[0][COL["gf_ticker"]] == "NASDAQ:AAPL"
    assert rows[0][COL["exchange"]] == "NASDAQ"
    assert rows[0][COL["type"]] == "Stock"


def test_listings_drop_test_issues_and_unquotable_symbols() -> None:
    text = _nasdaq_file(
        "TEST|Test Issue - Common Stock|Q|Y|N|100|N|N",
        "ABCDEF|Too Long - Common Stock|Q|N|N|100|N|N",
        "ABR$D|Preferred Series D|Q|N|N|100|N|N",
        "GOOD|Good Co - Common Stock|Q|N|N|100|N|N",
    )

    rows = sheet.parse_nasdaq_listed(text, {}, {})

    assert [row[COL["symbol"]] for row in rows] == ["GOOD"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Armada Acquisition Corp. III - Warrant", "Warrant"),
        ("Abony Acquisition Corp. I - Units", "Unit"),
        ("Apogee Acquisition Corp - Rights", "Right"),
        ("Strategy Inc - 10.00% Series A Perpetual Strife Preferred", "Preferred"),
        ("AT&T Inc. 5.350% Global Notes due 2066", "Note"),
        ("Ares Acquisition Corporation III Units, each consisting of one share", "Unit"),
    ],
)
def test_non_common_issues_are_named_not_called_stock(name: str, expected: str) -> None:
    # 1,202 of these were published as "Stock". They carry ordinary five-letter
    # tickers, and no vendor reports a sector or P/E for a warrant, so they are
    # most of what looks like an empty row in the sheet.
    assert sheet.security_type(name, is_etf=False) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Energy Transfer LP Common Units",
        "MPLX LP Common Units Representing Limited Partner Interests",
        "Plains All American Pipeline, L.P. - Common Units representing LP interests",
        # The word "unit" here describes the ADR's terms, not the issue.
        "Banco Santander Brasil SA American Depositary Shares, each representing one unit",
        "Unit Corporation - Common Stock",
        "Berkshire Hathaway Inc. New Common Stock",
    ],
)
def test_real_equities_are_not_mistaken_for_units(name: str) -> None:
    # Master limited partnerships are ordinary listed equity; Energy Transfer
    # and MPLX are both well over $50B and must not be filtered out with the
    # SPAC units they share a word with.
    assert sheet.security_type(name, is_etf=False) == "Stock"


def test_a_fund_is_an_etf_whatever_its_name_says() -> None:
    assert sheet.security_type("Some Rights Strategy ETF", is_etf=True) == "ETF"


def test_stocks_only_now_excludes_warrants_and_units() -> None:
    listing = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF\n"
        "AACI|Armada Acquisition Corp. III - Class A Ordinary Shares|Q|N|N|100|N\n"
        "AACIW|Armada Acquisition Corp. III - Warrant|Q|N|N|100|N\n"
        "AACIU|Armada Acquisition Corp. III - Units|Q|N|N|100|N\n"
    )

    kept = sheet.build_rows(listing, "", {}, {}, stocks_only=True)

    assert [row[0] for row in kept] == ["AACI"]


def test_etfs_are_flagged_from_the_listing_column() -> None:
    text = _nasdaq_file("QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N")

    rows = sheet.parse_nasdaq_listed(text, {}, {})

    assert rows[0][COL["type"]] == "ETF"


def test_other_listed_maps_the_nyse_family() -> None:
    text = _other_file(
        "GE|GE Aerospace|N|GE|N|100|N|",
        "IMO|Imperial Oil|A|IMO|N|100|N|",
        "SPY|SPDR S&P 500 ETF|P|SPY|Y|100|N|",
    )

    rows = sheet.parse_other_listed(text, {}, {})
    by_symbol = {row[COL["symbol"]]: row for row in rows}

    assert by_symbol["GE"][COL["exchange"]] == "NYSE"
    assert by_symbol["IMO"][COL["exchange"]] == "NYSE American"
    assert by_symbol["SPY"][COL["exchange"]] == "NYSE Arca"
    assert by_symbol["SPY"][COL["gf_ticker"]] == "NYSEARCA:SPY"


def test_cboe_listings_are_kept_whether_or_not_they_are_index_members() -> None:
    # Cboe was admitted only for index members, which kept CBOE itself but
    # excluded 1,616 real listings -- VXX and Goldman's physical gold fund
    # among them. The type column separates funds now, so all of them stay.
    text = _other_file(
        "CBOE|Cboe Global Markets|Z|CBOE|N|100|N|",
        "VXX|iPath Series B S&P 500 VIX Short-Term Futures ETN|Z|VXX|N|100|N|",
        "AAAU|Goldman Sachs Physical Gold ETF Shares|Z|AAAU|Y|100|N|",
    )
    sp500 = {"CBOE": {"sector": "Financials", "sub_industry": "Financial Exchanges"}}

    rows = sheet.parse_other_listed(text, sp500, {})
    by_symbol = {row[COL["symbol"]]: row for row in rows}

    assert set(by_symbol) == {"CBOE", "VXX", "AAAU"}
    assert by_symbol["CBOE"][COL["exchange"]] == "Cboe"
    assert by_symbol["CBOE"][COL["sp500"]] == sheet.SP500_MARK
    assert by_symbol["AAAU"][COL["type"]] == "ETF"


def test_an_unknown_venue_code_still_needs_index_membership() -> None:
    # A code nobody has seen before must not be able to flood the directory.
    text = _other_file(
        "REAL|Some Index Member|Q|REAL|N|100|N|",
        "NOISE|Something Else|Q|NOISE|N|100|N|",
    )
    sp500 = {"REAL": {"sector": "Financials", "sub_industry": "Banks"}}

    rows = sheet.parse_other_listed(text, sp500, {})

    assert [row[COL["symbol"]] for row in rows] == ["REAL"]
    assert rows[0][COL["exchange"]] == "Other"


def test_index_gics_beats_the_vendor_classification() -> None:
    text = _nasdaq_file("AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N")
    sp500 = {"AAPL": {"sector": "Information Technology", "sub_industry": "Hardware"}}
    enrichment = {"AAPL": {"sector": "Technology", "industry": "Consumer Electronics"}}

    rows = sheet.parse_nasdaq_listed(text, sp500, enrichment)

    assert rows[0][COL["sector"]] == "Information Technology"
    assert rows[0][COL["industry"]] == "Hardware"


def test_percent_above_the_average_is_precomputed() -> None:
    # Precomputing this is the entire reason the column is viable: as a live
    # formula it needed a year of history per row.
    text = _nasdaq_file("AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N")
    enrichment = {"AAPL": {"price": 110.0, "sma150": 100.0}}

    rows = sheet.parse_nasdaq_listed(text, {}, enrichment)

    assert rows[0][COL["pct_above_sma"]] == pytest.approx(0.10)


def test_percent_above_is_blank_without_both_inputs() -> None:
    text = _nasdaq_file("AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N")

    rows = sheet.parse_nasdaq_listed(text, {}, {"AAPL": {"price": 110.0}})

    assert rows[0][COL["pct_above_sma"]] is None


def test_market_data_outranks_both_other_sources_on_numbers() -> None:
    # The whole point of the market pass: it reaches every listing, while the
    # Alpha Vantage cache reaches a few hundred on a 25-call daily budget.
    merged = sheet.merge_enrichment(
        {"AAPL": {"sector": "Technology", "market_cap": 1.0, "price": 1.0}},
        {"AAPL": {"pe": 99.0, "sma150": 99.0, "market_cap": 2.0}},
        {"AAPL": {"pe": 31.5, "sma150": 200.0, "market_cap": 3e12, "price": 250.0}},
    )

    assert merged["AAPL"]["pe"] == 31.5
    assert merged["AAPL"]["sma150"] == 200.0
    assert merged["AAPL"]["market_cap"] == 3e12
    assert merged["AAPL"]["price"] == 250.0
    # Yahoo does not classify, so sector still comes from the screener.
    assert merged["AAPL"]["sector"] == "Technology"


def test_market_only_symbols_still_reach_the_sheet() -> None:
    # A symbol the screener and the cache both miss must not be dropped, or
    # P/E would stay confined to the curated few hundred all over again.
    merged = sheet.merge_enrichment({}, {}, {"ZZZ": {"pe": 12.0, "sma150": 8.0}})

    assert merged["ZZZ"]["pe"] == 12.0
    assert merged["ZZZ"]["sma150"] == 8.0


def test_cache_still_fills_what_the_market_pass_missed() -> None:
    merged = sheet.merge_enrichment(
        {}, {"AAPL": {"pe": 31.5, "sma150": 200.0}}, {"AAPL": {"price": 250.0}}
    )

    assert merged["AAPL"]["pe"] == 31.5
    assert merged["AAPL"]["sma150"] == 200.0


def test_merge_prefers_the_screener_but_keeps_pipeline_only_fields() -> None:
    screener = {"AAPL": {"sector": "Technology", "market_cap": 3e12, "price": 250.0}}
    pipeline = {
        "AAPL": {
            "sector": "Stale",
            "market_cap": 1.0,
            "price": 1.0,
            "pe": 31.5,
            "sma150": 200.0,
        }
    }

    merged = sheet.merge_enrichment(screener, pipeline)

    assert merged["AAPL"]["sector"] == "Technology"
    assert merged["AAPL"]["market_cap"] == 3e12
    # P/E and the long average exist nowhere else, so they always survive.
    assert merged["AAPL"]["pe"] == 31.5
    assert merged["AAPL"]["sma150"] == 200.0


def test_merge_falls_back_to_the_pipeline_when_the_screener_is_silent() -> None:
    merged = sheet.merge_enrichment({}, {"AAPL": {"sector": "Tech", "price": 9.0}})

    assert merged["AAPL"]["sector"] == "Tech"
    assert merged["AAPL"]["price"] == 9.0


def test_cache_enrichment_skips_symbols_with_no_data() -> None:
    cache = {
        "AAPL": store.make_entry("AAPL", sector="Tech", pe_ratio="31.5"),
        "XYZ": store.make_entry("XYZ", status=store.STATUS_NOT_FOUND),
    }
    cache["AAPL"]["ma150"] = "289.04"

    enrichment = sheet.enrichment_from_cache(cache)

    assert enrichment["AAPL"]["pe"] == 31.5
    assert enrichment["AAPL"]["sma150"] == 289.04
    assert "XYZ" not in enrichment


def test_cache_enrichment_keys_on_the_dotted_symbol() -> None:
    # The cache stores BRK-B; the listing files and the sheet use BRK.B.
    cache = {"BRK-B": store.make_entry("BRK-B", sector="Financials")}

    assert "BRK.B" in sheet.enrichment_from_cache(cache)


def test_build_rows_sorts_and_can_exclude_etfs() -> None:
    nasdaq = _nasdaq_file(
        "ZZZZ|Zeta Corp - Common Stock|Q|N|N|100|N|N",
        "QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N",
    )
    other = _other_file("AAA|Alpha Inc|N|AAA|N|100|N|")

    everything = sheet.build_rows(nasdaq, other, {}, {})
    assert [row[COL["symbol"]] for row in everything] == ["AAA", "QQQ", "ZZZZ"]

    stocks = sheet.build_rows(nasdaq, other, {}, {}, stocks_only=True)
    assert [row[COL["symbol"]] for row in stocks] == ["AAA", "ZZZZ"]


def test_coverage_counts_only_populated_cells() -> None:
    text = _nasdaq_file(
        "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
        "BARE|Bare Co - Common Stock|Q|N|N|100|N|N",
    )
    rows = sheet.parse_nasdaq_listed(text, {}, {"AAPL": {"sector": "Tech", "pe": 31.5}})

    filled = sheet.coverage(rows)

    assert filled["sector"] == 1
    assert filled["pe"] == 1
    assert filled["sma150"] == 0


def test_screener_rows_are_indexed_by_display_symbol() -> None:
    rows = [{"symbol": "BRK/B", "sector": "Financials", "marketCap": "1,000", "lastsale": "$5.00"}]

    lookup = sheet.parse_screener(rows)

    assert lookup["BRK.B"]["market_cap"] == 1000.0
    assert lookup["BRK.B"]["price"] == 5.0


# --------------------------------------------------------------------------
# Whole-universe market data
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("display", "expected"), [("BRK.B", "BRK-B"), ("aapl", "AAPL"), (" A ", "A")]
)
def test_yahoo_wants_the_dashed_class_form(display: str, expected: str) -> None:
    assert market.yahoo_symbol(display) == expected


def test_quotes_come_back_keyed_on_the_dotted_symbol() -> None:
    # The sheet publishes BRK.B; only the wire uses BRK-B.
    def fetcher(batch):
        assert list(batch) == ["BRK-B"]
        return [{"symbol": "BRK-B", "regularMarketPrice": 505.83, "trailingPE": 12.7}]

    fields = market.fetch_quote_fields(["BRK.B"], fetcher=fetcher)

    assert fields["BRK.B"]["price"] == 505.83
    assert fields["BRK.B"]["pe"] == 12.7


def test_a_zero_is_read_as_not_reported() -> None:
    # Yahoo sends 0 for an unknown cap and for a company with no earnings.
    # Published as a number, both would sort to the top of a numeric filter.
    def fetcher(batch):
        return [{"symbol": "ZZZ", "marketCap": 0, "trailingPE": 0, "regularMarketPrice": 3.0}]

    fields = market.fetch_quote_fields(["ZZZ"], fetcher=fetcher)

    assert fields["ZZZ"]["market_cap"] is None
    assert fields["ZZZ"]["pe"] is None
    assert fields["ZZZ"]["price"] == 3.0


def test_quotes_are_batched_and_paced() -> None:
    seen, pauses = [], []

    def fetcher(batch):
        seen.append(list(batch))
        return [{"symbol": symbol, "trailingPE": 1.0} for symbol in batch]

    fields = market.fetch_quote_fields(
        [f"S{n}" for n in range(250)],
        batch_size=100,
        fetcher=fetcher,
        pause_seconds=0.5,
        sleep=pauses.append,
    )

    assert [len(batch) for batch in seen] == [100, 100, 50]
    assert len(fields) == 250
    # Paced between batches, but not after the last one.
    assert pauses == [0.5, 0.5]


def test_one_refused_quote_batch_does_not_lose_the_rest() -> None:
    def fetcher(batch):
        if "S0" in batch:
            raise RuntimeError("429 Too Many Requests")
        return [{"symbol": symbol, "trailingPE": 1.0} for symbol in batch]

    fields = market.fetch_quote_fields(
        [f"S{n}" for n in range(4)], batch_size=2, fetcher=fetcher, pause_seconds=0
    )

    assert set(fields) == {"S2", "S3"}


def test_the_average_needs_a_full_window() -> None:
    closes = {"SHORT": [10.0] * 149, "LONG": [10.0] * 150}

    sma = market.fetch_sma(
        ["SHORT", "LONG"], downloader=lambda batch: closes, sleep=lambda _: None
    )

    # A month-old listing gets a blank, not a short-window number that would
    # mean something different from every other row in the column.
    assert "SHORT" not in sma
    assert sma["LONG"] == 10.0


def test_a_throttled_symbol_is_retried_on_a_second_pass() -> None:
    # Yahoo throttles partway through a run this size, and a throttled symbol
    # looks exactly like one with no history: both come back empty.
    calls = []

    def downloader(batch):
        calls.append(list(batch))
        if len(calls) == 1:
            return {}
        return {symbol: [10.0] * 150 for symbol in batch}

    sma = market.fetch_sma(["AAPL"], downloader=downloader, sleep=lambda _: None)

    assert sma["AAPL"] == 10.0
    assert len(calls) == 2


def test_a_symbol_answered_first_time_is_not_asked_again() -> None:
    calls = []

    def downloader(batch):
        calls.append(list(batch))
        return {symbol: [10.0] * 150 for symbol in batch}

    market.fetch_sma(["AAPL", "MSFT"], downloader=downloader, sleep=lambda _: None)

    # One pass only: the retry exists for gaps, not as a second full run.
    assert calls == [["AAPL", "MSFT"]]


def test_the_retry_pass_only_covers_what_is_missing() -> None:
    calls = []

    def downloader(batch):
        calls.append(list(batch))
        return {"AAPL": [10.0] * 150} if len(calls) == 1 else {"MSFT": [20.0] * 150}

    sma = market.fetch_sma(
        ["AAPL", "MSFT"], downloader=downloader, sleep=lambda _: None
    )

    assert calls[1] == ["MSFT"]
    assert sma == {"AAPL": 10.0, "MSFT": 20.0}


def test_the_retry_pass_waits_before_trying_again() -> None:
    pauses = []

    market.fetch_sma(
        ["AAPL"],
        downloader=lambda batch: {},
        attempts=2,
        retry_pause_seconds=20.0,
        batch_pause_seconds=0,
        sleep=pauses.append,
    )

    # Waited once, before the second pass -- not before the first.
    assert pauses == [20.0]


def test_an_implausible_pe_is_dropped_rather_than_published() -> None:
    # A reverse split that restates the price but not the earnings leaves a
    # ratio like 0.004, which renders as "0.00" and sorts to the top of a
    # cheap-stock filter.
    def fetcher(batch):
        return [
            {"symbol": "JZ", "trailingPE": 0.001123, "regularMarketPrice": 0.88},
            {"symbol": "KALU", "trailingPE": 12.45, "regularMarketPrice": 167.8},
        ]

    fields = market.fetch_quote_fields(["JZ", "KALU"], fetcher=fetcher)

    assert fields["JZ"]["pe"] is None
    assert fields["JZ"]["price"] == 0.88
    assert fields["KALU"]["pe"] == 12.45


def test_a_bad_pe_already_on_disk_is_not_restored(tmp_path) -> None:
    # The fallback replaces a missing value with yesterday's, so filtering only
    # at fetch time would resurrect the artefact on every single run.
    path = tmp_path / "market_data.json"
    path.write_text('{"JZ": {"pe": 0.001123, "price": 0.88}}')

    loaded = market.load(path)

    assert loaded["JZ"].pe is None
    assert loaded["JZ"].price == 0.88
    assert market.merge_over_previous({}, loaded)["JZ"].pe is None


def test_a_genuinely_enormous_pe_is_kept() -> None:
    # CrowdStrike really does trade at thousands of times trailing earnings.
    # Only the implausible low end is filtered.
    def fetcher(batch):
        return [{"symbol": "CRWD", "trailingPE": 5250.5}]

    assert market.fetch_quote_fields(["CRWD"], fetcher=fetcher)["CRWD"]["pe"] == 5250.5


def test_yesterdays_value_survives_a_symbol_yahoo_skipped() -> None:
    previous = {"AAPL": market.MarketRow(price=1.0, pe=30.0, sma150=200.0)}
    fresh = {"AAPL": market.MarketRow(price=250.0)}

    merged = market.merge_over_previous(fresh, previous)

    assert merged["AAPL"].price == 250.0
    # Not blanked just because today's run had no P/E for it.
    assert merged["AAPL"].pe == 30.0
    assert merged["AAPL"].sma150 == 200.0


def test_a_symbol_only_in_yesterdays_file_is_kept() -> None:
    merged = market.merge_over_previous({}, {"AAPL": market.MarketRow(pe=30.0)})

    assert merged["AAPL"].pe == 30.0


def test_market_data_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "market_data.json"
    rows = {
        "AAPL": market.MarketRow(price=250.0, market_cap=3e12, pe=31.5, sma150=200.0),
        "ZZZ": market.MarketRow(price=1.5),
    }

    market.save(path, rows)

    assert market.load(path) == rows
    # Absent fields stay absent rather than serialising as null.
    assert "pe" not in json.loads(path.read_text())["ZZZ"]


def test_loading_a_missing_market_file_is_not_an_error(tmp_path) -> None:
    assert market.load(tmp_path / "nope.json") == {}


def test_coverage_counts_populated_fields() -> None:
    rows = {
        "A": market.MarketRow(price=1.0, pe=2.0),
        "B": market.MarketRow(price=1.0),
    }

    assert market.coverage(rows) == {
        "price": 2,
        "market_cap": 0,
        "pe": 1,
        "sma150": 0,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, ""), (1234.0, 1234), (0.0918586789, 0.091859), ("AAPL", "AAPL")],
)
def test_cells_are_written_compactly(value, expected) -> None:
    # Full binary precision on 11,000 rows bloats the file the sheet downloads.
    assert build_universe.format_cell(value) == expected


def test_written_csv_round_trips_with_blanks_for_missing_numbers(tmp_path) -> None:
    import csv

    path = tmp_path / "universe.csv"
    row = ["AAPL", "NASDAQ:AAPL", "Apple", "NASDAQ", "Stock", "✓", "Tech", "Hardware",
           3.0e12, 250.5, None, 200.0, 0.2525]

    build_universe.write_universe(path, [row])

    with path.open(encoding="utf-8") as handle:
        written = list(csv.reader(handle))

    assert written[0] == list(sheet.SHEET_COLUMNS)
    assert written[1][0] == "AAPL"
    # Blank, not zero: the sheet filters this column numerically.
    assert written[1][COL["pe"]] == ""
    assert written[1][COL["market_cap"]] == "3000000000000"
