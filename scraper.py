import asyncio
import re
import json
import logging
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

# Create operational directories at script initialization
DATA_DIR = Path("data")
REPORTS_DIR = Path("reports")
LOGS_DIR = Path("logs")

DATA_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger()

PROPERTIES = [
    {"name": "Tarzana Inn", "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn", "total": 49},
    {"name": "Sea Air Inn", "url": "https://book.ipms247.com/booking/book-rooms-seaairinn", "total": 24},
    {"name": "Blufftop Inn", "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict", "total": 32},
]

async def wait_for_room_data(page):
    """Waits for reservation engines to successfully paint data frames."""
    try:
        await page.wait_for_selector(".vres-prog-wrap, #squaresWaveG, .loading, #loading", state="hidden", timeout=15000)
    except:
        pass

    selectors = [".vres_room_infoBg", ".vres_roomInfo", ".roomTypeRow", "[id*='roomType']"]
    hydrated = False
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, state="attached", timeout=8000)
            hydrated = True
            break
        except:
            continue
            
    if not hydrated:
        await page.wait_for_load_state("networkidle")
    await asyncio.sleep(4)

async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()
        logger.info(f"[{prop['name']}] Loading tracking reservation timeline stream...")

        try:
            await page.goto(prop["url"], timeout=60000, wait_until="domcontentloaded")
            await wait_for_room_data(page)
            full_text = await page.locator("body").inner_text()
        except Exception as e:
            await browser.close()
            raise Exception(f"Network viewport sync failure: {str(e)}")

        # Isolate text blocks based on pricing markers ($) to bypass raw img string leaks
        room_blocks = re.split(r'(?=\$\s*[\d,]+)', full_text)
        logger.info(f"[{prop['name']}] Splitting stream layout into {len(room_blocks)} data zones.")

        rooms = []
        seen_types = set()

        for block in room_blocks:
            if not block.strip():
                continue

            # Identify target pricing structures
            price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", block)
            if not price_match:
                continue
            price = float(price_match.group(1).replace(",", ""))

            # Trace backwards up the block lines to isolate actual descriptive names
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            name = "Standard Room"
            for line in lines:
                cleaned_line = re.sub(r'[\\\/\"\'\>\<\=\_\-]', '', line).strip()
                if any(kw in cleaned_line.lower() for kw in ["king", "queen", "suite", "room", "standard", "deluxe", "accessible"]):
                    if not any(x in cleaned_line.lower() for x in ["policy", "terms", "total", "details", "book"]):
                        name = cleaned_line
                        break

            # Scrub dirty trailing characters or markup noise
            name = re.sub(r'(?i)(no pets|non-smoking|smoking|view details|room details|book now|avg/night).*', '', name)
            name = re.sub(r'\s+', ' ', name).strip(' -,')

            if len(name) < 4 or name in seen_types:
                continue

            # Calculate remaining operational inventory allocation balances
            left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", block, re.I)
            if any(x in block.lower() for x in ["sold out", "not available", "unavailable"]):
                rooms_left = 0
            elif left_match:
                val = next(g for g in left_match.groups() if g is not None)
                rooms_left = int(val)
            else:
                rooms_left = 2  # Standard asset room threshold availability baseline

            rooms.append({
                "room_type": name,
                "rate": price,
                "rooms_left": rooms_left
            })
            seen_types.add(name)

        await browser.close()
        if not rooms:
            raise Exception("Zero clean room variations mapped out from data parsing strings.")

        return {
            "property": prop["name"],
            "url": prop["url"],
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "rooms": rooms,
            "summary": summarize(prop, rooms)
        }

def summarize(prop, rooms):
    total_rooms = prop["total"]
    available_rooms = [r for r in rooms if r["rooms_left"] > 0]
    total_remaining = sum(r["rooms_left"] for r in available_rooms)
    
    sold = max(total_rooms - total_remaining, 0)
    occ = int((sold / total_rooms) * 100) if total_rooms else 0

    rated = [r["rate"] for r in available_rooms if r["rate"] > 0]
    adr = sum(rated) / len(rated) if rated else 0.0

    return {
        "total_rooms_property": total_rooms,
        "total_remaining": total_remaining,
        "estimated_sold": sold,
        "estimated_occupancy_pct": occ,
        "blended_adr": round(adr, 2)
    }

def save(result):
    today = date.today().isoformat()
    file = DATA_DIR / f"{today}.json"

    data = {}
    if file.exists():
        try:
            with open(file, "r") as f:
                data = json.load(f)
        except:
            data = {}

    prop = result["property"]
    if prop not in data:
        data[prop] = []

    data[prop].append(result)
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

async def run():
    logger.info("Initializing inventory background sync automation...")
    for prop in PROPERTIES:
        try:
            res = await scrape_property(prop)
            save(res)
            logger.info(f"[{prop['name']}] Snapshot compiled successfully.")
        except Exception as e:
            logger.error(f"[{prop['name']}] Tracker run fault: {str(e)}")

if __name__ == "__main__":
    asyncio.run(run())
