import json
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "aggregates.json"

def load_all_records():
    records = []
    if not DATA_DIR.exists():
        return records

    for f in DATA_DIR.glob("*.json"):
        if f.name == "aggregates.json":
            continue

        date_str = f.stem  # Extract string components matching YYYY-MM-DD
        try:
            with open(f, "r") as open_file:
                data = json.load(open_file)
        except:
            continue

        for prop, entries in data.items():
            for e in entries:
                s = e.get("summary", {})
                if not s:
                    continue
                records.append({
                    "date": date_str,
                    "property": prop,
                    "adr": s.get("blended_adr"),
                    "occ": s.get("estimated_occupancy_pct"),
                    "remaining": s.get("total_remaining"),
                    "sold": s.get("estimated_sold")
                })
    return records

def aggregate(records, days):
    cutoff = datetime.utcnow() - timedelta(days=days)
    buckets = {}

    for r in records:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
        except:
            continue

        if d < cutoff:
            continue

        key = r["property"]
        if key not in buckets:
            buckets[key] = {"adr": [], "occ": [], "remaining": [], "sold": []}

        if r["adr"] and r["adr"] > 0:
            buckets[key]["adr"].append(r["adr"])
        if r["occ"] is not None:
            buckets[key]["occ"].append(r["occ"])
        if r["remaining"] is not None:
            buckets[key]["remaining"].append(r["remaining"])
        if r["sold"] is not None:
            buckets[key]["sold"].append(r["sold"])

    out = {}
    for prop, vals in buckets.items():
        out[prop] = {
            "avg_adr": round(sum(vals["adr"]) / len(vals["adr"]), 2) if vals["adr"] else 0.0,
            "avg_occ": round(sum(vals["occ"]) / len(vals["occ"]), 1) if vals["occ"] else 0.0,
            "avg_remaining": round(sum(vals["remaining"]) / len(vals["remaining"]), 1) if vals["remaining"] else 0.0,
            "avg_sold": round(sum(vals["sold"]) / len(vals["sold"]), 1) if vals["sold"] else 0.0
        }
    return out

def run():
    print("Beginning trend summaries calculation run...")
    records = load_all_records()

    weekly = aggregate(records, 7)
    monthly = aggregate(records, 30)

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "weekly": weekly,
        "monthly": monthly
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print("✅ data/aggregates.json written successfully.")

if __name__ == "__main__":
    run()
