"""
Hotel Inventory & Rate Tracker
Tracks same-day remaining inventory and rates for multiple properties via IPMS247 booking pages.
Runs automatically 5x/day + generates a 7AM next-day summary report.
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

# ─── Anti-bot helpers ─────────────────────────────────────────────────────────
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

async def human_delay(min_s=1.5, max_s=4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def build_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=random.choice(VIEWPORTS),
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "DNT": "1"},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
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
                await asyncio.
