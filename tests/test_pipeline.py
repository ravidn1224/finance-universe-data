"""Tests for symbol handling, cache persistence and the fetch/refresh loop."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import cache as cache_cli
from universe import config, liquidity, quotes, store, symbols
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
