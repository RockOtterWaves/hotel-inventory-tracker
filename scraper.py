"""
Hotel Inventory & Rate Tracker
Tracks same-day remaining inventory and rates for multiple properties via IPMS247 booking pages.
Runs automatically 5x/day + generates a 7AM next-day summary report.
"""

import asyncio
import json
import os
import random
import re
import sys
import time
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
import traceback

# ─── Third-party (installed via requirements.txt) ───────────────────────────
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

# ─── Project paths ───────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
LOG_DIR    = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"
CONFIG_FILE= BASE_DIR / "config.json"

for d in [DATA_DIR, LOG_DIR, REPORT_DIR]:
    d.mkdir(exist_ok=True)

# ─── Logging ─────────────────────────────────────────────────────────────────
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

# ─── Default config (user edits config.json) ─────────────────────────────────
DEFAULT_CONFIG = {
    "properties": [
        {
            "name": "Tarzana Inn",
            "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn",
            "total_rooms": 49
        },
        {
            "name": "Sea Air Inn",
            "url": "https://live.ipms247.com/booking/book-rooms-seaairinn",
            "total_rooms": 24
        },
        {
            "name": "Blufftop Inn",
            "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict",
            "total_rooms": 32
        }
    ],
    "schedule": {
        "intraday_times": ["09:00", "15:00", "18:00", "21:30", "23:59"],
        "morning_report_time": "07:00"
    },
    "scraper": {
        "min_delay_sec": 3,
        "max_delay_sec": 8,
        "page_timeout_ms": 45000,
        "retry_attempts": 3,
        "retry_delay_sec": 15
    }
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        logger.info("Loaded config.json")
        return cfg
    # Write defaults so user can edit
    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    logger.info("Created default config.json")
    return DEFAULT_CONFIG

# ─── Anti-bot helpers ─────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]

async def human_delay(min_s=1.5, max_s=4.0):
    """Randomised delay to mimic human browsing."""
    await asyncio.sleep(random.uniform(min_s, max_s))

async def build_browser_context(playwright, cfg: dict):
    """Launch a stealth browser context."""
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-gpu",
        ],
    )
    context = await browser.new_context(
        user_agent=ua,
        viewport=vp,
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
        },
    )
    # Remove navigator.webdriver fingerprint
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        window.chrome = { runtime: {} };
    """)
    return browser, context

# ─── Date helpers ─────────────────────────────────────────────────────────────
def today_str() -> str:
    return date.today().strftime("%m-%d-%Y")

def is_same_day_booking_valid(page_checkin_str: str, target_date: date) -> bool:
    """
    Verify the booking page is still showing the correct check-in date.
    Handles MM-DD-YYYY and MM/DD/YYYY formats.
    """
    if not page_checkin_str:
        return True  # can't verify, proceed
    try:
        clean = page_checkin_str.strip().replace("/", "-")
        page_date = datetime.strptime(clean, "%m-%d-%Y").date()
        return page_date == target_date
    except Exception:
        return True  # parse failed, don't discard

# ─── Core scraping logic ──────────────────────────────────────────────────────
async def scrape_property(prop: dict, cfg: dict, target_date: date) -> Optional[dict]:
    """
    Scrapes one property's booking page for today's remaining rooms and rates.
    Returns a result dict or None on failure.
    """
    name  = prop["name"]
    url   = prop["url"]
    s_cfg = cfg.get("scraper", DEFAULT_CONFIG["scraper"])

    for attempt in range(1, s_cfg["retry_attempts"] + 1):
        logger.info(f"[{name}] Attempt {attempt}/{s_cfg['retry_attempts']} ...")
        try:
            result = await _do_scrape(prop, cfg, target_date)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[{name}] Attempt {attempt} failed: {e}")
            if attempt < s_cfg["retry_attempts"]:
                await asyncio.sleep(s_cfg["retry_delay_sec"] * attempt)
    logger.error(f"[{name}] All attempts exhausted.")
    return None

async def _do_scrape(prop: dict, cfg: dict, target_date: date) -> Optional[dict]:
    name      = prop["name"]
    url       = prop["url"]
    s_cfg     = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    checkin   = target_date.strftime("%m-%d-%Y")
    checkout  = (target_date + timedelta(days=1)).strftime("%m-%d-%Y")

    async with async_playwright() as p:
        browser, context = await build_browser_context(p, cfg)
        page = await context.new_page()
        page.set_default_timeout(s_cfg["page_timeout_ms"])

        try:
            # ── Navigate ──────────────────────────────────────────────────────
            await page.goto(url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            # ── Set check-in / check-out dates ────────────────────────────────
            await _set_dates(page, checkin, checkout)
            await human_delay(1, 2.5)

            # ── Click "Check Availability" ────────────────────────────────────
            btn = page.locator(
                "button:has-text('Check Availability'), "
                "a:has-text('Check Availability'), "
                "input[value*='Check Availability']"
            )
            if await btn.count() > 0:
                await btn.first.click()
                await human_delay(3, 5)
            else:
                # Try submitting via Enter on the checkout field
                await page.keyboard.press("Tab")
                await page.keyboard.press("Enter")
                await human_delay(3, 5)

            # Wait for room listings
            await page.wait_for_selector(
                ".room-type-info, .roomtype-list, [class*='room'], .booking-room",
                timeout=30000
            )
            await human_delay(1, 2)

            # ── Validate that the page date matches target ────────────────────
            page_checkin = await _read_checkin_date(page)
            if not is_same_day_booking_valid(page_checkin, target_date):
                logger.warning(
                    f"[{name}] Page check-in date '{page_checkin}' != target "
                    f"'{checkin}'. Skipping this run (date rolled over)."
                )
                return None

            # ── Parse rooms ───────────────────────────────────────────────────
            html  = await page.content()
            rooms = _parse_rooms(html, name)

            if not rooms:
                logger.warning(f"[{name}] No rooms parsed from page.")
                return None

            # ── Validate completeness (double-check) ──────────────────────────
            _validate_rooms(rooms, name)

            return {
                "property": name,
                "url": url,
                "total_rooms": prop["total_rooms"],
                "scraped_at": datetime.now().isoformat(),
                "target_date": target_date.isoformat(),
                "rooms": rooms,
                "summary": _summarise(rooms, prop["total_rooms"]),
            }

        finally:
            await context.close()
            await browser.close()

async def _set_dates(page, checkin: str, checkout: str):
    """Try multiple selector strategies to set check-in / checkout fields."""
    # Strategy 1: fill input fields directly
    for sel, val in [
        ("input[name*='checkin'], input[id*='checkin'], input[placeholder*='Check']", checkin),
        ("input[name*='checkout'], input[id*='checkout']", checkout),
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.triple_click()
                await el.fill(val)
                await human_delay(0.3, 0.8)
                continue
        except Exception:
            pass

    # Strategy 2: IPMS247 specific date fields
    for field_hint, val in [("checkin", checkin), ("checkout", checkout)]:
        try:
            el = page.locator(f"[id*='{field_hint}'], [name*='{field_hint}']").first
            if await el.count() > 0:
                await el.triple_click()
                await el.fill(val)
                await human_delay(0.3, 0.7)
        except Exception:
            pass

def _parse_rooms(html: str, hotel_name: str) -> list:
    """
    Robustly parse room types, remaining inventory, and rates from HTML.
    Works across IPMS247 live.ipms247.com and book.ipms247.com layouts.
    """
    rooms = []

    # ── Price patterns ────────────────────────────────────────────────────────
    rate_patterns = [
        r'\$\s*([\d,]+(?:\.\d{2})?)',
        r'USD\s*([\d,]+(?:\.\d{2})?)',
        r'([\d,]+(?:\.\d{2})?)\s*(?:USD|per night)',
    ]

    # ── Inventory patterns ────────────────────────────────────────────────────
    inv_patterns = [
        r'(\d+)\s*[Rr]oom[s]?\s*[Ll]eft',
        r'[Oo]nly\s*(\d+)\s*[Rr]oom',
        r'[Hh]urry[!]?\s*(\d+)\s*[Rr]oom',
        r'(\d+)\s*[Ll]eft',
    ]
    not_available_re = re.compile(
        r'[Nn]ot\s+[Aa]vailable|[Ss]old\s+[Oo]ut|[Uu]navailable', re.I
    )

    # ── Room-block splitting ──────────────────────────────────────────────────
    # Split by common room-block wrappers; fall back to rate-anchored blocks
    blocks = []

    # Try splitting on room cards (class variations across IPMS247 templates)
    block_re = re.compile(
        r'(?:class="[^"]*(?:room-type|roomtype|room_type|room-info|room-detail)[^"]*")',
        re.I,
    )
    split_positions = [m.start() for m in block_re.finditer(html)]
    if len(split_positions) >= 2:
        for i, pos in enumerate(split_positions):
            end = split_positions[i+1] if i+1 < len(split_positions) else len(html)
            blocks.append(html[pos:end])
    else:
        # Fallback: anchor on price tags
        dollar_positions = [m.start() for m in re.finditer(r'\$\s*\d+', html)]
        seen = set()
        for dp in dollar_positions:
            start = max(0, dp - 1500)
            end   = min(len(html), dp + 1500)
            chunk = html[start:end]
            # De-dup by approximate region
            key = dp // 2000
            if key not in seen:
                seen.add(key)
                blocks.append(chunk)

    if not blocks:
        blocks = [html]  # last resort: parse entire page

    seen_names = set()
    for block in blocks:
        # ── Room name ─────────────────────────────────────────────────────────
        name_match = re.search(
            r'<(?:h[1-6]|div|span|td|p)[^>]*class="[^"]*(?:room[_-]?name|room[_-]?title|room[_-]?type[_-]?name)[^"]*"[^>]*>\s*([^<]{5,80})',
            block, re.I
        )
        if not name_match:
            name_match = re.search(
                r'(?:Deluxe|Comfort|Standard|Superior|Suite|King|Queen|Double|Twin|Single|Studio)'
                r'[^<\n]{2,60}',
                block
            )
        if not name_match:
            continue
        room_name = re.sub(r'\s+', ' ', name_match.group(1) if name_match.lastindex else name_match.group(0)).strip()
        room_name = re.sub(r'<[^>]+>', '', room_name).strip()  # strip any leftover tags

        if len(room_name) < 3 or room_name in seen_names:
            continue
        seen_names.add(room_name)

        # ── Availability / rate ───────────────────────────────────────────────
        if not_available_re.search(block):
            rooms.append({
                "room_type": room_name,
                "available": False,
                "rooms_left": 0,
                "rate": None,
            })
            continue

        # Rate
        rate = None
        for pat in rate_patterns:
            m = re.search(pat, block)
            if m:
                try:
                    rate = float(m.group(1).replace(",", ""))
                    break
                except Exception:
                    pass

        # Rooms left
        rooms_left = None
        for pat in inv_patterns:
            m = re.search(pat, block)
            if m:
                try:
                    rooms_left = int(m.group(1))
                    break
                except Exception:
                    pass

        if rate is None and rooms_left is None:
            # Block with a room name but no useful data — skip
            continue

        rooms.append({
            "room_type": room_name,
            "available": True,
            "rooms_left": rooms_left,  # None = unknown but available
            "rate": rate,
        })

    logger.info(f"[{hotel_name}] Parsed {len(rooms)} room type(s).")
    return rooms

def _validate_rooms(rooms: list, hotel_name: str):
    """Log warnings if data looks incomplete."""
    no_rate  = [r for r in rooms if r.get("available") and r.get("rate") is None]
    no_inv   = [r for r in rooms if r.get("available") and r.get("rooms_left") is None]
    if no_rate:
        logger.warning(f"[{hotel_name}] Rooms missing rate: {[r['room_type'] for r in no_rate]}")
    if no_inv:
        logger.info(f"[{hotel_name}] Rooms with unknown inventory (may show 'Add Room' without count): "
                    f"{[r['room_type'] for r in no_inv]}")

def _summarise(rooms: list, total_rooms: int) -> dict:
    """Compute blended ADR, total remaining rooms, estimated occupancy."""
    available_rooms = [r for r in rooms if r.get("available")]
    rated_rooms     = [r for r in available_rooms if r.get("rate") is not None]

    # Total rooms left (use sum of rooms_left where known; else count unique available types)
    known_counts = [r["rooms_left"] for r in available_rooms if r.get("rooms_left") is not None]
    total_remaining = sum(known_counts) if known_counts else len(available_rooms)

    # Blended ADR (weighted average isn't possible without individual room counts,
    # so we use simple average of available rated rooms)
    if rated_rooms:
        blended_adr = round(sum(r["rate"] for r in rated_rooms) / len(rated_rooms), 2)
    else:
        blended_adr = None

    sold = total_rooms - total_remaining
    occupancy_pct = round(max(0, sold) / total_rooms * 100, 1) if total_rooms else None

    return {
        "total_rooms_property": total_rooms,
        "total_remaining": total_remaining,
        "estimated_sold": max(0, sold),
        "estimated_occupancy_pct": occupancy_pct,
        "blended_adr": blended_adr,
        "room_types_available": len(available_rooms),
        "room_types_sold_out": len([r for r in rooms if not r.get("available")]),
    }

async def _read_checkin_date(page) -> str:
    """Attempt to read the check-in date currently shown on the page."""
    for sel in [
        "input[name*='checkin']", "input[id*='checkin']",
        "[class*='checkin'] input", "[class*='check-in'] input",
        "input[placeholder*='Check']",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                val = await el.input_value()
                if val:
                    return val
        except Exception:
            pass
    return ""

# ─── Persistence ─────────────────────────────────────────────────────────────
def load_day_data(target_date: date) -> dict:
    """Load today's accumulated data file."""
    path = DATA_DIR / f"{target_date:%Y-%m-%d}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_day_data(target_date: date, data: dict):
    path = DATA_DIR / f"{target_date:%Y-%m-%d}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def append_snapshot(target_date: date, result: dict):
    """Add a scrape snapshot to the day's data file."""
    data = load_day_data(target_date)
    prop = result["property"]
    if prop not in data:
        data[prop] = []
    data[prop].append(result)
    save_day_data(target_date, data)
    logger.info(
        f"[{prop}] Snapshot saved — "
        f"remaining: {result['summary']['total_remaining']}, "
        f"ADR: {result['summary']['blended_adr']}, "
        f"occupancy: {result['summary']['estimated_occupancy_pct']}%"
    )

