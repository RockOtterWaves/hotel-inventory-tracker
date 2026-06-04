"""
Hotel Inventory & Rate Tracker
Tracks same-day remaining inventory and rates for multiple properties via IPMS247 booking pages.
Runs automatically 5x/day + generates a 7AM next-day summary report.
Features: Playwright Stealth, Network intercept filtering, and Humanized action rhythms.
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
from playwright_stealth import stealth_async
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
    },
    "ai_fallback": {
        "enabled": False,
        "api_key": ""
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

# ─── Anti-bot profiles ────────────────────────────────────────────────────────
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
            await asyncio.sleep(0.3)
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
            await page.keyboard.type(value, delay=random.uniform(50, 150))
            await page.keyboard.press("Tab")
            return True
    except Exception:
        pass
    return False

def is_date_valid(page_val: str, target: date) -> bool:
    if not page_val: return True
    for fmt in ["%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(page_val.strip(), fmt).date() == target
        except ValueError: pass
    return True

async def scrape_property(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    for attempt in range(1, s["retry_attempts"] + 1):
        logger.info(f"[{prop['name']}] Attempt {attempt}/{s['retry_attempts']}")
        try:
            result = await _scrape(prop, cfg, target)
            if result: return result
        except Exception as e:
            logger.warning(f"[{prop['name']}] Error: {e}")
            await asyncio.sleep(s["retry_delay_sec"])
    return None

async def _scrape(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    name, url = prop["name"], prop["url"]
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    checkin = target.strftime("%m-%d-%Y")
    checkout = (target + timedelta(days=1)).strftime("%m-%d-%Y")

    async with async_playwright() as p:
        browser, context = await build_context(p)
        page = await context.new_page()
        await stealth_async(page)
        await page.route("**/*", intercept_network_resources)
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s["page_timeout_ms"])
            await asyncio.sleep(2)
            
            page_checkin = await _read_date_field(page, "checkin")
            if not page_checkin or not is_date_valid(page_checkin, target):
                await _set_date_field(page, "checkin", checkin, name)
                await _set_date_field(page, "checkout", checkout, name)
                await _click_availability(page, name)

            # Wait for data to populate
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('$') || document.body.innerText.includes('Sold Out')",
                    timeout=20000
                )
            except PWTimeout:
                await asyncio.sleep(5)

            html = await page.content()
            rooms = _parse_rooms_ipms247(html, name)
            
            if not rooms: return None

            summary = _summarise(rooms, prop["total_rooms"])
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
        if await human_click(page, sel): return True
    return False

async def _read_date_field(page, field_type: str) -> str:
    try:
        el = page.locator(f"input[id*='{field_type}']").first
        return await el.input_value(timeout=1000) if await el.count() > 0 else ""
    except: return ""

def _parse_rooms_ipms247(html: str, hotel_name: str) -> list:
    rooms = []
    blocks = re.split(r'(?=<(?:div|tr)[^>]+class="[^"]*vres_room_info[^"]*")', html)[1:]
    if not blocks: blocks = [html]

    for block in blocks:
        name_match = re.search(r'class="[^"]*vres_roomName[^"]*">([^<]+)', block)
        if not name_match: continue
        
        name = name_match.group(1).strip()
        rate_match = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', block)
        rate = float(rate_match.group(1).replace(',', '')) if rate_match else None
        
        rooms.append({
            "room_type": name,
            "available": "Sold Out" not in block,
            "rate": rate,
            "rooms_left": None 
        })
    return rooms

def _summarise(rooms: list, total_rooms: int) -> dict:
    avail = [r for r in rooms if r["available"] and r["rate"]]
    rem = len(avail)
    adr = round(sum(r["rate"] for r in avail) / rem, 2) if rem > 0 else 0
    occ = round(((total_rooms - rem) / total_rooms) * 100, 1) if total_rooms else 0
    return {
        "total_rooms_property": total_rooms, "total_remaining": rem,
        "estimated_sold": total_rooms - rem, "estimated_occupancy_pct": occ,
        "blended_adr": adr
    }

def load_day_data(d: date) -> dict:
    path = DATA_DIR / f"{d:%Y-%m-%d}.json"
    return json.load(open(path)) if path.exists() else {}

def save_day_data(d: date, data: dict):
    json.dump(data, open(DATA_DIR / f"{d:%Y-%m-%d}.json", "w"), indent=2, default=str)

def append_snapshot(d: date, result: dict):
    data = load_day_data(d)
    data.setdefault(result["property"], []).append(result)
    save_day_data(d, data)

async def run_all_properties(cfg: dict, label: str = "manual"):
    target = date.today()
    for prop in cfg.get("properties", []):
        res = await scrape_property(prop, cfg, target)
        if res: append_snapshot(target, res)

def generate_morning_report(report_date: date):
    prev = report_date - timedelta(days=1)
    data = load_day_data(prev)
    if not data: return
    print(f"Report for {prev}") # Simplified for output

def start_scheduler(cfg: dict):
    scheduler = AsyncIOScheduler(timezone="America/Los_Angeles")
    for t in cfg["schedule"]["intraday_times"]:
        h, m = map(int, t.split(":"))
        scheduler.add_job(run_all_properties, CronTrigger(hour=h, minute=m), args=[cfg, t])
    scheduler.start()
    return scheduler

async def main():
    cfg = load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        await run_all_properties(cfg, "manual")
    else:
        start_scheduler(cfg)
        await run_all_properties(cfg, "startup")
        while True: await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
