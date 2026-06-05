import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
LEDGER_FILE = DATA_DIR / "history_ledger.json"
AGG_FILE = DATA_DIR / "aggregates.json"

CAPACITIES = {"Tarzana Inn": 49, "Sea Air Inn": 24, "Blufftop Inn": 32}
SKIP_FILES = {"aggregates.json", "history_ledger.json", "config.json"}

def load_daily_files():
    daily = {}
    for f in sorted(DATA_DIR.glob("*.json")):
        if f.name in SKIP_FILES: continue
        try:
            with open(f) as fh: daily[f.stem] = json.load(fh)
        except: continue
    return daily

def process_day(date_str, data):
    result = {}
    for prop, capacity in CAPACITIES.items():
        entries = sorted(data.get(prop, []), key=lambda x: x.get("scraped_at", ""))
        summary = next((e.get("summary") for e in reversed(entries) if e.get("summary")), None)
        rooms   = next((e.get("rooms") for e in reversed(entries) if e.get("rooms")), [])
        if not summary: continue
        
        sold = max(0, capacity - summary.get("total_remaining", capacity))
        occ  = round(sold / capacity * 100, 1) if capacity else 0
        adr  = summary.get("blended_adr") or 0
        
        result[prop] = {
            "date": date_str, "occ": occ, "adr": round(float(adr), 2),
            "rev": round(occ / 100 * float(adr), 2), "rooms": rooms
        }
    return result

def build_aggregates(ledger):
    aggs = defaultdict(lambda: {"weeks": defaultdict(list), "months": defaultdict(list)})
    for d_str, props in ledger.items():
        dt = datetime.strptime(d_str, "%Y-%m-%d")
        w_key = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
        m_key = f"{dt.year}-{dt.month:02d}"
        for prop, m in props.items():
            aggs[prop]["weeks"][w_key].append(m)
            aggs[prop]["months"][m_key].append(m)
    
    final = {}
    for prop, periods in aggs.items():
        final[prop] = {"weeks": {}, "months": {}}
        for k, days in periods["weeks"].items():
            final[prop]["weeks"][k] = {"occ": round(sum(d['occ'] for d in days)/len(days),1), "adr": round(sum(d['adr'] for d in days)/len(days),2), "rev": round(sum(d['rev'] for d in days)/len(days),2)}
        for k, days in periods["months"].items():
            final[prop]["months"][k] = {"occ": round(sum(d['occ'] for d in days)/len(days),1), "adr": round(sum(d['adr'] for d in days)/len(days),2), "rev": round(sum(d['rev'] for d in days)/len(days),2)}
    return final

def run():
    daily = load_daily_files()
    ledger = {d: process_day(d, s) for d, s in daily.items() if process_day(d, s)}
    with open(LEDGER_FILE, "w") as f: json.dump(ledger, f, indent=2)
    with open(AGG_FILE, "w") as f: json.dump(build_aggregates(ledger), f, indent=2)

if __name__ == "__main__": run()
