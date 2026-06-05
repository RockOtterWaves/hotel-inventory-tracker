"""
Hotel Inventory & Rate Tracker (FIXED + ENHANCED)
"""

import asyncio
import json
import re
import sys
import random
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─────────────────────────────────────────────────────────────
# PATHS + LOGGING
# ─────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
LOG_DIR     = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"

for d in [DATA_DIR, LOG_DIR]:
    d.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"tracker_{date.today():%Y-%m-%d}.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "properties": [
        {
            "name": "Tarzana Inn",
            "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn",
            "total_rooms": 49
        }
    ],
    "scraper": {
        "retry_attempts": 3,
        "retry_delay_sec": 15,
        "timeout_ms": 45000
    }
}

def load_config():
    if CONFIG_FILE.exists():
        return json.load(open(CONFIG_FILE))
    json.dump(DEFAULT_CONFIG, open(CONFIG_FILE, "w"), indent=2)
    return DEFAULT_CONFIG

# ─────────────────────────────────────────────────────────────
# BROWSER SETUP
# ─────────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"
]

async def build_browser():
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1400, "height": 900},
        timezone_id="America/Los_Angeles"
    )
    return p, browser, context

# ─────────────────────────────────────────────────────────────
# CRITICAL FIX: WAIT FOR REAL DATA (NOT SHELL)
# ─────────────────────────────────────────────────────────────
async def wait_for_rates(page):
    logger.info("Waiting for IPMS247 data load...")

    # 1. Wait for spinner to disappear
    try:
        await page.wait_for_selector(".vres-prog-wrap", state="hidden", timeout=20000)
    except:
        logger.warning("Spinner did not disappear (continuing...)")

    # 2. Wait for actual $ prices
    try:
        await page.wait_for_function(
            "() => document.body.innerText.includes('$')",
            timeout=20000
        )
        logger.info("Detected pricing in DOM ✅")
    except:
        raise Exception("Rates never loaded")

    # 3. Small buffer for rendering stability
    await asyncio.sleep(2)

# ─────────────────────────────────────────────────────────────
# ROBUST ROOM PARSER
# ─────────────────────────────────────────────────────────────
def parse_rooms(html: str) -> List[Dict]:
    rooms = []

    # Match lines like: "King Room - $129"
    pattern = re.findall(r'([A-Za-z\s]+)\$?(\d+)', html)

    for name, price in pattern:
        name = name.strip()
        if len(name) < 3:
            continue

        rooms.append({
            "room_type": name,
            "rate": int(price),
            "available": True
        })

    return rooms

# ─────────────────────────────────────────────────────────────
# SCRAPER CORE
# ─────────────────────────────────────────────────────────────
async def scrape_property(prop, cfg):
    attempts = cfg["scraper"]["retry_attempts"]

    for attempt in range(1, attempts + 1):
        try:
            logger.info(f"[{prop['name']}] Attempt {attempt}/{attempts}")

            p, browser, context = await build_browser()
            page = await context.new_page()

            await page.goto(prop["url"], timeout=60000)

            # ✅ CRITICAL FIX HERE
            await wait_for_rates(page)

            html = await page.content()

            rooms = parse_rooms(html)

            # ✅ HARD FAIL if empty (forces retry)
            if not rooms:
                logger.warning("No rooms parsed — dumping HTML")
                with open("debug.html", "w") as f:
                    f.write(html)
                raise Exception("Parser returned empty")

            logger.info(f"✅ Parsed {len(rooms)} rooms")

            await browser.close()
            await p.stop()

            return {
                "property": prop["name"],
                "rooms": rooms,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.warning(f"[{prop['name']}] Failed: {str(e)}")

            if attempt < attempts:
                await asyncio.sleep(cfg["scraper"]["retry_delay_sec"])
            else:
                logger.error(f"[{prop['name']}] All attempts exhausted")

    return None

# ─────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────
async def run():
    cfg = load_config()

    for prop in cfg["properties"]:
        result = await scrape_property(prop, cfg)

        if result:
            print("\n✅ SUCCESS")
            print(result)
        else:
            print(f"\n❌ FAILED: {prop['name']}")

# ─────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run())
