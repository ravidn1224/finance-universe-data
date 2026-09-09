# finance-universe-data

A small data pipeline that builds `master_stocks.csv`: one row per ticker in a
curated US-equity universe, with company name, sector, industry, market cap and
50-day moving average, sourced from the
[Alpha Vantage](https://www.alphavantage.co/) `OVERVIEW` endpoint.

Two sources are used, matched to how fast each field moves:

- **Alpha Vantage** supplies the descriptive fields (`name`, `sector`,
  `industry`) plus shares outstanding. Its free tier allows only **25 requests
  per day**, one per symbol, so the universe cycles through it roughly every 37
  days — fine for data that rarely changes.
- **Yahoo Finance** supplies `price` and `marketCap` for the **whole universe
  every day**, through a bulk endpoint that covers ~915 tickers in about five
  requests.

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
| `clean_tickers.txt` | The ticker universe (curated; see the warning below) |
| `cache_av.json` | Accumulated overview cache — the expensive asset |
| `master_stocks.csv` | Published output |

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
python -m pytest                   # run the test suite
```

`cache.py` takes `--max-calls N`, `--refresh-after-days N`, `--fill-only` and
`--retry-missing`. `update_quotes.py` takes `--dry-run`, `--limit N` and
`--batch-size N`.

## Automation

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `update_master.yml` | Daily 03:00 UTC | Refreshes fundamentals and all prices, rebuilds the CSV, commits only if data changed |
| `ci.yml` | Push / PR | Runs the tests on Python 3.11 and 3.12 |

Add `ALPHAVANTAGE_API_KEY` as a repository secret.

## Configuration

All settings are environment variables: `ALPHAVANTAGE_API_KEY`, `MAX_CALLS`,
`SLEEP_SECONDS`, `HTTP_TIMEOUT`, `MAX_RETRIES`, `REFRESH_AFTER_DAYS`,
`FILL_ONLY`, `RETRY_MISSING`, plus path overrides `CACHE_FILE`, `TICKERS_FILE`
and `MASTER_FILE`.

## Data notes

`master_stocks.csv` columns are `symbol,name,sector,industry,marketCap,price,
last_updated`. `price` is the **50-day moving average**, not a live quote —
that was Alpha Vantage's `50DayMovingAverage`, and the Yahoo refresh computes
the same statistic so the column keeps its meaning.

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

Roughly 18 of the 915 tickers no longer resolve at either source — companies
such as Kellanova and Comerica that have since been acquired. They keep their
last known values; pruning them from `clean_tickers.txt` would be a reasonable
cleanup.

> **Warning:** `clean_tickers.txt` is a curated ~915-symbol list. Running
> `update_tickers.py` replaces it with the full ~11,500 symbol universe from the
> exchange listings, which at 25 calls per day would take over a year to fill.
> Run it only if you intend to widen the universe.
