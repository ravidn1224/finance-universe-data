import json, glob

parts = glob.glob("partials/cache_part_*.json")
merged = {}

for part in parts:
    with open(part, "r") as f:
        merged.update(json.load(f))

with open("cache_av.json", "w") as f:
    json.dump(merged, f, indent=2)

print("Merged", len(parts), "parts")
print("Total tickers:", len(merged))
