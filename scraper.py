import asyncio
import re
import json
import logging
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

PROPERTIES = [
    {"name": "Tarzana Inn", "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn", "total": 49},
    {"name": "Sea Air Inn", "url": "https://book.ipms247.com/booking/book-rooms-seaairinn", "total": 24},
    {"name": "Blufftop Inn", "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict", "total": 32},
]

# ─────────────────────────────────────────────────────────────
# RESILIENT WAITING & HYDRATION CHECK
# ─────────────────────────────────────────────────────────────
async def wait_for_room_data(page):
    """Waits for core layout structures to hydrate and paint text nodes safely."""
    # Step 1: Wait for loading spinners to explicitly hide
    try:
        await page.wait_for_selector(".vres-prog-wrap, #squaresWaveG, .loading, #loading", state="hidden", timeout=12000)
    except:
        pass

    # Step 2: Wait for known structural content elements to bind to the page frame
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
            await page.wait_for_selector(sel, state="attached", timeout=5000)
            hydrated = True
            break
        except:
            continue
            
    if not hydrated:
        logger.warning("Target structural classes not found. Checking document stream fallback...")
        await page.wait_for_load_state("networkidle")

    # Give Javascript engine an extra cushion to securely map data properties onto text blocks
    await asyncio.sleep(4)


async def scrape_property(prop):
    async with async_playwright() as p:
        # Mask automation fingerprints completely
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )

        page = await context.new_page()
        logger.info(f"{prop['name']} → Loading page context")

        # Load document framework safely
        await page.goto(prop["url"], timeout=60000, wait_until="domcontentloaded")

        # Execute our validation hydration check
        await wait_for_room_data(page)

        # Grab the full text body of the main layout element container directly
        full_text = await page.locator("body").inner_text()

        # Isolate individual room variants by using a regex split lookahead 
        # Whenever a line contains room classifications (King, Queen, Suite, etc.) next to an expected price structure, we slice.
        room_blocks = re.split(
            r'(?=(?:Deluxe|Comfort|Standard|Superior|Suite|King|Queen|Double|Twin|Studio|Single|Accessible)[^\n]{0,50}\n)', 
            full_text, 
            flags=re.I
        )

        logger.info(f"{prop['name']} → Split page stream into {len(room_blocks)} potential block clusters")

        rooms = []
        seen_types = set()

        for block in room_blocks:
            if not block.strip():
                continue

            # 1. Parse Room Rate
            price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", block)
            if not price_match:
                continue
            price = float(price_match.group(1).replace(",", ""))

            # 2. Isolate Room Title Row
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if not lines:
                continue
            
            # Find the line that represents our clean room categorization
            name = lines[0]
            for line in lines:
                if any(kw in line.lower() for kw in ["king", "queen", "suite", "room", "standard", "deluxe", "twin"]):
                    name = line
                    break

            # Scrub structural filler components from the descriptor string
            name = re.sub(r'(?i)(no pets|non-smoking|non smoking|smoking|view details|room details|book now|avg/night)\s*[,\-]?\s*', '', name).strip(' -,')

            if len(name) < 4 or any(x in name.lower() for x in ["policy", "login", "terms", "total", "select"]):
                continue

            # Prevent duplication processing inside same target snapshot block
            if name in seen_types:
                continue

            # 3. Parse Allocation Inventory Left
            left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", block, re.I)
            
            rooms_left = 1  # Standard fallback configuration assuming 1 remains available if hidden
            if left_match:
                val = next(g for g in left_match.groups() if g is not None)
                rooms_left = int(val)
            elif any(x in block.lower() for x in ["sold out", "not available", "unavailable"]):
                rooms_left = 0

            rooms.append({
                "room_type": name,
                "rate": price,
                "rooms_left": rooms_left
            })
            seen_types.add(name)

        await browser.close()

        if not rooms:
            raise Exception("No clean room configurations extracted after stream split conversion")

        return {
            "property": prop["name"],
            "scraped_at": datetime.utcnow().isoformat(),
            "rooms": rooms,
            "summary": summarize(prop, rooms)
        }


def summarize(prop, rooms):
    total_rooms = prop["total"]
    total_remaining = sum(r["rooms_left"] for r in rooms)
    sold = max(total_rooms - total_remaining, 0)
    
    rated = [r["rate"] for r in rooms if r["rate"] > 0]
    adr = sum(rated) / len(rated) if rated else 0.0
    occ = int((sold / total_rooms) * 100) if total_rooms else 0

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

    data = json.load(open(file)) if file.exists() else {}
    prop = result["property"]
    if prop not in data:
        data[prop] = []

    data[prop].append(result)
    with open(file, "w") as f:
        json.dump(data, f, indent=2)


async def run():
    for prop in PROPERTIES:
        try:
            res = await scrape_property(prop)
            save(res)
            logger.info(f"{prop['name']} ✅ Gathered successfully ({len(res['rooms'])} types matched)")
        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        asyncio.run(run())
