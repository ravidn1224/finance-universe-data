import requests
import pandas as pd

NASDAQ_LIST = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
NYSE_LIST   = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

OUTPUT = "clean_tickers.txt"

def download_tickers(url):
    text = requests.get(url, timeout=10).text
    rows = [line.split("|") for line in text.split("\n") if line.strip()]
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df.columns = [c.strip() for c in df.columns]

    # NASDAQ uses "Symbol", NYSE uses "ACT Symbol"
    for col in ["Symbol", "ACT Symbol"]:
        if col in df.columns:
            return df[col].dropna().tolist()

    raise ValueError("Symbol column not found")

def clean_symbol(sym):
    bad = ["$", "/", "^", ".", "-"]
    if any(b in sym for b in bad):
        return False
    if sym.endswith(("U", "W", "R")) and len(sym) > 3:
        return False
    return True

def main():
    nasdaq = download_tickers(NASDAQ_LIST)
    nyse   = download_tickers(NYSE_LIST)

    all_symbols = sorted(set(nasdaq + nyse))
    cleaned = [s.strip().upper() for s in all_symbols if clean_symbol(s)]

    with open(OUTPUT, "w") as f:
        for sym in cleaned:
            f.write(sym + "\n")

    print(f"Updated clean_tickers.txt with {len(cleaned)} tickers.")

if __name__ == "__main__":
    main()
