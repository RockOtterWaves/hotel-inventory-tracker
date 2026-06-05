"""
aggregate.py — Reads daily JSON snapshots from scraper.py and produces:
  data/history_ledger.json   Per-day final metrics per property
  data/aggregates.json       Weekly + monthly rollups (occ%, ADR, RevPAR)

Compatible with scraper.py room format:
  {"room_type": str, "rate": float, "rooms_left": int}
  summary keys: total_rooms_property, total_remaining, estimated_sold,
                estimated_occupancy_pct, blended_adr
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR    = Path("data")
LEDGER_FILE = DATA_DIR / "history_ledger.json"
AGG_FILE    = DATA_DIR / "aggregates.json"

# Must match PROPERTIES in scraper.py
CAPACITIES = {
    "Tarzana Inn":  49,
    "Sea Air Inn":  24,
    "Blufftop Inn": 32,
}

SKIP_FILES = {"aggregates.json", "history_ledger.json", "config.json"}

# ── Load all raw daily snapshot files ─────────────────────────────────────────
def load_daily_files() -> dict:
    daily = {}
    for f in sorted(DATA_DIR.glob("*.json")):
        if f.name in SKIP_FILES:
            continue
        try:
            with open(f) as fh:
                daily[f.stem] = json.load(fh)
        except Exception as e:
            print(f"[aggregate] Skipping {f.name}: {e}")
    return daily

# ── Compute final-day metrics from one day's snapshots ────────────────────────
def process_day(date_str: str, data: dict) -> dict:
    result = {}
    for prop, capacity in CAPACITIES.items():
        entries = data.get(prop, [])
        if not entries:
            continue

        # Sort ascending by scraped_at to find the most recent state
        entries.sort(key=lambda x: x.get("scraped_at", ""))

        summary = next((e.get("summary") for e in reversed(entries) if e.get("summary")), None)
        rooms   = next((e.get("rooms") for e in reversed(entries) if e.get("rooms")), [])

        if not summary:
            continue

        remaining = summary.get("total_remaining", capacity)
        adr       = summary.get("blended_adr") or 0
        sold      = max(0, capacity - remaining)
        
        # Prevent division by zero
        occ       = round(sold / capacity * 100, 1) if capacity > 0 else 0
        revpar    = round(occ / 100 * float(adr), 2)

        # Normalize rooms for dashboard compatibility
        normalised_rooms = []
        for r in rooms:
            normalised_rooms.append({
                "room_type":  r.get("room_type", "Unknown"),
                "available":  (r.get("rooms_left", 0) or 0) > 0,
                "rooms_left": r.get("rooms_left", 0),
                "rate":       r.get("rate"),
            })

        result[prop] = {
            "date":      date_str,
            "occ":       occ,
            "adr":       round(float(adr), 2),
            "rev":       revpar,
            "sold":      sold,
            "remaining": remaining,
            "capacity":  capacity,
            "rooms":     normalised_rooms,
        }
    return result

# ── Build weekly + monthly rollups ────────────────────────────────────────────
def build_aggregates(ledger: dict) -> dict:
    aggs = defaultdict(lambda: {
        "weeks":  defaultdict(list),
        "months": defaultdict(list),
    })

    for date_str, props in ledger.items():
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        week_key  = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
        month_key = f"{dt.year}-{dt.month:02d}"
        for prop, metrics in props.items():
            aggs[prop]["weeks"][week_key].append(metrics)
            aggs[prop]["months"][month_key].append(metrics)

    def avg(items, key):
        vals = [i[key] for i in items if i.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0

    final = {}
    for prop, periods in aggs.items():
        final[prop] = {"weeks": {}, "months": {}}
        for k, days in periods["weeks"].items():
            final[prop]["weeks"][k] = {
                "occ": avg(days, "occ"), "adr": avg(days, "adr"),
                "rev": avg(days, "rev"), "days": len(days),
            }
        for k, days in periods["months"].items():
            final[prop]["months"][k] = {
                "occ": avg(days, "occ"), "adr": avg(days, "adr"),
                "rev": avg(days, "rev"), "days": len(days),
            }
    return final

# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    DATA_DIR.mkdir(exist_ok=True)
    daily = load_daily_files()

    if not daily:
        return

    ledger = {d: process_day(d, s) for d, s in daily.items()}
    ledger = {d: v for d, v in ledger.items() if v} 

    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)

    aggs = build_aggregates(ledger)
    with open(AGG_FILE, "w") as f:
        json.dump(aggs, f, indent=2)

if __name__ == "__main__":
    run()
