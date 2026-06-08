"""
aggregate.py
Reads daily JSON snapshots from scraper.py and produces:
  data/history_ledger.json   per-day final metrics per property
  data/aggregates.json       weekly + monthly rollups

KEY FIXES vs prior version:
1. Date-cutoff filtering: each property's booking engine closes at a specific
   time. Snapshots taken AFTER that cutoff belong to the NEXT day and must be
   excluded from the current day's calculation.
     Sea Air Inn  : closes 10:00 PM PT = 05:00 UTC next calendar day
     Blufftop Inn : closes 10:00 PM PT = 05:00 UTC next calendar day
     Tarzana Inn  : closes 12:00 AM PT = 07:00 UTC next calendar day

2. UTC-to-PT date mapping: scraper runs on GitHub Actions in UTC. Runs after
   5:00 PM PT are saved under the NEXT UTC calendar date. aggregate.py scans
   all JSON files and assigns each snapshot to the correct PT date.

3. ADR: simple average of rates in the LAST VALID snapshot for the day.
   (weighted pickup ADR is unreliable with only 3-5 scans/day and sparse
    per-type pickup counts — simple avg of end-of-day offered rates is more
    stable and representative.)
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR    = Path("data")
LEDGER_FILE = DATA_DIR / "history_ledger.json"
AGG_FILE    = DATA_DIR / "aggregates.json"

SKIP_FILES = {"aggregates.json", "history_ledger.json", "config.json"}

# Property config: total rooms + UTC hour of booking cutoff on NEXT calendar day
# (i.e. last valid snapshot must have scraped_at < next_day 0:00 + cutoff_hours UTC)
PROPERTIES = {
    "Tarzana Inn":  {"capacity": 49, "cutoff_utc_hour": 7},   # midnight PT = 07:00 UTC
    "Sea Air Inn":  {"capacity": 24, "cutoff_utc_hour": 5},   # 10 PM PT   = 05:00 UTC
    "Blufftop Inn": {"capacity": 32, "cutoff_utc_hour": 5},   # 10 PM PT   = 05:00 UTC
}

PT_OFFSET = timedelta(hours=7)   # PDT = UTC-7 (use 8 in winter PST)

def utc_to_pt_date(utc_dt: datetime):
    """Return the Pacific Time calendar date for a UTC datetime."""
    return (utc_dt - PT_OFFSET).date()

def cutoff_utc(file_date, cutoff_hour: int) -> datetime:
    """Return the UTC datetime of the booking cutoff for a given PT date."""
    # file_date is a PT date; cutoff falls on next UTC calendar day at cutoff_hour
    # e.g. PT date June 6, cutoff_hour=5 -> June 7 05:00 UTC
    next_day = file_date + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, cutoff_hour, 0, 0)

# ── Load all raw daily snapshot files and remap to PT dates ──────────────────
def load_by_pt_date() -> dict:
    """
    Returns dict[pt_date_str][prop_name] = list of snapshots,
    where each snapshot is assigned to the PT date it belongs to
    (which may differ from the UTC-based filename).
    """
    by_pt: dict = defaultdict(lambda: defaultdict(list))

    for f in sorted(DATA_DIR.glob("*.json")):
        if f.name in SKIP_FILES:
            continue
        try:
            raw = json.load(open(f))
        except Exception as e:
            print(f"[aggregate] Skipping {f.name}: {e}")
            continue

        for prop, entries in raw.items():
            cfg = PROPERTIES.get(prop)
            if not cfg:
                continue
            for entry in entries:
                try:
                    utc_dt = datetime.fromisoformat(entry["scraped_at"].replace("Z", ""))
                except Exception:
                    continue
                pt_date = utc_to_pt_date(utc_dt)
                by_pt[str(pt_date)][prop].append(entry)

    return by_pt

# ── Compute final metrics for one PT date + property ─────────────────────────
def process_property_day(pt_date_str: str, prop: str, entries: list, capacity: int, cutoff_hour: int) -> dict | None:
    from datetime import date as date_cls
    pt_date   = datetime.strptime(pt_date_str, "%Y-%m-%d").date()
    cutoff_dt = cutoff_utc(pt_date, cutoff_hour)

    # Keep only snapshots before the booking cutoff
    valid = []
    for e in entries:
        try:
            utc_dt = datetime.fromisoformat(e["scraped_at"].replace("Z", ""))
            if utc_dt < cutoff_dt:
                valid.append((utc_dt, e))
        except Exception:
            continue

    if not valid:
        return None

    # Sort ascending by time
    valid.sort(key=lambda x: x[0])
    last_utc, last_entry = valid[-1]

    summary = last_entry.get("summary")
    rooms   = last_entry.get("rooms", [])

    if not summary:
        return None

    remaining = summary.get("total_remaining", capacity)
    sold      = max(0, capacity - remaining)
    occ       = round(sold / capacity * 100, 1) if capacity else 0

    # ADR: simple average of rates in last valid snapshot
    rates = [r["rate"] for r in rooms if r.get("rate") and r["rate"] > 0]
    adr   = round(sum(rates) / len(rates), 2) if rates else 0.0
    revpar = round(occ / 100 * adr, 2)

    # Normalise rooms for dashboard
    norm_rooms = [{
        "room_type":  r.get("room_type", "Unknown"),
        "available":  (r.get("rooms_left") or 0) > 0,
        "rooms_left": r.get("rooms_left", 0),
        "rate":       r.get("rate"),
    } for r in rooms]

    return {
        "date":           pt_date_str,
        "occ":            occ,
        "adr":            adr,
        "rev":            revpar,
        "sold":           sold,
        "remaining":      remaining,
        "capacity":       capacity,
        "rooms":          norm_rooms,
        "last_scan_utc":  last_utc.isoformat(),
        "valid_snapshots": len(valid),
    }

# ── Build weekly + monthly rollups ────────────────────────────────────────────
def build_aggregates(ledger: dict) -> dict:
    aggs = defaultdict(lambda: {"weeks": defaultdict(list), "months": defaultdict(list)})

    for date_str, props in ledger.items():
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        week_key  = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
        month_key = f"{dt.year}-{dt.month:02d}"
        for prop, m in props.items():
            aggs[prop]["weeks"][week_key].append(m)
            aggs[prop]["months"][month_key].append(m)

    def avg(items, key):
        vals = [i[key] for i in items if i.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0

    final = {}
    for prop, periods in aggs.items():
        final[prop] = {"weeks": {}, "months": {}}
        for k, days in periods["weeks"].items():
            final[prop]["weeks"][k] = {"occ": avg(days,"occ"), "adr": avg(days,"adr"), "rev": avg(days,"rev"), "days": len(days)}
        for k, days in periods["months"].items():
            final[prop]["months"][k] = {"occ": avg(days,"occ"), "adr": avg(days,"adr"), "rev": avg(days,"rev"), "days": len(days)}
    return final

# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    DATA_DIR.mkdir(exist_ok=True)
    print("[aggregate] Loading and remapping snapshots to PT dates...")

    by_pt = load_by_pt_date()
    if not by_pt:
        print("[aggregate] No data files found.")
        return

    print(f"[aggregate] Found PT dates: {sorted(by_pt.keys())}")

    ledger = {}
    for pt_date_str in sorted(by_pt.keys()):
        day_result = {}
        for prop, entries in by_pt[pt_date_str].items():
            cfg = PROPERTIES.get(prop)
            if not cfg:
                continue
            result = process_property_day(
                pt_date_str, prop, entries,
                cfg["capacity"], cfg["cutoff_utc_hour"]
            )
            if result:
                day_result[prop] = result
                print(f"  {pt_date_str} [{prop}] occ={result['occ']}%  ADR=${result['adr']}  remaining={result['remaining']}  scans={result['valid_snapshots']}")
        if day_result:
            ledger[pt_date_str] = day_result

    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)
    print(f"\n[aggregate] Written → {LEDGER_FILE}  ({len(ledger)} days)")

    aggs = build_aggregates(ledger)
    with open(AGG_FILE, "w") as f:
        json.dump(aggs, f, indent=2)
    print(f"[aggregate] Written → {AGG_FILE}  ({len(aggs)} properties)")

    print("\n--- Weekly Rollups ---")
    for prop in aggs:
        print(f"\n  {prop}")
        for wk, v in sorted(aggs[prop]["weeks"].items()):
            print(f"    {wk}: occ={v['occ']}%  ADR=${v['adr']}  RevPAR=${v['rev']}  ({v['days']}d)")

if __name__ == "__main__":
    run()
