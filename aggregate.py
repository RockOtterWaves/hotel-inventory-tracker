import json
import re
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "aggregates.json"
HISTORY_LEDGER = DATA_DIR / "history_ledger.json"

# Fixed capacity rules matching your structural portfolio parameters
CAPACITIES = {
    "Tarzana Inn": 49,
    "Sea Air Inn": 24,
    "Blufftop Inn": 32
}

def load_raw_daily_files():
    """Groups raw scraping files chronologically by calendar date."""
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
    Evaluates intraday data increments to calculate absolute EOD remaining keys 
    and estimates ADR based on room pickup pricing streams.
    """
    day_compiled = {}
    
    for prop_name, capacity in CAPACITIES.items():
        entries = data_dict.get(prop_name, [])
        if not entries:
            continue
            
        # Sort files chronologically by execution run time
        entries.sort(key=lambda x: x.get("scraped_at", ""))
        
        last_valid_summary = None
        last_valid_rooms = []
        
        # Room tracking variables for pickup tracking
        previous_rooms = {}
        total_pickup_revenue = 0.0
        total_rooms_picked_up = 0
        all_observed_rates = []

        for e in entries:
            rooms_list = e.get("rooms", [])
            summary = e.get("summary", {})
            if not summary or not rooms_list:
                continue

            # Skip snapshots where the hotel is offline or closed (total remaining = 0 but ADR = 0)
            if summary.get("total_remaining", 0) == 0 and summary.get("blended_adr", 0) == 0:
                continue
                
            # Keep track of the last available snapshot before any office closures
            last_valid_summary = summary
            last_valid_rooms = rooms_list
            
            # Loop through room types to identify pickup variations
            for r in rooms_list:
                rtype = r["room_type"]
                rate = r["rate"]
                left = r["rooms_left"]
                if rate > 0:
                    all_observed_rates.append(rate)
                
                if rtype in previous_rooms:
                    prev_left = previous_rooms[rtype]
                    # If vacancy dropped, rooms were picked up at this rate
                    if left < prev_left:
                        picked_up = prev_left - left
                        total_pickup_revenue += (picked_up * rate)
                        total_rooms_picked_up += picked_up
                
                previous_rooms[rtype] = left

        # Fallback values if no valid data points remain open
        if not last_valid_summary:
            continue

        final_remaining = last_valid_summary.get("total_remaining", capacity)
        final_sold = max(capacity - final_remaining, 0)
        final_occ = round((final_sold / capacity) * 100, 1) if capacity else 0.0

        # Calculate estimated ADR
        if total_rooms_picked_up > 0:
            estimated_adr = round(total_pickup_revenue / total_rooms_picked_up, 2)
        elif last_valid_summary.get("blended_adr", 0) > 0:
            estimated_adr = last_valid_summary["blended_adr"]
        elif all_observed_rates:
            estimated_adr = round(sum(all_observed_rates) / len(all_observed_rates), 2)
        else:
            estimated_adr = 0.0

        # Calculate RevPAR
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
            "rooms": last_valid_rooms
        }
        
    return day_compiled

def compile_periodic_averages(ledger_data):
    """Groups daily data blocks into fixed weekly and monthly windows."""
    output = {}
    for prop_name in CAPACITIES.keys():
        prop_history = [day[prop_name] for day in ledger_data.values() if prop_name in day]
        if not prop_history:
            continue
            
        prop_history.sort(key=lambda x: x["date"])
        
        # Running Weekly (Last 7 Records)
        w_slice = prop_history[-7:]
        w_occ = sum(d["occupancy_pct"] for d in w_slice) / len(w_slice) if w_slice else 0
        w_adr = sum(d["estimated_adr"] for d in w_slice) / len(w_slice) if w_slice else 0
        w_rev = sum(d["revpar"] for d in w_slice) / len(w_slice) if w_slice else 0
        
        # Running Monthly (Last 30 Records)
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
    print("Beginning STR-Standard Historical Ledger consolidation process...")
    daily_snapshots = load_raw_daily_files()
    
    master_ledger = {}
    # Process files sequentially by date
    for date_str in sorted(daily_snapshots.keys()):
        master_ledger[date_str] = process_final_daily_data(date_str, daily_snapshots[date_str])
        
    # Write historical ledger to file
    with open(HISTORY_LEDGER, "w") as f:
        json.dump(master_ledger, f, indent=2)
    print(f"Successfully finalized {len(master_ledger)} tracking entries inside {HISTORY_LEDGER}")

    # Compute rolling metrics for the dashboard cards
    aggregates = compile_periodic_averages(master_ledger)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(aggregates, f, indent=2)
    print("Successfully compiled metrics arrays into aggregates.json!")

if __name__ == "__main__":
    run()
