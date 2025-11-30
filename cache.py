import requests
import pandas as pd
import json
import os
import time
from datetime import datetime

API_KEY = "2GW4W6Y9R8Q8ILEY"

CACHE_FILE = "cache_av.json"
CLEAN_TICKERS_FILE = "clean_tickers.txt"

ALPHA_URL = "https://www.alphavantage.co/query?function=OVERVIEW&symbol={}&apikey={}"

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
        print("Cache saved:", CACHE_FILE)

def load_clean_tickers():
    with open(CLEAN_TICKERS_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

def fetch_one(symbol):
    url = ALPHA_URL.format(symbol, API_KEY)
    r = requests.get(url)
    data = r.json()

    # Alpha Vantage returns an error object when limit reached or missing data
    if "Symbol" not in data:
        print(f"❌ Skipped (no data / limit reached): {symbol}")
        return None

    return {
        "symbol": symbol,
        "name": data.get("Name", ""),
        "sector": data.get("Sector", ""),
        "industry": data.get("Industry", ""),
        "marketCap": data.get("MarketCapitalization", ""),
        "price": data.get("50DayMovingAverage", "")
    }

def main():
    print("Loading existing cache...")
    cache = load_cache()

    print("Loading tickers...")
    tickers = load_clean_tickers()
    print("Total tickers:", len(tickers))

    calls_left = 25    # Alpha Vantage Free Tier (25/day)
    new_calls = 0

    for sym in tickers:
        if sym in cache and cache[sym].get("sector", "") != "":
            continue  # Already in cache

        if new_calls >= calls_left:
            print("📌 Reached Alpha Vantage limit for today (25/day).")
            break

        print("Fetching:", sym)
        profile = fetch_one(sym)

        if profile:
            cache[sym] = profile
            new_calls += 1

        save_cache(cache)
        time.sleep(15)  # Required to avoid API lockout

    print("Finished. New API calls used:", new_calls)

if __name__ == "__main__":
    main()
