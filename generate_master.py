import json
import pandas as pd
from datetime import datetime

CACHE_FILE = "cache_av.json"
TICKERS_FILE = "clean_tickers.txt"
OUTPUT_FILE = "master_stocks.csv"

def load_cache():
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_tickers():
    with open(TICKERS_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

def fix_symbol(sym):
    # Handle weird tickers like BRK.B -> BRK-B
    if "." in sym:
        sym = sym.replace(".", "-")
    return sym.upper()

def main():
    print("Loading cache...")
    cache = load_cache()

    print("Loading ticker list...")
    tickers = load_tickers()

    rows = []

    for raw in tickers:
        sym = fix_symbol(raw)

        if sym in cache:
            rows.append(cache[sym])
        else:
            rows.append({
                "symbol": sym,
                "name": "",
                "sector": "",
                "industry": "",
                "marketCap": "",
                "price": ""
            })

    df = pd.DataFrame(rows)
    df["last_updated"] = datetime.utcnow().isoformat()

    df.to_csv(OUTPUT_FILE, index=False)
    print("MASTER created:", OUTPUT_FILE)

if __name__ == "__main__":
    main()