# ─── Reports ─────────────────────────────────────────────────────────────────
def generate_morning_report(report_date: date):
    """
    7 AM report: compute previous day's final occupancy & ADR.
    Uses last valid snapshot before midnight for each property.
    """
    prev_date = report_date - timedelta(days=1)
    data      = load_day_data(prev_date)

    if not data:
        logger.warning(f"No data found for {prev_date}. Morning report skipped.")
        return

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  HOTEL INVENTORY REPORT — Night of {prev_date:%B %d, %Y}")
    lines.append(f"  Generated: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"{'='*60}")

    for prop_name, snapshots in data.items():
        if not snapshots:
            continue

        # Find last valid snapshot (ignore erroneous midnight data if date rolled)
        valid_snaps = [s for s in snapshots if s.get("summary")]
        if not valid_snaps:
            continue
        last = valid_snaps[-1]
        summary = last["summary"]

        lines.append(f"\n  🏨  {prop_name}")
        lines.append(f"  {'─'*50}")
        lines.append(f"  Total Rooms in Property : {summary['total_rooms_property']}")
        lines.append(f"  Estimated Rooms Sold    : {summary['estimated_sold']}")
        lines.append(f"  Remaining (unsold)      : {summary['total_remaining']}")
        lines.append(f"  ESTIMATED OCCUPANCY     : {summary['estimated_occupancy_pct']}%")
        lines.append(f"  Blended ADR             : ${summary['blended_adr'] or 'N/A'}")

        # Timeline table
        lines.append(f"\n  Snapshot Timeline:")
        lines.append(f"  {'Time':<10} {'Remaining':>10} {'ADR':>10} {'Occ%':>8}")
        lines.append(f"  {'-'*40}")
        for s in valid_snaps:
            t  = datetime.fromisoformat(s["scraped_at"]).strftime("%H:%M")
            rm = s["summary"]["total_remaining"]
            adr= s["summary"]["blended_adr"] or "—"
            oc = s["summary"]["estimated_occupancy_pct"]
            lines.append(f"  {t:<10} {str(rm):>10} {str(adr):>10} {str(oc)+' %':>8}")

    lines.append(f"\n{'='*60}\n")
    report_text = "\n".join(lines)

    rpath = REPORT_DIR / f"morning_report_{prev_date:%Y-%m-%d}.txt"
    with open(rpath, "w") as f:
        f.write(report_text)

    # Also produce a CSV for easy analysis
    _export_csv(data, prev_date)

    print(report_text)
    logger.info(f"Morning report written to {rpath}")

def _export_csv(data: dict, d: date):
    rows = []
    for prop, snaps in data.items():
        for s in snaps:
            if not s.get("summary"):
                continue
            row = {
                "property": prop,
                "scraped_at": s["scraped_at"],
                "target_date": s["target_date"],
                **s["summary"],
            }
            rows.append(row)
    if rows:
        df = pd.DataFrame(rows)
        csv_path = REPORT_DIR / f"data_{d:%Y-%m-%d}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(f"CSV exported to {csv_path}")

# ─── Orchestrator ─────────────────────────────────────────────────────────────
async def run_all_properties(cfg: dict, label: str = "manual"):
    """Scrape all configured properties for today."""
    target_date = date.today()
    logger.info(f"=== Starting scrape run [{label}] for {target_date} ===")

    props = cfg.get("properties", [])
    for prop in props:
        # Random inter-property delay (anti-bot)
        await asyncio.sleep(random.uniform(5, 15))
        result = await scrape_property(prop, cfg, target_date)
        if result:
            append_snapshot(target_date, result)
            _print_snapshot(result)
        else:
            logger.error(f"[{prop['name']}] Scrape returned no data for {label} run.")

    logger.info(f"=== Scrape run [{label}] complete ===")

def _print_snapshot(result: dict):
    s = result["summary"]
    print(
        f"\n  [{result['property']}] @ {datetime.fromisoformat(result['scraped_at']):%H:%M}\n"
        f"  Remaining rooms : {s['total_remaining']}\n"
        f"  Blended ADR     : ${s['blended_adr']}\n"
        f"  Occupancy est.  : {s['estimated_occupancy_pct']}%\n"
    )

# ─── Scheduler ───────────────────────────────────────────────────────────────
def start_scheduler(cfg: dict):
    scheduler = AsyncIOScheduler(timezone="America/Los_Angeles")

    schedule_cfg = cfg.get("schedule", DEFAULT_CONFIG["schedule"])
    for t in schedule_cfg.get("intraday_times", []):
        h, m = map(int, t.split(":"))
        scheduler.add_job(
            run_all_properties,
            CronTrigger(hour=h, minute=m),
            args=[cfg, f"scheduled-{t}"],
            id=f"scrape_{t.replace(':','')}"
        )
        logger.info(f"Scheduled intraday scrape at {t}")

    # Morning report
    rt = schedule_cfg.get("morning_report_time", "07:00")
    rh, rm = map(int, rt.split(":"))
    scheduler.add_job(
        generate_morning_report,
        CronTrigger(hour=rh, minute=rm),
        args=[date.today()],
        id="morning_report",
    )
    logger.info(f"Scheduled morning report at {rt}")

    scheduler.start()
    return scheduler

# ─── Entry point ─────────────────────────────────────────────────────────────
async def main():
    cfg = load_config()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "run":
            # Manual one-off run
            await run_all_properties(cfg, label="manual")
            return
        elif cmd == "report":
            # Generate yesterday's morning report right now
            generate_morning_report(date.today())
            return

    # Default: start scheduler + keep alive
    logger.info("Starting Hotel Inventory Tracker (scheduled mode) ...")
    scheduler = start_scheduler(cfg)

    # Run once immediately on startup
    await run_all_properties(cfg, label="startup")

    # Keep the event loop alive
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
