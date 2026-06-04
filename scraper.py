"""
Hotel Inventory & Rate Tracker
Tracks same-day remaining inventory and rates for multiple properties via IPMS247 booking pages.
Runs automatically 5x/day + generates a 7AM next-day summary report.
Optimized for explicit remaining room countdown calculations.
"""

import asyncio
import json
import re
import sys
import random
import logging
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
        "page_timeout_ms": 45000,
        "retry_attempts": 3,
        "retry_delay_sec": 15
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

# ─── Anti-bot helpers ─────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
VIEWPORTS = [{"width": 1920, "height": 1080}, {"width": 1440, "height": 900}]

async def human_delay(min_s=1.0, max_s=3.5):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def build_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-gpu"]
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=random.choice(VIEWPORTS),
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "window.chrome = {runtime: {}};"
    )
    return browser, context

async def intercept_network_resources(route):
    ignored_resources = ["image", "font", "media"]
    if route.request.resource_type in ignored_resources:
        await route.abort()
    else:
        await route.continue_()

async def human_click(page, selector: str, timeout_ms: int = 2000) -> bool:
    try:
        el = page.locator(selector).first
        if await el.count() > 0:
            await el.scroll_into_view_if_needed(timeout=timeout_ms)
            await asyncio.sleep(random.uniform(0.2, 0.4))
            await el.click(timeout=timeout_ms)
            return True
    except Exception:
        pass
    return False

async def human_type(page, selector: str, value: str, timeout_ms: int = 2000) -> bool:
    try:
        el = page.locator(selector).first
        if await el.count() > 0:
            await el.scroll_into_view_if_needed(timeout=timeout_ms)
            await el.click(click_count=3, timeout=timeout_ms)
            await page.keyboard.type(value, delay=random.uniform(40, 100))
            await page.keyboard.press("Tab")
            return True
    except Exception:
        pass
    return False

