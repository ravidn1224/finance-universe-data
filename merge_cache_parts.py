import json
import glob

parts = glob.glob("cache_part_*.json")

merged = {}

for part in parts:
    with open(part) as f:
        data = json.load(f)
        merged.update(data)

with open("cache_av.json", "w") as f:
    json.dump(merged, f, indent=2)

print(f"Merged {len(parts)} parts → cache_av.json with {len(merged)} tickers")
