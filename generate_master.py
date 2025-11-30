import time
import yfinance as yf
import pandas as pd
from datetime import datetime

TICKERS_FILE = "tickers.txt"
OUTPUT_CSV = "master_stocks.csv"

def load_tickers(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f.readlines()]
    return [t for t in lines if t and not t.startswith("#")]

def fetch_one_ticker(ticker: str) -> dict:
    try:
        t = yf.Ticker(ticker)
        info = t.info  # yfinance מחזיר dict גדול

        return {
            "symbol": ticker,
            "name": info.get("shortName") or info.get("longName") or "",
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "exchange": info.get("exchange", ""),
            "marketCap": info.get("marketCap", ""),
            "beta": info.get("beta", ""),
            "trailingPE": info.get("trailingPE", ""),
            "forwardPE": info.get("forwardPE", ""),
            "priceToBook": info.get("priceToBook", ""),
            "dividendYield": info.get("dividendYield", ""),
            "currency": info.get("currency", ""),
        }
    except Exception as e:
        print(f"[ERROR] {ticker}: {e}")
        return {
            "symbol": ticker,
            "name": "",
            "sector": "",
            "industry": "",
            "exchange": "",
            "marketCap": "",
            "beta": "",
            "trailingPE": "",
            "forwardPE": "",
            "priceToBook": "",
            "dividendYield": "",
            "currency": "",
        }

def main():
    tickers = load_tickers(TICKERS_FILE)
    print(f"Loaded {len(tickers)} tickers")

    rows = []
    for i, ticker in enumerate(tickers, start=1):
        print(f"[{i}/{len(tickers)}] Fetching {ticker}...")
        row = fetch_one_ticker(ticker)
        rows.append(row)
        time.sleep(0.2)  # לא להעמיס על yfinance / Yahoo

    df = pd.DataFrame(rows)

    # מוסיף timestamp כדי שתדע מתי זה נבנה
    df["lastUpdated"] = datetime.utcnow().isoformat() + "Z"

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {OUTPUT_CSV} with {len(df)} rows.")

if __name__ == "__main__":
    main()
