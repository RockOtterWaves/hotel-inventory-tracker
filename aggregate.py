import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "aggregates.json"
HISTORY_LEDGER = DATA_DIR / "history_ledger.json"

CAPACITIES = {"Tarzana Inn": 49, "Sea Air Inn": 24, "Blufftop Inn": 32}

def load_raw_daily_files():
    daily_snapshots = {}
    for f in DATA_DIR.glob("*.json"):
        if f.name in ["aggregates.json", "history_ledger.json"]: continue
        try:
            with open(f, "r") as open_file: daily_snapshots[f.stem] = json.load(open_file)
        except: continue
    return daily_snapshots

def process_final_daily_data(date_str, data_dict):
    day_compiled = {}
    for prop_name, capacity in CAPACITIES.items():
        entries = data_dict.get(prop_name, [])
        if not entries: continue
        entries.sort(key=lambda x: x.get("scraped_at", ""))
        
        last_valid_summary = next((e.get("summary") for e in reversed(entries) if e.get("summary")), {})
        sold = max(capacity - last_valid_summary.get("total_remaining", capacity), 0)
        
        day_compiled[prop_name] = {
            "date": date_str, "occ": round((sold / capacity) * 100, 1),
            "adr": round(last_valid_summary.get("blended_adr", 0), 2),
            "rev": round(((sold / capacity) * last_valid_summary.get("blended_adr", 0)), 2)
        }
    return day_compiled

def run():
    ledger = {d: process_final_daily_data(d, s) for d, s in load_raw_daily_files().items()}
    with open(HISTORY_LEDGER, "w") as f: json.dump(ledger, f, indent=2)

    # Calendar Anchored Buckets
    aggs = defaultdict(lambda: {"weeks": defaultdict(list), "months": defaultdict(list)})
    for d_str, props in ledger.items():
        dt = datetime.strptime(d_str, "%Y-%m-%d")
        w_key = f"{dt.year}-W{dt.isocalendar()[1]}"
        m_key = f"{dt.year}-{dt.strftime('%m')}"
        for p, m in props.items():
            aggs[p]["weeks"][w_key].append(m); aggs[p]["months"][m_key].append(m)

    final = {}
    for p, periods in aggs.items():
        final[p] = {"weeks": {}, "months": {}}
        for k, v in periods["weeks"].items():
            final[p]["weeks"][k] = {"occ": round(sum(i['occ'] for i in v)/len(v),1), "adr": round(sum(i['adr'] for i in v)/len(v),2), "rev": round(sum(i['rev'] for i in v)/len(v),2)}
        for k, v in periods["months"].items():
            final[p]["months"][k] = {"occ": round(sum(i['occ'] for i in v)/len(v),1), "adr": round(sum(i['adr'] for i in v)/len(v),2), "rev": round(sum(i['rev'] for i in v)/len(v),2)}
            
    with open(OUTPUT_FILE, "w") as f: json.dump(final, f, indent=2)

if __name__ == "__main__": run()
