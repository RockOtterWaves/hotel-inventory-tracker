"""
Hotel Inventory & Rate Tracker
Tracks same-day remaining inventory and rates for multiple properties via IPMS247 booking pages.
Runs automatically 5x/day + generates a 7AM next-day summary report.
Automatically commits and pushes data back to GitHub when running in automation.
"""

import asyncio
import json
import re
import sys
import random
import logging
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

# ─── Project paths ────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
LOG_DIR     = BASE_DIR / "logs"
REPORT_DIR  = BASE_DIR / "reports"
CONFIG_FILE = BASE_DIR / "config.json"

for d in [DATA_DIR, LOG_DIR, REPORT_DIR]:
    d.mkdir(exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
log_path = LOG_DIR / f"tracker_{date.today():%Y-%m-%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hotel_tracker")

# ─── Default config ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "properties": [
        {"name": "Tarzana Inn",  "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn",  "total_rooms": 49},
        {"name": "Sea Air Inn",  "url": "https://live.ipms247.com/booking/book-rooms-seaairinn",   "total_rooms": 24},
        {"name": "Blufftop Inn", "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict", "total_rooms": 32},
    ],
    "schedule": {
        "intraday_times": ["09:00", "15:00", "18:00", "21:30", "23:59"],
        "morning_report_time": "07:00"
    },
    "scraper": {
        "page_timeout_ms": 60000,
        "retry_attempts": 3,
        "retry_delay_sec": 20
    }
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    logger.info("Created default config.json")
    return DEFAULT_CONFIG

