# finance-universe-data

A small data pipeline that builds `master_stocks.csv`: one row per ticker in a
curated US-equity universe, with company name, sector, industry, market cap and
50-day moving average, sourced from the
[Alpha Vantage](https://www.alphavantage.co/) `OVERVIEW` endpoint.

Two sources are used, matched to how fast each field moves:

- **Alpha Vantage** supplies the descriptive fields (`name`, `sector`,
  `industry`), the `peRatio`, plus shares outstanding. Its free tier allows only
  **25 requests per day**, one per symbol, so the universe cycles through it
  roughly every 37 days — fine for data that rarely changes.
- **Yahoo Finance** supplies `price`, `marketCap` and the 150-day average
  (`ma150`) for the **whole universe every day**, through a bulk endpoint that
  covers ~915 tickers in about five requests.

That split is the point: prices are genuinely daily, while the 25-call budget
is spent only on things that actually change slowly.

## Layout

| Path | Purpose |
| --- | --- |
| `universe/` | Shared library: config, symbol rules, cache store, API clients |
| `cache.py` | Alpha Vantage: fill missing fundamentals, refresh stale ones |
| `update_quotes.py` | Yahoo: refresh prices and market caps for everything |
| `generate_master.py` | Rebuild `master_stocks.csv` from the cache |
| `update_tickers.py` | Rebuild `clean_tickers.txt` from the NASDAQ Trader listings |
| `build_universe.py` | Rebuild `universe.csv`, the full directory the sheet reads |
| `clean_tickers.txt` | The ticker universe (curated; see the warning below) |
| `cache_av.json` | Accumulated overview cache — the expensive asset |
| `master_stocks.csv` | Published output: the curated universe, one row per ticker |
| `universe.csv` | Published output: every US listing, for the Google Sheet |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export ALPHAVANTAGE_API_KEY=your_key_here
```

The key is read from the environment only. Never commit it: a key in git
history is a leaked key even after it is deleted from the working tree.

## Usage

```bash
python cache.py --dry-run          # show what the budget would buy, free
python cache.py                    # spend today's 25 Alpha Vantage calls
python update_quotes.py            # refresh prices for the whole universe
python generate_master.py          # rebuild the CSV from the cache
python update_tickers.py --dry-run # preview the rebuilt ticker universe
python build_universe.py           # rebuild the sheet's full listing directory
python -m pytest                   # run the test suite
```

`cache.py` takes `--max-calls N`, `--refresh-after-days N`, `--fill-only` and
`--retry-missing`. `update_quotes.py` takes `--dry-run`, `--limit N`,
`--batch-size N` and `--threads N`. `update_tickers.py` takes `--limit N`,
`--quick`, `--no-shortlist`, `--force` and the same batching flags.

## Automation

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `update_master.yml` | Daily 03:00 UTC | Refreshes fundamentals and all prices, rebuilds both CSVs, commits only if data changed |
| `update_tickers.yml` | Weekly, Sunday 06:00 UTC | Re-ranks the universe by liquidity so the list keeps itself current |
| `ci.yml` | Push / PR | Runs the tests on Python 3.11 and 3.12 |

Add `ALPHAVANTAGE_API_KEY` as a repository secret.

## Configuration

All settings are environment variables: `ALPHAVANTAGE_API_KEY`, `MAX_CALLS`,
`SLEEP_SECONDS`, `HTTP_TIMEOUT`, `MAX_RETRIES`, `REFRESH_AFTER_DAYS`,
`FILL_ONLY`, `RETRY_MISSING`, plus path overrides `CACHE_FILE`, `TICKERS_FILE`,
`MASTER_FILE` and `UNIVERSE_FILE`.

## Data notes

`master_stocks.csv` columns are `symbol,name,sector,industry,marketCap,price,
peRatio,ma150,last_updated`. `price` is the **50-day moving average**, not a
live quote — that was Alpha Vantage's `50DayMovingAverage`, and the Yahoo
refresh computes the same statistic so the column keeps its meaning.

`peRatio` is Alpha Vantage's `PERatio`, captured on the same slow fundamentals
cycle; its `"None"` placeholder for companies without earnings is published as
blank. `ma150` is the **150-day moving average**, computed by the daily Yahoo
refresh from a year of history and left blank for symbols with less history
than the window. New fields are appended before `last_updated` so the column
order consumers depend on never shifts.

`marketCap` is shares outstanding times the latest close. Shares outstanding
comes from Alpha Vantage and is only refreshed on its slow cycle, which is fine
because share counts move only on buybacks and issuance. Until a symbol has
been through Alpha Vantage at least once under this version, its market cap is
left at the last known value rather than estimated.

`last_updated` is **per row**: it records when that symbol's data last moved,
not when the file was built. A build that changes no data therefore produces an
identical file and no commit.

Cache entries also carry `status`, `fetched_at`, `quoted_at` and
`sharesOutstanding` for bookkeeping; those are deliberately kept out of the
published CSV so its schema never shifts.

## `universe.csv` — the Google Sheet's directory

A second, much wider output: every NASDAQ, NYSE, NYSE American and NYSE Arca
listing (~11,200 rows, ~7,100 excluding ETFs), with columns `symbol,gf_ticker,
name,exchange,type,sp500,sector,industry,market_cap,price,pe,sma150,
pct_above_sma`.

It merges four sources, in this order of precedence:

- **NASDAQ Trader listing files** decide who exists, what exchange they are on
  and whether they are a fund. Test issues and symbols no quote source can
  address (preferred series such as `ABR$D`) are dropped.
- **S&P 500 constituents** set the `sp500` flag and win on sector and industry,
  because GICS is the stricter taxonomy.
- **The NASDAQ screener** fills sector, industry, market cap and last price for
  the rest of the market — one request covering ~7,000 stocks.
- **This pipeline's own cache** supplies `pe` and `sma150`, which exist nowhere
  else, and backfills the other fields where the screener is silent.

`pct_above_sma` is computed here rather than left to the sheet. As a live
spreadsheet formula it needed a year of history per row, which at this scale
never finished loading.

The reason this file exists at all is that the spreadsheet used to do this
merge itself. Doing so meant calling `api.nasdaq.com` from Google's servers,
which routinely stalls for those clients, and Apps Script cannot set a request
timeout — so the sheet's build would sit in a single fetch until it hit the
hard six-minute script limit and was killed, logging nothing. A GitHub runner
reaches the screener in about two seconds and has no deadline. The sheet now
downloads this finished file and writes it in one call.

## How the ticker universe is chosen

The exchange listings carry ~11,500 common stocks, far more than 25 Alpha
Vantage calls a day can supply. `update_tickers.py` keeps the most heavily
traded `--limit` of them (default 1000), so the list maintains itself: newly
active companies rise in, and dormant or delisted ones drop out. Symbols
already in the list survive until they fall well past the cut-off, so names
hovering at the boundary do not flip in and out week to week.

Ranking every symbol through Yahoo would mean one request each and take close
to half an hour, so it runs in two stages:

1. **Shortlist.** NASDAQ's screener returns the latest session's price and
   volume for every listed stock in a *single* request. One session is too
   noisy to rank on directly — it agrees with the monthly average on about 91%
   of a top-1000 — but it is easily accurate enough to decide who is in
   contention, so the top `2 × limit` go through.
2. **Rank.** Only that shortlist is measured against Yahoo's one-month average
   dollar volume, which decides the final list.

That takes about a minute instead of ~28. `--quick` stops after stage 1 (a
couple of seconds, noisier); `--no-shortlist` measures the full listing the
slow way. If the screener is unreachable or returns a suspiciously short
response, the run falls back to the full sweep rather than quietly dropping
symbols, and a result that comes back too small aborts instead of truncating
the list — use `--force` to override.

Because the screener only lists live securities, this also clears out symbols
that have been acquired or renamed, which a static list accumulates: `BK`
became `BNY` in the BNY Mellon rebrand, and `EA`, `MMC`, `AVB` and `EQR` no
longer trade at all.
