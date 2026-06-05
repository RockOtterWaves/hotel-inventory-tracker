import json
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "aggregates.json"

# ─────────────────────────────────────────────
# LOAD ALL DAILY FILES
# ─────────────────────────────────────────────
def load_all_records():
    records = []

    for f in DATA_DIR.glob("*.json"):
        if f.name == "aggregates.json":
            continue

        date_str = f.stem
        try:
            data = json.load(open(f))
        except:
            continue

        for prop, entries in data.items():
            for e in entries:
                s = e.get("summary", {})
                records.append({
                    "date": date_str,
                    "property": prop,
                    "adr": s.get("blended_adr"),
                    "occ": s.get("estimated_occupancy_pct"),
                    "remaining": s.get("total_remaining"),
                    "sold": s.get("estimated_sold")
                })

    return records


# ─────────────────────────────────────────────
# GENERIC AGGREGATOR
# ─────────────────────────────────────────────
def aggregate(records, days):
    cutoff = datetime.utcnow() - timedelta(days=days)

    buckets = {}

    for r in records:
        try:
            d = datetime.fromisoformat(r["date"])
        except:
            continue

        if d < cutoff:
            continue

        key = r["property"]

        if key not in buckets:
            buckets[key] = {
                "adr": [],
                "occ": [],
                "remaining": [],
                "sold": []
            }

        if r["adr"]:
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
            "avg_adr": round(sum(vals["adr"]) / len(vals["adr"]), 2) if vals["adr"] else None,
            "avg_occ": round(sum(vals["occ"]) / len(vals["occ"]), 1) if vals["occ"] else None,
            "avg_remaining": round(sum(vals["remaining"]) / len(vals["remaining"]), 1) if vals["remaining"] else None,
            "avg_sold": round(sum(vals["sold"]) / len(vals["sold"]), 1) if vals["sold"] else None
        }

    return out


# ─────────────────────────────────────────────
# SAVE OUTPUT
# ─────────────────────────────────────────────
def run():
    records = load_all_records()

    weekly = aggregate(records, 7)
    monthly = aggregate(records, 30)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "weekly": weekly,
        "monthly": monthly
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print("✅ aggregates.json updated")


if __name__ == "__main__":
    run()
