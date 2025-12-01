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

API_KEY = "3JKDVI62T6OZUPAO"

CACHE_FILE = "cache_av.json"
TICKERS_FILE = "clean_tickers.txt"

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
    print(BLUE + f"📄 Loading tickers from {TICKERS_FILE}..." + RESET, flush=True)
    
    if not os.path.exists(TICKERS_FILE):
        print(RED + f"❌ ERROR: File not found: {TICKERS_FILE}" + RESET)
        print(RED + "   Please run update_tickers.py first to generate clean_tickers.txt" + RESET, flush=True)
        return []

    with open(TICKERS_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]



# ----------------------------
# Fetch one ticker
# ----------------------------

def fetch_one(symbol):
    print(f"{BLUE}▶️ Fetching: {symbol}{RESET}", flush=True)
    url = ALPHA_URL.format(symbol, API_KEY)
    r = requests.get(url)
    data = r.json()

    # Check for API rate limit errors (Alpha Vantage returns these keys)
    if "Note" in data or "Information" in data or "Error Message" in data:
        error_msg = data.get("Note", data.get("Information", data.get("Error Message", "")))
        print(f"{RED}   ⛔ API LIMIT/ERROR: {error_msg}{RESET}")
        return "LIMIT_REACHED"
    
    # Check if we got valid stock data
    if "Symbol" not in data:
        # Debug: show what keys we actually got
        print(f"{RED}   ❌ No data found for: {symbol}{RESET}")
        print(f"{YELLOW}   DEBUG: Response keys: {list(data.keys())}{RESET}")
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
    # Load existing cache
    cache = load_cache()
    
    # Load all tickers
    tickers = load_clean_tickers()
    print(BLUE + f"🔢 Total tickers: {len(tickers)}" + RESET, flush=True)
    print(BLUE + f"🔢 Already cached: {len(cache)}" + RESET, flush=True)

    # Filter out tickers that are already cached
    tickers_to_fetch = [t for t in tickers if t not in cache]
    print(BLUE + f"🔢 Need to fetch: {len(tickers_to_fetch)}" + RESET, flush=True)

    calls_used = 0
    calls_daily_limit = 25

    for sym in tickers_to_fetch:
        print(YELLOW + f"\n=== API CALL #{calls_used+1} — {sym} ===" + RESET, flush=True)

        result = fetch_one(sym)

        # Check if we hit the rate limit
        if result == "LIMIT_REACHED":
            print(RED + f"\n⛔ REACHED API LIMIT after {calls_used} successful calls — stopping." + RESET, flush=True)
            break
        
        # Only save and increment if we got valid data
        if result and isinstance(result, dict):
            cache[sym] = result
            calls_used += 1
            
            # Save cache after each successful fetch (in case of interruption)
            if calls_used % 5 == 0:  # Save every 5 calls
                save_cache(cache)
                print(BLUE + f"   💾 Progress saved ({calls_used} calls)" + RESET, flush=True)

        if calls_used >= calls_daily_limit:
            print(RED + "\n⛔ REACHED DAILY API LIMIT — stopping." + RESET, flush=True)
            break

        # Wait between calls (only if not the last one)
        if calls_used < calls_daily_limit and calls_used < len(tickers_to_fetch):
            print(BLUE + "⏳ Waiting 12 seconds..." + RESET, flush=True)
            time.sleep(10)

    print(GREEN + f"\n🎉 Done. New API calls made: {calls_used}" + RESET, flush=True)
    
    # Save the updated cache
    save_cache(cache)



if __name__ == "__main__":
    main()
