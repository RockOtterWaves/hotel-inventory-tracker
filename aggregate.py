import json
import re
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "aggregates.json"
HISTORY_LEDGER = DATA_DIR / "history_ledger.json"

# Portfolio Capacity Map
CAPACITIES = {
    "Tarzana Inn": 49,
    "Sea Air Inn": 24,
    "Blufftop Inn": 32
}

def load_raw_daily_files():
    """Groups raw tracking records by operational calendar date."""
    daily_snapshots = {}
    if not DATA_DIR.exists():
        return daily_snapshots

    for f in DATA_DIR.glob("*.json"):
        if f.name in ["aggregates.json", "history_ledger.json"]:
            continue
        
        date_str = f.stem  # YYYY-MM-DD
        try:
            with open(f, "r") as open_file:
                day_data = json.load(open_file)
            if day_data:
                daily_snapshots[date_str] = day_data
        except:
            continue
    return daily_snapshots

def process_final_daily_data(date_str, data_dict):
    """
    Computes precise interval pickup pricing. If zero pickup occurs,
    maps to the lowest active captured baseline rate of sold inventory.
    """
    day_compiled = {}
    
    for prop_name, capacity in CAPACITIES.items():
        entries = data_dict.get(prop_name, [])
        if not entries:
            continue
            
        # Sort files chronologically to measure interval step-downs
        entries.sort(key=lambda x: x.get("scraped_at", ""))
        
        last_valid_summary = None
        last_valid_rooms = []
        
        previous_rooms = {}
        total_pickup_revenue = 0.0
        total_rooms_picked_up = 0
        baseline_rates = []

        for e in entries:
            rooms_list = e.get("rooms", [])
            summary = e.get("summary", {})
            if not summary or not rooms_list:
                continue

            # Skip blank offline fields resulting from early office closures
            if summary.get("total_remaining", 0) == 0 and summary.get("blended_adr", 0) == 0:
                continue
                
            last_valid_summary = summary
            last_valid_rooms = rooms_list
            
            for r in rooms_list:
                rtype = r["room_type"]
                rate = r["rate"]
                left = r["rooms_left"]
                if rate > 0:
                    baseline_rates.append(rate)
                
                if rtype in previous_rooms:
                    prev_left = previous_rooms[rtype]
                    # Direct Interval Pickup Isolation
                    if left < prev_left:
                        picked_up = prev_left - left
                        total_pickup_revenue += (picked_up * rate)
                        total_rooms_picked_up += picked_up
                
                previous_rooms[rtype] = left

        if not last_valid_summary:
            continue

        final_remaining = last_valid_summary.get("total_remaining", capacity)
        final_sold = max(capacity - final_remaining, 0)
        final_occ = round((final_sold / capacity) * 100, 1) if capacity else 0.0

        # STRICT ADR CALCULATION:
        # 1. Use pure interval-step velocity revenue if pickup happened.
        # 2. If no pickup happened, use the lowest captured baseline rate (actual realization value).
        if total_rooms_picked_up > 0:
            estimated_adr = round(total_pickup_revenue / total_rooms_picked_up, 2)
        elif baseline_rates:
            estimated_adr = min(baseline_rates)
        else:
            estimated_adr = 0.0

        estimated_revpar = round((final_occ / 100.0) * estimated_adr, 2)

        day_compiled[prop_name] = {
            "date": date_str,
            "property": prop_name,
            "capacity": capacity,
            "rooms_remaining": final_remaining,
            "rooms_sold": final_sold,
            "occupancy_pct": final_occ,
            "estimated_adr": estimated_adr,
            "revpar": estimated_revpar,
            "rooms": last_valid_rooms  # Preserved for live room type tables
        }
        
    return day_compiled

def compile_periodic_averages(ledger_data):
    """Calculates running metrics for the summary cards."""
    output = {}
    for prop_name in CAPACITIES.keys():
        prop_history = [day[prop_name] for day in ledger_data.values() if prop_name in day]
        if not prop_history:
            continue
            
        prop_history.sort(key=lambda x: x["date"])
        
        # 7-Day Window
        w_slice = prop_history[-7:]
        w_occ = sum(d["occupancy_pct"] for d in w_slice) / len(w_slice) if w_slice else 0
        w_adr = sum(d["estimated_adr"] for d in w_slice) / len(w_slice) if w_slice else 0
        w_rev = sum(d["revpar"] for d in w_slice) / len(w_slice) if w_slice else 0
        
        # 30-Day Window
        m_slice = prop_history[-30:]
        m_occ = sum(d["occupancy_pct"] for d in m_slice) / len(m_slice) if m_slice else 0
        m_adr = sum(d["estimated_adr"] for d in m_slice) / len(m_slice) if m_slice else 0
        m_rev = sum(d["revpar"] for d in m_slice) / len(m_slice) if m_slice else 0
        
        output[prop_name] = {
            "weekly": {"avg_occ": round(w_occ, 1), "avg_adr": round(w_adr, 2), "avg_revpar": round(w_rev, 2)},
            "monthly": {"avg_occ": round(m_occ, 1), "avg_adr": round(m_adr, 2), "avg_revpar": round(m_rev, 2)}
        }
    return output

def run():
    print("Consolidating operational interval records...")
    daily_snapshots = load_raw_daily_files()
    
    master_ledger = {}
    for date_str in sorted(daily_snapshots.keys()):
        master_ledger[date_str] = process_final_daily_data(date_str, daily_snapshots[date_str])
        
    with open(HISTORY_LEDGER, "w") as f:
        json.dump(master_ledger, f, indent=2)

    aggregates = compile_periodic_averages(master_ledger)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(aggregates, f, indent=2)
    print("STR Database updates successfully written to disk.")

if __name__ == "__main__":
    run()
