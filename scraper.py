import asyncio
import re
import json
import logging
import random
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

# Data Directory Structure Verification
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

async def wait_for_hotel_hydration(page, prop_name):
    """Monitors asynchronous loaders to ensure pricing grid canvas elements are painted."""
    logger.info(f"[{prop_name}] Waiting for reservation canvas loaders to clear...")
    
    # Core spinner arrays used by IPMS frameworks
    loaders = [".vres-prog-wrap", "#squaresWaveG", ".loading", "#loading", ".processing_id"]
    for loader in loaders:
        try:
            await page.wait_for_selector(loader, state="hidden", timeout=4000)
        except:
            pass

    # Wait until any common pricing character ($) or row block is painted to the visible layout frame
    try:
        await page.wait_for_selector("text=$", timeout=15000)
        await asyncio.sleep(4)  # Let the calculation engines stabilize room counters completely
        return True
    except Exception as e:
        logger.warning(f"[{prop_name}] Timeout waiting for pricing strings: {str(e)}")
        
    await page.wait_for_load_state("networkidle", timeout=10000)
    return False

async def scrape_property_with_retry(prop, max_retries=3):
    """Executes room parsing loops, independently identifying inventory slices row-by-row."""
    for attempt in range(1, max_retries + 1):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1920, "height": 1080}
            )
            page = await context.new_page()
            
            try:
                logger.info(f"[{prop['name']}] Attempt {attempt}/{max_retries} -> Connecting...")
                await page.goto(prop["url"], timeout=45000, wait_until="domcontentloaded")
                
                await wait_for_hotel_hydration(page, prop["name"])
                
                # Dynamic Broad Selector extraction strategy
                # Looks at any structural block containing a dollar sign to prevent template container mismatches
                candidate_locators = [
                    ".vres_room_infoBg", ".vres_roomInfo", "tr.roomTypeRow", 
                    "div[class*='room']", "div[id*='room']", ".booking-room"
                ]
                
                blocks = []
                for selector in candidate_locators:
                    found_elements = await page.locator(selector).all()
                    if found_elements:
                        # Inspect the found nodes to verify they actually contain room text metadata
                        for el in found_elements:
                            txt = await el.inner_text()
                            if txt and "$" in txt:
                                blocks.append(txt)
                        if len(blocks) > 0:
                            logger.info(f"[{prop['name']}] Isolated structural blocks matching selector: '{selector}'")
                            break
                
                # Universal Body Text Fallback if specialized components are missing or compressed
                if not blocks:
                    body_text = await page.locator("body").inner_text()
                    # Check for sold out specifically
                    if any(x in body_text.lower() for x in ["sold out", "no rooms", "not available"]):
                        # Create a dummy "sold out" entry so aggregate.py knows the state
                        return [{"room_type": "Sold Out", "rate": 0, "rooms_left": 0}]
                    blocks = re.split(r'(?=\$\s*\d)', body_text)
                rooms = parse_raw_blocks(blocks, prop["name"])
                await browser.close()
                
                if rooms:
                    return rooms
                else:
                    raise ValueError("No valid rooms compiled from raw text sweeps.")
                    
            except Exception as e:
                logger.warning(f"[{prop['name']}] Attempt {attempt} failed: {str(e)}")
                await browser.close()
                if attempt == max_retries:
                    raise e
                await asyncio.sleep(5 * attempt)

def parse_raw_blocks(blocks, prop_name):
    """Processes array elements, extracting room configurations, accurate inventory rates, and counts."""
    rooms = []
    seen_types = set()

    for item in blocks:
        if not item.strip() or "$" not in item:
            continue

        # 1. Clean and Isolate Room Pricing
        price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", item)
        if not price_match:
            continue
        price = float(price_match.group(1).replace(",", ""))

        # 2. Extract Clean Room Type Labels
        lines = [line.strip() for line in item.split("\n") if line.strip()]
        room_name = "Standard Room"
        
        for line in lines:
            cleaned = re.sub(r'[\\\/\"\'\>\<\=\_\-\;\:]', '', line).strip()
            # Eliminate metadata configuration selectors
            if "adult" in cleaned.lower() or "child" in cleaned.lower() or "room" == cleaned.lower():
                continue
            if any(k in cleaned.lower() for k in ["king", "queen", "suite", "room", "studio", "deluxe", "accessible", "double", "standard"]):
                if not any(x in cleaned.lower() for x in ["policy", "terms", "total", "details", "book", "condition", "tax", "select"]):
                    room_name = cleaned
                    break

        # Tidy up extraneous template markers
        room_name = re.sub(r'(?i)(no pets|non-smoking|smoking|view details|room details|book now|avg/night).*', '', room_name)
        room_name = re.sub(r'\s+', ' ', room_name).strip(' -,')

        if len(room_name) < 4 or room_name in seen_types:
            continue

        # 3. Determine remaining inventory count accurately
        left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", item, re.I)
        
        if any(x in item.lower() for x in ["sold out", "not available", "unavailable", "fully booked"]):
            rooms_left = 0
        elif left_match:
            val = next(g for g in left_match.groups() if g is not None)
            rooms_left = int(val)
        else:
            # Standard fallback assumption for available categories when no explicit low-inventory flag is present
            rooms_left = 5 

        rooms.append({
            "room_type": room_name,
            "rate": price,
            "rooms_left": rooms_left
        })
        seen_types.add(room_name)

    logger.info(f"[{prop_name}] Extracted {len(rooms)} distinct room configurations.")
    return rooms

def summarize(prop, rooms):
    """Applies formula: Occupancy % = (Total Property Rooms - Remaining Rooms) / Total Property Rooms"""
    total_rooms = prop["total"]
    
    # Sum up all rooms remaining across all parsed configurations
    total_remaining = sum(r["rooms_left"] for r in rooms)
    
    # Prevent remaining keys from exceeding structural property ceilings due to generic defaults
    if total_remaining > total_rooms:
        total_remaining = total_rooms

    # Execute occupancy calculations
    sold = max(total_rooms - total_remaining, 0)
    occ_pct = int((sold / total_rooms) * 100) if total_rooms else 0

    # Handle ADR calculations cleanly across parsed components
    rates = [r["rate"] for r in rooms if r["rate"] > 0]
    adr = sum(rates) / len(rates) if rates else 0.0

    return {
        "total_rooms_property": total_rooms,
        "total_remaining": int(total_remaining),
        "estimated_sold": int(sold),
        "estimated_occupancy_pct": int(occ_pct),
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
                logger.info(f"[{prop['name']}] Snapshot successfully written to local JSON storage.")
            else:
                logger.error(f"[{prop['name']}] Run aborted: Missing operational room parameters.")
        except Exception as e:
            logger.error(f"[{prop['name']}] System Fault: {str(e)}")

if __name__ == "__main__":
    asyncio.run(run())
