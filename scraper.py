import asyncio
import re
import json
import logging
import random
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

# Initialize directories dynamically at runtime
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"
]

async def wait_for_hotel_hydration(page, prop_name):
    """Ensures the property room tables and rates are fully loaded before parsing."""
    logger.info(f"[{prop_name}] Waiting for reservation canvas loaders to clear...")
    
    loaders = [".vres-prog-wrap", "#squaresWaveG", ".loading", "#loading", ".processing_id"]
    for loader in loaders:
        try:
            await page.wait_for_selector(loader, state="hidden", timeout=5000)
        except:
            pass

    anchors = [".vres_room_infoBg", ".vres_roomInfo", "tr.roomTypeRow", "div.room_type_title"]
    for anchor in anchors:
        try:
            await page.wait_for_selector(anchor, state="visible", timeout=12000)
            logger.info(f"[{prop_name}] Confirmed element canvas match: {anchor}")
            await asyncio.sleep(4)  # Let dynamic calculations completely settle
            return True
        except:
            continue

    await page.wait_for_load_state("networkidle", timeout=15000)
    await asyncio.sleep(5)
    return False

async def scrape_property_with_retry(prop, max_retries=3):
    """Executes the scrape request using DOM-based structural extraction."""
    for attempt in range(1, max_retries + 1):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": random.randint(1440, 1920), "height": random.randint(900, 1080)}
            )
            page = await context.new_page()
            
            try:
                logger.info(f"[{prop['name']}] Attempt {attempt}/{max_retries} -> Connecting...")
                await page.goto(prop["url"], timeout=45000, wait_until="commit")
                
                await wait_for_hotel_hydration(page, prop["name"])
                
                rooms = []
                seen_types = set()
                
                # Target the actual room wrapper blocks used by IPMS247
                room_elements = await page.locator(".vres_room_infoBg, .vres_roomInfo").all()
                
                # Fallback if specific wrappers aren't present
                if not room_elements:
                    room_elements = await page.locator("tr.roomTypeRow, div.room-type-container").all()
                
                for el in room_elements:
                    text_content = await el.inner_text()
                    if not text_content or "$" not in text_content:
                        continue
                    
                    lines = [line.strip() for line in text_content.split("\n") if line.strip()]
                    
                    # 1. Isolate the human-readable room type label
                    room_name = "Standard Room"
                    for line in lines:
                        cleaned = re.sub(r'[\\\/\"\'\>\<\=\_\-\;\:]', '', line).strip()
                        if "adult" in cleaned.lower() or "child" in cleaned.lower():
                            continue
                        if any(k in cleaned.lower() for k in ["king", "queen", "suite", "room", "studio", "deluxe", "accessible", "double"]):
                            if not any(x in cleaned.lower() for x in ["policy", "terms", "total", "details", "book", "condition", "tax"]):
                                room_name = cleaned
                                break
                    
                    # Clean trailing artifacts from room names
                    room_name = re.sub(r'(?i)(no pets|non-smoking|smoking|view details|room details|book now|avg/night).*', '', room_name)
                    room_name = re.sub(r'\s+', ' ', room_name).strip(' -,')
                    
                    if len(room_name) < 4 or room_name in seen_types:
                        continue
                    
                    # 2. Extract price
                    price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", text_content)
                    if not price_match:
                        continue
                    price = float(price_match.group(1).replace(",", ""))
                    
                    # 3. Handle availability metrics cleanly
                    left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", text_content, re.I)
                    if any(x in text_content.lower() for x in ["sold out", "not available", "unavailable", "fully booked"]):
                        rooms_left = 0
                    elif left_match:
                        val = next(g for g in left_match.groups() if g is not None)
                        rooms_left = int(val)
                    else:
                        # Fallback baseline when open but no warning count is listed
                        rooms_left = 2
                    
                    rooms.append({
                        "room_type": room_name,
                        "rate": price,
                        "rooms_left": rooms_left
                    })
                    seen_types.add(room_name)
                
                await browser.close()
                
                if rooms:
                    logger.info(f"[{prop['name']}] Successfully isolated {len(rooms)} room categories from DOM grid.")
                    return rooms
                else:
                    raise ValueError("No active room elements passed parsing constraints.")
                    
            except Exception as e:
                logger.warning(f"[{prop['name']}] Attempt {attempt} error: {str(e)}")
                await browser.close()
                if attempt == max_retries:
                    raise e
                await asyncio.sleep(6 * attempt)

def summarize(prop, rooms):
    total_rooms = prop["total"]
    total_remaining = sum(r["rooms_left"] for r in rooms)
    
    sold = max(total_rooms - total_remaining, 0)
    occ = int((sold / total_rooms) * 100) if total_rooms else 0

    rated = [r["rate"] for r in rooms if r["rate"] > 0]
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
    logger.info("Initializing inventory synchronization stream...")
    for prop in PROPERTIES:
        try:
            rooms = await scrape_property_with_retry(prop)
            if rooms:
                res = {
                    "property": prop["name"],
                    "url": prop["url"],
                    "scraped_at": datetime.utcnow().isoformat() + "Z",
                    "rooms": rooms,
                    "summary": summarize(prop, rooms)
                }
                save(res)
                logger.info(f"[{prop['name']}] Snapshot written successfully.")
            else:
                logger.error(f"[{prop['name']}] Skipped snapshot: No active rooms processed.")
        except Exception as e:
            logger.error(f"[{prop['name']}] Execution fault: {str(e)}")

if __name__ == "__main__":
    asyncio.run(run())
