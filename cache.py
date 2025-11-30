import requests
import pandas as pd
import json
import os
import time
from datetime import datetime

# 🎨 צבעים
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
RESET  = "\033[0m"

API_KEY = "VI5JL4DUZA37DRTW"

CACHE_FILE = "cache_av.json"
CLEAN_TICKERS_FILE = "clean_tickers.txt"
CHUNK_FILE = os.environ.get("CHUNK_FILE", "clean_tickers.txt")


ALPHA_URL = "https://www.alphavantage.co/query?function=OVERVIEW&symbol={}&apikey={}"

# ----------------------------
# Load / Save Cache
# ----------------------------

def load_cache():
    if os.path.exists(CACHE_FILE) and os.path.getsize(CACHE_FILE) > 0:
        print(BLUE + f"📂 Loading existing cache..." + RESET, flush=True)
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    print(YELLOW + "⚠️ No cache found. Creating new cache..." + RESET, flush=True)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(GREEN + f"💾 Cache saved ({len(cache)} symbols)" + RESET, flush=True)


# ----------------------------
# Load Tickers
# ----------------------------

def load_clean_tickers():
    print(BLUE + f"📄 Loading tickers from {CHUNK_FILE}..." + RESET, flush=True)
    
    if not os.path.exists(CHUNK_FILE):
        print(RED + f"❌ ERROR: File not found: {CHUNK_FILE}" + RESET)
        print(RED + "   Did you forget to run split_tickers.py or commit tickers_X.txt ?" + RESET, flush=True)
        return []

    with open(CHUNK_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]



# ----------------------------
# Fetch one ticker
# ----------------------------

def fetch_one(symbol):
    print(f"{BLUE}▶️ Fetching: {symbol}{RESET}", flush=True)
    url = ALPHA_URL.format(symbol, API_KEY)
    r = requests.get(url)
    data = r.json()

    if "Symbol" not in data:
        print(f"{RED}   ❌ Skipped/no data/limit reached: {symbol}{RESET}")
        return None

    print(f"{GREEN}   ✔ Success: {symbol}{RESET}")
    return {
        "symbol": symbol,
        "name": data.get("Name", ""),
        "sector": data.get("Sector", ""),
        "industry": data.get("Industry", ""),
        "marketCap": data.get("MarketCapitalization", ""),
        "price": data.get("50DayMovingAverage", "")
    }


# ----------------------------
# MAIN
# ----------------------------

def main():
    tickers = load_clean_tickers()

    print(BLUE + f"🔢 Total tickers in this chunk: {len(tickers)}" + RESET, flush=True)

    partial_cache = {}  # <-- כל Chunk בונה cache חלקי משלו

    calls_used = 0
    calls_daily_limit = 25

    for sym in tickers:
        print(YELLOW + f"\n=== API CALL #{calls_used+1} — {sym} ===" + RESET, flush=True)

        result = fetch_one(sym)

        if result:
            partial_cache[sym] = result
            calls_used += 1

        if calls_used >= calls_daily_limit:
            print(RED + "\n⛔ REACHED DAILY API LIMIT — stopping." + RESET, flush=True)
            break

        print(BLUE + "⏳ Waiting 12 seconds..." + RESET, flush=True)
        time.sleep(12)

    print(GREEN + f"\n🎉 Done. API calls in this chunk: {calls_used}" + RESET, flush=True)

    partial_file = f"cache_part_{os.environ.get('CHUNK_ID','0')}.json"
    with open(partial_file, "w", encoding="utf-8") as f:
        json.dump(partial_cache, f, indent=2)
        print(f"{GREEN}💾 Saved partial: {partial_file}{RESET}", flush=True)



if __name__ == "__main__":
    main()
