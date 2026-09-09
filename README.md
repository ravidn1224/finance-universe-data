# finance-universe-data

A small data pipeline that builds `master_stocks.csv`: one row per ticker in a
curated US-equity universe, with company name, sector, industry, market cap and
50-day moving average, sourced from the
[Alpha Vantage](https://www.alphavantage.co/) `OVERVIEW` endpoint.

The Alpha Vantage free tier allows **25 requests per day**, so the pipeline is
built around a durable cache. Each daily run spends that budget on whatever is
most valuable: first any symbols missing from the cache, then the entries that
have gone stalest. With ~915 tickers the universe cycles through a full refresh
roughly every 37 days.

## Layout

| Path | Purpose |
| --- | --- |
| `universe/` | Shared library: config, symbol rules, cache store, API client |
| `cache.py` | Spend the daily budget: fill gaps, then refresh stale entries |
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
python cache.py --dry-run     # show what the budget would be spent on, free
python cache.py               # spend today's 25 calls and update the cache
python generate_master.py     # rebuild the CSV from the cache
python -m pytest              # run the test suite
```

Useful flags: `--max-calls N` to change the budget, `--refresh-after-days N` to
change what counts as stale, `--fill-only` to never refresh, and
`--retry-missing` to re-query symbols previously recorded as having no data.

## Automation

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `update_master.yml` | Daily 03:00 UTC | Spends 25 calls, rebuilds the CSV, commits only if data changed |
| `ci.yml` | Push / PR | Runs the tests on Python 3.11 and 3.12 |

Add `ALPHAVANTAGE_API_KEY` as a repository secret.

## Configuration

All settings are environment variables: `ALPHAVANTAGE_API_KEY`, `MAX_CALLS`,
`SLEEP_SECONDS`, `HTTP_TIMEOUT`, `MAX_RETRIES`, `REFRESH_AFTER_DAYS`,
`FILL_ONLY`, `RETRY_MISSING`, plus path overrides `CACHE_FILE`, `TICKERS_FILE`
and `MASTER_FILE`.

## Data notes

`master_stocks.csv` columns are `symbol,name,sector,industry,marketCap,price,
last_updated`. `price` is the 50-day moving average reported by `OVERVIEW`, not
a live quote.

`last_updated` is **per row**: it records when that symbol's data was fetched,
not when the file was built. A build that changes no data therefore produces an
identical file and no commit. Rows cached before timestamps were tracked show a
blank `last_updated` until their first refresh.

Cache entries carry `status` (`ok` or `not_found`) and `fetched_at` for
bookkeeping; those fields are deliberately kept out of the published CSV.

> **Warning:** `clean_tickers.txt` is a curated ~915-symbol list. Running
> `update_tickers.py` replaces it with the full ~11,500 symbol universe from the
> exchange listings, which at 25 calls per day would take over a year to fill.
> Run it only if you intend to widen the universe.
