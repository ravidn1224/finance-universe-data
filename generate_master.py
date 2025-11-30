import json
import pandas as pd
from datetime import datetime
import os

# 🎨 ANSI Colors
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
RESET  = "\033[0m"

CACHE_FILE = "cache_av.json"
TICKERS_FILE = "clean_tickers.txt"
OUTPUT_FILE = "master_stocks.csv"


# ---------------------------------
# Utility functions
# ---------------------------------

def load_cache():
    if os.path.exists(CACHE_FILE) and os.path.getsize(CACHE_FILE) > 0:
        print(BLUE + f"📂 Loading cache file: {CACHE_FILE}" + RESET)
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    print(RED + "⚠️ cache_av.json missing or empty!" + RESET)
    return {}


def load_tickers():
    print(BLUE + f"📄 Loading tickers from {TICKERS_FILE} ..." + RESET)
    with open(TICKERS_FILE, "r") as f:
        tickers = [line.strip() for line in f if line.strip()]
    print(BLUE + f"🔢 Total tickers loaded: {len(tickers)}" + RESET)
    return tickers


def fix_symbol(sym):
    # Handle BRK.B → BRK-B, MKC.V → MKC-V
    if "." in sym:
        return sym.replace(".", "-").upper()
    return sym.upper()


# ⭐ Text formatting (Title Case)
def clean_text(text):
    if not text:
        return ""
    return text.title()


# ---------------------------------
# Main Builder
# ---------------------------------

def main():
    print(BLUE + "\n=== Building MASTER CSV ===" + RESET)

    cache = load_cache()
    tickers = load_tickers()

    rows = []

    for original in tickers:
        sym = fix_symbol(original)

        if sym in cache:
            print(GREEN + f"✔ Using cached: {sym}" + RESET)
            rows.append(cache[sym])
        else:
            print(YELLOW + f"⚠️ Missing in cache: {sym} (added blank row)" + RESET)
            rows.append({
                "symbol": sym,
                "name": "",
                "sector": "",
                "industry": "",
                "marketCap": "",
                "price": ""
            })

    print(BLUE + "\n🧱 Converting rows to DataFrame..." + RESET)
    df = pd.DataFrame(rows)

    # ⭐ Apply Title Case to sector & industry
    df["sector"] = df["sector"].astype(str).apply(clean_text)
    df["industry"] = df["industry"].astype(str).apply(clean_text)

    df["last_updated"] = datetime.utcnow().isoformat()

    print(GREEN + f"💾 Saving MASTER file: {OUTPUT_FILE}" + RESET)
    df.to_csv(OUTPUT_FILE, index=False)

    print(GREEN + "\n🎉 MASTER build completed successfully!" + RESET)


if __name__ == "__main__":
    main()
