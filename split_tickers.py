CHUNKS = 40  # Number of parallel jobs

# Read clean tickers
with open("clean_tickers.txt") as f:
    tickers = [line.strip() for line in f if line.strip()]

total = len(tickers)
chunk_size = total // CHUNKS + 1

print(f"Splitting {total} tickers into {CHUNKS} chunks of ~{chunk_size} each.")

for i in range(CHUNKS):
    chunk = tickers[i * chunk_size:(i + 1) * chunk_size]
    filename = f"tickers_{i}.txt"
    with open(filename, "w") as f:
        for t in chunk:
            f.write(t + "\n")
    print(f"Created {filename} with {len(chunk)} tickers.")