# ─── GitHub Push Helper ───────────────────────────────────────────────────────
def github_push_changes(commit_message: str):
    """Helper to commit and push updated data/reports back to the repository."""
    try:
        import os
        if not os.getenv("GITHUB_ACTIONS"):
            logger.info("Local environment detected. Skipping automatic GitHub push.")
            return

        logger.info("GitHub Actions environment detected. Syncing changes to repository...")
        
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        
        subprocess.run(["git", "add", "data/*", "reports/*"], check=False)
        
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not status.stdout.strip():
            logger.info("No new data changes to commit.")
            return

        subprocess.run(["git", "commit", "-m", commit_message], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        logger.info("Successfully pushed changes to GitHub repository branch.")
    except Exception as e:
        logger.error(f"Failed to push changes to GitHub: {e}")

# ─── Anti-bot helpers ─────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

async def build_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    return browser, context

# ─── Date helpers ─────────────────────────────────────────────────────────────
def is_date_valid(page_val: str, target: date) -> bool:
    if not page_val:
        return True
    for fmt in ["%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(page_val.strip(), fmt).date() == target
        except ValueError:
            pass
    return True

# ─── Core scraper ─────────────────────────────────────────────────────────────
async def scrape_property(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    for attempt in range(1, s["retry_attempts"] + 1):
        logger.info(f"[{prop['name']}] Attempt {attempt}/{s['retry_attempts']}")
        try:
            result = await _scrape(prop, cfg, target)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[{prop['name']}] Attempt {attempt} failed: {e}")
        if attempt < s["retry_attempts"]:
            await asyncio.sleep(s["retry_delay_sec"])
    logger.error(f"[{prop['name']}] All attempts exhausted.")
    return None


async def _scrape(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    name     = prop["name"]
    url      = prop["url"]
    s        = cfg.get("scraper", DEFAULT_CONFIG["scraper"])

    async with async_playwright() as p:
        browser, context = await build_context(p)
        page = await context.new_page()
        page.set_default_timeout(s["page_timeout_ms"])

        try:
            logger.info(f"[{name}] Navigating to {url}")
            await page.goto(url, wait_until="commit", timeout=s["page_timeout_ms"])

            # Human Simulation: Slight scroll to trigger asset lazy loading
            await page.evaluate("window.scrollTo(0, 300);")
            await asyncio.sleep(1.5)
            await page.evaluate("window.scrollTo(0, 0);")

            # ── Step 1: Validate date hasn't rolled over ───────────────────
            page_checkin = await _read_date_field(page, "checkin")
            if page_checkin:
                logger.info(f"[{name}] Page landing check-in date: '{page_checkin}'")
                if not is_date_valid(page_checkin, target):
                    logger.warning(f"[{name}] Date rolled over to next day on page. Skipping tracking for {target}.")
                    return None

            # ── Step 2: Active DOM Hydration Polling Loop ─────────────────
            logger.info(f"[{name}] Waiting for room records to hydrate inside DOM...")
            
            hydrated = False
            for loop in range(15):  # Poll every 1 second for up to 15 seconds
                html_check = await page.content()
                # Check for standard room classifications or structural elements containing inventory
                if any(k in html_check for k in ["King", "Queen", "Suite", "Room", "Standard", "Deluxe"]):
                    logger.info(f"[{name}] Active room data detected in DOM after {loop}s.")
                    hydrated = True
                    break
                await asyncio.sleep(1)

            if not hydrated:
                logger.warning(f"[{name}] Polling window complete. Proceeding with standard structural parsing.")

            # Extra buffer for text bindings to finalize
            await asyncio.sleep(2)

            # ── Step 3: Grab full page HTML and parse ─────────────────────
            html  = await page.content()
            rooms = _parse_rooms_ipms247(html, name)

            if not rooms:
                snippet = html[html.find("id=\"resheader\""):html.find("id=\"resheader\"")+2000] if "id=\"resheader\"" in html else html[:2000]
                logger.warning(f"[{name}] No rooms parsed. HTML snapshot snippet:\n{snippet[:500]}")
                return None

            summary = _summarise(rooms, prop["total_rooms"])
            logger.info(f"[{name}] ✓ {len(rooms)} room types | remaining={summary['total_remaining']} | ADR=${summary['blended_adr']} | occ={summary['estimated_occupancy_pct']}%")

            return {
                "property":    name,
                "url":         url,
                "total_rooms": prop["total_rooms"],
                "scraped_at":  datetime.now().isoformat(),
                "target_date": target.isoformat(),
                "rooms":       rooms,
                "summary":     summary,
            }

        finally:
            await context.close()
            await browser.close()


async def _read_date_field(page, field_type: str) -> str:
    selectors = [
        f"input[id*='{field_type}']",
        f"input[name*='{field_type}']",
        f"[class*='{field_type}'] input",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                val = await el.input_value()
                if val:
                    return val
        except Exception:
            continue
    return ""


# ─── IPMS247-specific HTML parser ─────────────────────────────────────────────
def _parse_rooms_ipms247(html: str, hotel_name: str) -> list:
    """
    Parse room data from IPMS247 booking engine HTML layout blocks.
    """
    rooms = []
    seen  = set()

    block_patterns = [
        r'(?=<(?:div|tr)[^>]+class="[^"]*vres_room_info[^"]*")',
        r'(?=<(?:div|tr)[^>]+class="[^"]*roomTypeRow[^"]*")',
        r'(?=<(?:div|tr)[^>]+class="[^"]*room-type-row[^"]*")',
        r'(?=<(?:div|tr)[^>]+id="[^"]*roomType[^"]*")',
    ]

    blocks = []
    for pat in block_patterns:
        parts = re.split(pat, html)
        if len(parts) > 2:
            blocks = parts[1:]  
            logger.info(f"[{hotel_name}] Split into {len(blocks)} blocks via pattern")
            break

    if not blocks:
        logger.info(f"[{hotel_name}] Using dollar-sign fallback block split")
        positions = [m.start() for m in re.finditer(r'\$\s*\d{2,3}', html)]
        seen_pos  = set()
        for pos in positions:
            bucket = pos // 3000
            if bucket not in seen_pos:
                seen_pos.add(bucket)
                blocks.append(html[max(0, pos-2000):pos+2000])

    if not blocks:
        blocks = [html]

    not_avail_re = re.compile(r'not\s*available|sold\s*out|unavailable|no\s*rooms', re.I)

    for block in blocks:
        name_match = None
        for name_pat in [
            r'class="[^"]*(?:vres_roomName|vres_room_name|roomTypeName|room_type_name)[^"]*"[^>]*>\s*([^<]{4,80})',
            r'class="[^"]*(?:vres_roomType|roomType)[^"]*"[^>]*>\s*([^<]{4,80})',
            r'(?:Deluxe|Comfort|Standard|Superior|Suite|King|Queen|Double|Twin|Studio|Single|Accessible)[^\n<]{2,60}',
        ]:
            name_match = re.search(name_pat, block, re.I)
            if name_match:
                break

        if not name_match:
            continue

        raw_name = name_match.group(1) if name_match.lastindex else name_match.group(0)
        room_name = re.sub(r'<[^>]+>', '', raw_name).strip()
        room_name = re.sub(r'\s+', ' ', room_name).strip()

        room_name = re.sub(r'(?i)(no pets|non-smoking|non smoking|smoking|downstairs|upstairs)\s*[,\-]?\s*', '', room_name).strip()
        room_name = room_name.strip(' -,')

        if len(room_name) < 3 or room_name in seen:
            continue
        seen.add(room_name)

        if not_avail_re.search(block):
            rooms.append({"room_type": room_name, "available": False, "rooms_left": 0, "rate": None})
            continue

        rate = None
        for rate_pat in [
            r'class="[^"]*(?:vres_rate|roomRate|rate_price|price)[^"]*"[^>]*>\s*\$?\s*([\d,]+(?:\.\d{2})?)',
            r'\$\s*([\d,]+(?:\.\d{2})?)',
            r'USD\s*([\d,]+(?:\.\d{2})?)',
        ]:
            m = re.search(rate_pat, block)
            if m:
                try:
                    val = float(m.group(1).replace(',', ''))
                    if 20 < val < 2000:  
                        rate = val
                        break
                except Exception:
                    pass

        rooms_left = None
        for inv_pat in [
            r'(\d+)\s*[Rr]oom[s]?\s*[Ll]eft',
            r'[Oo]nly\s*(\d+)\s*[Rr]oom',
            r'[Hh]urry[!]?\s*(\d+)\s*[Rr]oom',
            r'(\d+)\s*[Ll]eft',
            r'[Rr]emaining[:\s]+(\d+)',
        ]:
            m = re.search(inv_pat, block)
            if m:
                try:
                    rooms_left = int(m.group(1))
                    break
                except Exception:
                    pass

        if rate is None and rooms_left is None:
            continue

        rooms.append({
            "room_type":  room_name,
            "available":  True,
            "rooms_left": rooms_left,
            "rate":       rate,
        })

    logger.info(f"[{hotel_name}] Parsed {len(rooms)} room type(s): {[r['room_type'] for r in rooms]}")
    return rooms


def _summarise(rooms: list, total_rooms: int) -> dict:
    available = [r for r in rooms if r.get("available")]
    rated     = [r for r in available if r.get("rate") is not None]
    known_inv = [r["rooms_left"] for r in available if r.get("rooms_left") is not None]

    total_remaining = sum(known_inv) if known_inv else len(available)
    blended_adr     = round(sum(r["rate"] for r in rated) / len(rated), 2) if rated else None
    sold            = max(0, total_rooms - total_remaining)
    occupancy       = round(sold / total_rooms * 100, 1) if total_rooms else None

    return {
        "total_rooms_property":    total_rooms,
        "total_remaining":         total_remaining,
        "estimated_sold":          sold,
        "estimated_occupancy_pct": occupancy,
        "blended_adr":             blended_adr,
        "room_types_available":    len(available),
        "room_types_sold_out":     len([r for r in rooms if not r.get("available")]),
    }


# ─── Persistence ──────────────────────────────────────────────────────────────
def load_day_data(d: date) -> dict:
    path = DATA_DIR / f"{d:%Y-%m-%d}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_day_data(d: date, data: dict):
    with open(DATA_DIR / f"{d:%Y-%m-%d}.json", "w") as f:
        json.dump(data, f, indent=2, default=str)

def append_snapshot(d: date, result: dict):
    data = load_day_data(d)
    prop = result["property"]
    if prop not in data:
        data[prop] = []
    data[prop].append(result)
    save_day_data(d, data)
    s = result["summary"]
    logger.info(f"[{prop}] Saved — remaining={s['total_remaining']} ADR=${s['blended_adr']} occ={s['estimated_occupancy_pct']}%")


# ─── Reports ──────────────────────────────────────────────────────────────────
def generate_morning_report(report_date: date):
    prev  = report_date - timedelta(days=1)
    data  = load_day_data(prev)
    if not data:
        logger.warning(f"No data for {prev}. Morning report skipped.")
        return

    lines = [
        "=" * 60,
        f"  HOTEL INVENTORY REPORT — Night of {prev:%B %d, %Y}",
        f"  Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=" * 60,
    ]
    for prop, snaps in data.items():
        valid = [s for s in snaps if s.get("summary")]
        if not valid:
            continue
        last = valid[-1]
        s    = last["summary"]
        lines += [
            f"\n  🏨  {prop}",
            f"  {'─'*50}",
            f"  Total Rooms       : {s['total_rooms_property']}",
            f"  Est. Rooms Sold   : {s['estimated_sold']}",
            f"  Remaining         : {s['total_remaining']}",
            f"  EST. OCCUPANCY    : {s['estimated_occupancy_pct']}%",
            f"  Blended ADR       : ${s['blended_adr'] or 'N/A'}",
            f"\n  Snapshot Timeline:",
            f"  {'Time':<10} {'Remaining':>10} {'ADR':>10} {'Occ%':>8}",
            f"  {'-'*40}",
        ]
        for snap in valid:
            t   = datetime.fromisoformat(snap["scraped_at"]).strftime("%H:%M")
            rm  = snap["summary"]["total_remaining"]
            adr = snap["summary"]["blended_adr"] or "—"
            oc  = snap["summary"]["estimated_occupancy_pct"]
            lines.append(f"  {t:<10} {str(rm):>10} {str(adr):>10} {str(oc)+' %':>8}")

    lines.append(f"\n{'='*60}\n")
    text = "\n".join(lines)

    rpath = REPORT_DIR / f"morning_report_{prev:%Y-%m-%d}.txt"
    with open(rpath, "w") as f:
        f.write(text)
    print(text)
    logger.info(f"Morning report written → {rpath}")

    rows = []
    for prop, snaps in data.items():
        for snap in snaps:
            if snap.get("summary"):
                rows.append({"property": prop, "scraped_at": snap["scraped_at"], **snap["summary"]})
    if rows:
        pd.DataFrame(rows).to_csv(REPORT_DIR / f"data_{prev:%Y-%m-%d}.csv", index=False)


# ─── Orchestrator ─────────────────────────────────────────────────────────────
async def run_all_properties(cfg: dict, label: str = "manual"):
    target = date.today()
    logger.info(f"=== Scrape run [{label}] for {target} ===")
    for prop in cfg.get("properties", []):
        await asyncio.sleep(random.uniform(4, 8))
        result = await scrape_property(prop, cfg, target)
        if result:
            append_snapshot(target, result)
            s = result["summary"]
            print(f"\n  [{result['property']}] remaining={s['total_remaining']} ADR=${s['blended_adr']} occ={s['estimated_occupancy_pct']}%\n")
        else:
            logger.error(f"[{prop['name']}] No data returned for [{label}].")
            
    logger.info(f"=== Run [{label}] complete ===")
    github_push_changes(f"chore: data snapshot update [{label}] {target.isoformat()}")


# ─── Scheduler ────────────────────────────────────────────────────────────────
def start_scheduler(cfg: dict):
    tz        = "America/Los_Angeles"
    scheduler = AsyncIOScheduler(timezone=tz)
    sched_cfg = cfg.get("schedule", DEFAULT_CONFIG["schedule"])

    for t in sched_cfg.get("intraday_times", []):
        h, m = map(int, t.split(":"))
        scheduler.add_job(run_all_properties, CronTrigger(hour=h, minute=m, timezone=tz),
                          args=[cfg, f"scheduled-{t}"], id=f"scrape_{t.replace(':','')}")
        logger.info(f"Scheduled scrape at {t} PT")

    rt = sched_cfg.get("morning_report_time", "07:00")
    rh, rm = map(int, rt.split(":"))
    scheduler.add_job(generate_morning_report, CronTrigger(hour=rh, minute=rm, timezone=tz),
                      args=[date.today()], id="morning_report")
    logger.info(f"Scheduled morning report at {rt} PT")

    scheduler.start()
    return scheduler


# ─── Entry point ──────────────────────────────────────────────────────────────
async def main():
    cfg = load_config()
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "run":
            await run_all_properties(cfg, "manual")
            return
        elif cmd == "report":
            generate_morning_report(date.today())
            github_push_changes(f"chore: morning summary report compile {date.today().isoformat()}")
            return

    logger.info("Starting Hotel Inventory Tracker (scheduled mode)...")
    scheduler = start_scheduler(cfg)
    await run_all_properties(cfg, "startup")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
