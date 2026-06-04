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
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

async def human_delay(min_s=1.0, max_s=3.5):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def build_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-infobars",
            "--window-position=0,0"
        ],
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=random.choice(VIEWPORTS),
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "DNT": "1"},
    )
    return browser, context

# ─── Intelligent Network Interceptor ──────────────────────────────────────────
async def intercept_network_resources(route):
    """Filters unnecessary tracker metrics and layout assets to save bandwidth and blend signature footprints."""
    ignored_resources = ["image", "font", "media"]
    ignored_domains = [
        "google-analytics.com", "facebook.net", "doubleclick.net", 
        "hotjar.com", "mixpanel.com", "segment.io"
    ]
    url = route.request.url.lower()
    if route.request.resource_type in ignored_resources or any(domain in url for domain in ignored_domains):
        await route.abort()
    else:
        await route.continue_()

# ─── Humanized Interactive Mechanics ──────────────────────────────────────────
async def human_click(page, selector: str, timeout_ms: int = 2000) -> bool:
    """Scrolls smoothly, pauses behaviorally, hovers, and fires structural mouse clicks."""
    try:
        el = page.locator(selector).first
        if await el.count() > 0:
            await el.scroll_into_view_if_needed(timeout=timeout_ms)
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await el.hover(timeout=timeout_ms)
            await asyncio.sleep(random.uniform(0.1, 0.4))
            await el.click(timeout=timeout_ms)
            return True
    except Exception:
        pass
    return False

async def human_type(page, selector: str, value: str, timeout_ms: int = 2000) -> bool:
    """Simulates realistic timing cadences across typing characters."""
    try:
        el = page.locator(selector).first
        if await el.count() > 0:
            await el.scroll_into_view_if_needed(timeout=timeout_ms)
            await el.click(click_count=3, timeout=timeout_ms)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            for char in value:
                await page.keyboard.type(char)
                await asyncio.sleep(random.uniform(0.04, 0.18))
            await page.keyboard.press("Tab")
            return True
    except Exception:
        pass
    return False

# ─── Date validation ──────────────────────────────────────────────────────────
def is_date_valid(page_val: str, target: date) -> bool:
    if not page_val:
        return True
    for fmt in ["%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(page_val.strip(), fmt).date() == target
        except ValueError:
            pass
    return True

# ─── Core scraper execution ───────────────────────────────────────────────────
async def scrape_property(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    s = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    for attempt in range(1, s["retry_attempts"] + 1):
        logger.info(f"[{prop['name']}] Attempt {attempt}/{s['retry_attempts']}")
        try:
            result = await _scrape(prop, cfg, target)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[{prop['name']}] Attempt {attempt} met error exceptions: {e}")
            if attempt < s["retry_attempts"]:
                await asyncio.sleep(s["retry_delay_sec"] * attempt)
    logger.error(f"[{prop['name']}] All runtime attempts exhausted.")
    return None

async def _scrape(prop: dict, cfg: dict, target: date) -> Optional[dict]:
    name     = prop["name"]
    url      = prop["url"]
    s        = cfg.get("scraper", DEFAULT_CONFIG["scraper"])
    checkin  = target.strftime("%m-%d-%Y")
    checkout = (target + timedelta(days=1)).strftime("%m-%d-%Y")

    async with async_playwright() as p:
        browser, context = await build_context(p)
        page = await context.new_page()
        
        # Apply anti-fingerprint camouflage mapping natively
        await stealth_async(page)
        
        # Implement routing intercept optimization
        await page.route("**/*", intercept_network_resources)
        page.set_default_timeout(s["page_timeout_ms"])

        try:
            logger.info(f"[{name}] Opening connection window to target route...")
            await page.goto(url, wait_until="domcontentloaded", timeout=s["page_timeout_ms"])
            
            # Allow script layer to evaluate background data loads
            await asyncio.sleep(2) 
            page_checkin = await _read_date_field(page, "checkin")
            
            if not page_checkin or not is_date_valid(page_checkin, target):
                logger.info(f"[{name}] Form synchronization adjustment required. Running manual form entry workflow...")
                await _set_date_field(page, "checkin", checkin, name)
                await human_delay(0.4, 0.9)

                await _set_date_field(page, "checkout", checkout, name)
                await human_delay(0.4, 0.9)

                clicked = await _click_availability(page, name)
                if not clicked:
                    logger.warning(f"[{name}] Custom submission control missed execution target.")
            else:
                logger.info(f"[{name}] Default layout parameters matches target parameter range ({page_checkin or 'Today'}). Skipping state execution shifts.")

            # ── Wait for room block populated arrays ───────────────────────
            logger.info(f"[{name}] Intercepting DOM mutations for structural data tables...")
            try:
                await page.wait_for_function(
                    """() => {
                        const targets = document.querySelectorAll('.vres_room_infoBg, .vres_roomInfo, [class*="vres_room"]');
                        for (const node of targets) {
                            const payload = node.innerText || '';
                            if (payload.length > 50 && (payload.includes('$') || payload.includes('Room') || payload.includes('Sold Out') || payload.includes('Available'))) {
                                return true;
                            }
                        }
                        return false;
                    }""",
                    timeout=20000
                )
                logger.info(f"[{name}] DOM observation target metrics satisfied.")
            except PWTimeout:
                logger.warning(f"[{name}] Structural threshold wait state exceeded limits. Calling failover buffer window...")
                await human_delay(5, 7)

            # Verification of date target limits
            page_checkin = await _read_date_field(page, "checkin")
            if page_checkin and not is_date_valid(page_checkin, target):
                logger.warning(f"[{name}] Data schema rollover exception detected ('{page_checkin}'). Execution dropped.")
                return None

            html  = await page.content()
