import asyncio
import re
import json
import logging
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

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
    """Waits for core engine layout components to complete loading animations."""
    try:
        await page.wait_for_selector(".vres-prog-wrap, #squaresWaveG, .loading, #loading", state="hidden", timeout=15000)
    except:
        pass

    selectors = [
        ".vres_room_infoBg", 
        ".vres_roomInfo", 
        ".roomTypeRow", 
        "[id*='roomType']", 
        ".vres_main_container"
    ]
    
    hydrated = False
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, state="attached", timeout=8000)
            hydrated = True
            break
        except:
            continue
            
    if not hydrated:
        logger.warning("Primary selectors missing. Forcing network stream synchronization...")
        await page.wait_for_load_state("networkidle")

    await asyncio.sleep(5)

async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )

        page = await context.new_page()
        logger.info(f"[{prop['name']}] Loading reservation stream URL...")

        try:
            await page.goto(prop["url"], timeout=60000, wait_until="domcontentloaded")
            await wait_for_room_data(page)
            full_text = await page.locator("body").inner_text()
        except Exception as e:
            await browser.close()
            raise Exception(f"Network loading or navigation failure: {str(e)}")

        # Split document text based on common room classifications
        room_blocks = re.split(
            r'(?=(?:Deluxe|Comfort|Standard|Superior|Suite|King|Queen|Double|Twin|Studio|Single|Accessible)[^\n]{0,75}\n)', 
            full_text, 
            flags=re.I
        )

        logger.info(f"[{prop['name']}] Split stream into {len(room_blocks)} parsing partitions")

        rooms = []
        seen_types = set()

        for block in room_blocks:
            if not block.strip():
                continue

            # Parse Room Rate
            price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", block)
            price = float(price_match.group(1).replace(",", "")) if price_match else 0.0

            # Isolate Room Title Row and strip leftover HTML components/tags
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if not lines:
                continue
            
            raw_name = lines[0]
            for line in lines:
                if any(kw in line.lower() for kw in ["king", "queen", "suite", "room", "standard", "deluxe"]):
                    raw_name = line
                    break

            # Scrub operational elements and dirty characters/HTML string leakage
            name = re.sub(r'(?i)(no pets|non-smoking|non smoking|smoking|view details|room details|book now|avg/night|text|class=).*', '', raw_name)
            name = re.sub(r'[\\\/\"\'\>\<\=\_\-]', '', name)
            name = re.sub(r'\s+', ' ', name).strip(' -,')

            if len(name) < 4 or any(x in name.lower() for x in ["policy", "login", "terms", "total", "select", "template"]):
                continue

            if name in seen_types:
                continue

            # Parse Available Inventory Allocation
            left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", block, re.I)
            
            if any(x in block.lower() for x in ["sold out", "not available", "unavailable"]):
                rooms_left = 0
            elif left_match:
                val = next(g for g in left_match.groups() if g is not None)
                rooms_left = int(val)
            else:
                rooms_left = 1  # Fallback baseline room capacity assumption

            rooms.append({
                "room_type": name,
                "rate": price,
                "rooms_left": rooms_left
            })
            seen_types.add(name)

        await browser.close()

        if not rooms:
            raise Exception("No valid room profiles parsed from raw interface string layout")

        return {
            "property": prop["name"],
            "url": prop["url"],
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "rooms": rooms,
            "summary": summarize(prop, rooms)
        }

def summarize(prop, rooms):
    total_rooms = prop["total"]
    
    # Exclude sold out categories from ADR matching logic
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
    # Anchor files using local execution context dates
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
    logger.info("Initializing inventory crawler run sequence...")
    for prop in PROPERTIES:
        try:
            res = await scrape_property(prop)
            save(res)
            logger.info(f"[{prop['name']}] Run successful: Saved entry metrics ({len(res['rooms'])} types detected).")
        except Exception as e:
            logger.error(f"[{prop['name']}] Run failed: {str(e)}")

if __name__ == "__main__":
    import sys
    # Support both structural runner parameter patterns matching repository workflows
    if len(sys.argv) > 1 and sys.argv[1] in ["run", "scrape"]:
        asyncio.run(run())
    else:
        asyncio.run(run())