def is_date_valid(page_val: str, target: date) -> bool:
    if not page_val: 
        return True
    for fmt in ["%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(page_val.strip(), fmt).date() == target
        except ValueError: 
            pass
    return True

# ─── Core Scraper Logic ───────────────────────────────────────────────────────
async def scrape_property(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    for attempt in range(1, s["retry_attempts"] + 1):
        logger.info(f"[{prop['name']}] Attempt {attempt}/{s['retry_attempts']}")
        try:
            result = await _scrape(prop, cfg, target)
            if result: 
                return result
        except Exception as e:
            logger.warning(f"[{prop['name']}] Run exception met: {e}")
            await asyncio.sleep(s["retry_delay_sec"])
    logger.error(f"[{prop['name']}] Execution loops depleted.")
    return None

async def _scrape(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    name, url = prop["name"], prop["url"]
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    checkin = target.strftime("%m-%d-%Y")
    checkout = (target + timedelta(days=1)).strftime("%m-%d-%Y")

    async with async_playwright() as p:
        browser, context = await build_context(p)
        page = await context.new_page()
        await page.route("**/*", intercept_network_resources)
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s["page_timeout_ms"])
            await asyncio.sleep(2)
            
            page_checkin = await _read_date_field(page, "checkin")
            if not page_checkin or not is_date_valid(page_checkin, target):
                logger.info(f"[{name}] Form inputs out of range. Updating check-in rules manually...")
                await _set_date_field(page, "checkin", checkin, name)
                await _set_date_field(page, "checkout", checkout, name)
                await _click_availability(page, name)
            else:
                logger.info(f"[{name}] Verification complete ({page_checkin}). Bypassing redundant entries.")

            logger.info(f"[{name}] Awaiting active grid render targets...")
            try:
                await page.wait_for_function(
                    """() => {
                        const body = document.body.innerText || '';
                        return body.includes('$') || body.includes('Sold Out') || body.includes('Available');
                    }""",
                    timeout=20000
                )
            except PWTimeout:
                logger.warning(f"[{name}] Sync timing exceeded threshold limits. Forcing buffer delay...")
                await asyncio.sleep(5)

            html = await page.content()
            rooms = _parse_rooms_ipms247(html, name)
            
            if not rooms: 
                return None

            summary = _summarise(rooms, prop["total_rooms"])
            logger.info(f"[{name}] ✓ Verification Complete: {len(rooms)} structures mapped. Remaining={summary['total_remaining']} | Occ={summary['estimated_occupancy_pct']}%")
            
            return {
                "property": name, "url": url, "total_rooms": prop["total_rooms"],
                "scraped_at": datetime.now().isoformat(), "target_date": target.isoformat(),
                "rooms": rooms, "summary": summary
            }

        finally:
            await browser.close()

async def _set_date_field(page, field_type: str, value: str, name: str):
    selectors = [f"input[id*='{field_type}']", f"input[name*='{field_type}']"]
    for sel in selectors:
        if await human_type(page, sel, value): return

async def _click_availability(page, name: str) -> bool:
    selectors = ["button:has-text('Check Availability')", "input[value*='Check Availability']"]
    for sel in selectors:
        if await human_click(page, sel): 
            await human_delay(2.0, 4.0)
            return True
    return False

async def _read_date_field(page, field_type: str) -> str:
    selectors = [f"input[id*='{field_type}']", f"input[name*='{field_type}']"]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                return await el.input_value(timeout=1000)
        except Exception: 
            continue
    return ""

# ─── Robust HTML Data Separation Engine ───────────────────────────────────────
def _parse_rooms_ipms247(html: str, hotel_name: str) -> list:
    rooms = []
    seen = set()
    
    blocks = re.split(r'(?=<(?:div|tr)[^>]+class="[^"]*(?:vres_room_info|roomTypeRow|vres_roomInfo)[^"]*")', html)[1:]
    if not blocks: 
        blocks = [html]

    for block in blocks:
        clean_block = block
        if "vres_footer" in clean_block:
            clean_block = clean_block.split("vres_footer")[0]
        if "booking-footer" in clean_block:
            clean_block = clean_block.split("booking-footer")[0]

        name_match = re.search(r'class="[^"]*(?:vres_roomName|roomTypeName)[^"]*"[^>]*>\s*([^<]+)', clean_block, re.I)
        if not name_match:
            name_match = re.search(r'(?:Deluxe|Comfort|Standard|Superior|Suite|King|Queen|Double)[^\n<]{2,40}', clean_block, re.I)
            
        if not name_match: 
            continue
        
        room_name = name_match.group(1).strip() if name_match.lastindex else name_match.group(0).strip()
        room_name = re.sub(r'<[^>]+>', '', room_name).strip()
        room_name = re.sub(r'\s+', ' ', room_name)
        
        if len(room_name) < 3 or room_name in seen: 
            continue
        seen.add(room_name)

        is_sold_out = False
        status_area_match = re.search(r'(?:<button|<span|<div)[^>]*class="[^"]*(?:status|book|avail)[^"]*"[^>]*>([\s\S]*?)(?:</button|</span>|</div>)', clean_block, re.I)
        if status_area_match and any(x in status_area_match.group(1).lower() for x in ["sold out", "not available", "unavailable"]):
            is_sold_out = True
        elif status_area_match is None and any(x in clean_block.lower() for x in ["sold out", "no rooms available"]):
            is_sold_out = True

        if is_sold_out:
            rooms.append({"room_type": room_name, "available": False, "rooms_left": 0, "rate": None})
            continue

        rate = None
        rate_match = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', clean_block)
        if rate_match:
            try:
                val = float(rate_match.group(1).replace(',', ''))
                if 30 < val < 1500: rate = val
            except: pass

        rooms_left = None
        # Target specific expressions such as "Only 3 left" or "2 rooms remaining"
        inv_match = re.search(r'(\d+)\s*(?:room[s]?\s*left|left|remaining)', clean_block, re.I)
        if inv_match:
            try: 
                rooms_left = int(inv_match.group(1))
            except: pass

        rooms.append({
            "room_type": room_name,
            "available": True,
            "rate": rate,
            "rooms_left": rooms_left
        })
        
    return rooms

def _summarise(rooms: list, total_rooms: int) -> dict:
    avail = [r for r in rooms if r["available"]]
    rated = [r for r in avail if r["rate"] is not None]
    
    # Track if the page explicitly explicitly listed individual room countdown metrics
    has_explicit_counts = any(r["rooms_left"] is not None for r in avail)
    
    if has_explicit_counts:
        # Sum only room blocks where countdown labels are found
        total_remaining = sum(r["rooms_left"] for r in avail if r["rooms_left"] is not None)
        # If there are open room blocks but they don't have text countdown labels, 
        # it means their inventory is healthy (more than the urgency threshold, usually > 5 rooms).
        # We will handle this gracefully, but if your systems explicitly show remaining totals, this sums them cleanly.
        if total_remaining == 0 and len(avail) > 0:
            total_remaining = len(avail)
