import asyncio
import re
import json
import logging
from datetime import datetime, date
from pathlib import Path

from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

PROPERTIES = [
    {"name": "Tarzana Inn", "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn", "total": 49},
    {"name": "Sea Air Inn", "url": "https://book.ipms247.com/booking/book-rooms-seaairinn", "total": 24},
    {"name": "Blufftop Inn", "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict", "total": 32},
]

# ─────────────────────────────────────────────
# CRITICAL: WAIT FOR REAL DATA
# ─────────────────────────────────────────────
async def wait_for_real_data(page):
    try:
        # wait until loading appears
        await page.wait_for_selector("text=Please wait", timeout=10000)
    except:
        pass

    # wait until loading disappears
    await page.wait_for_function(
        """() => !document.body.innerText.includes("Please wait")""",
        timeout=30000
    )

    await asyncio.sleep(3)


# ─────────────────────────────────────────────
# SCRAPER (FINAL WORKING VERSION)
# ─────────────────────────────────────────────
async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )

        page = await context.new_page()

        logger.info(f"{prop['name']} → Loading page")

        await page.goto(prop["url"], timeout=60000)

        # ✅ CRITICAL FIX
        await wait_for_real_data(page)

        # ✅ Get FINAL visible content
        content = await page.content()

        # DEBUG SAVE (optional but useful)
        debug_file = Path(f"debug_{prop['name'].replace(' ', '_')}.html")
        debug_file.write_text(content)

        rooms = []

        # ─────────────────────────────────────────────
        # INTELLIGENT PARSING (REAL DATA EXTRACTION)
        # ─────────────────────────────────────────────

        # Match blocks like:
        # Deluxe King
        # $169.00
        # 2 Rooms Left
        pattern = re.compile(
            r"([A-Za-z0-9 \-\(\)\/]+)\s*\$([\d\.]+).*?(\d+)\s+Room",
            re.DOTALL
        )

        matches = pattern.findall(content)

        for m in matches:
            name = m[0].strip()
            price = float(m[1])
            rooms_left = int(m[2])

            # clean junk
            if len(name) < 4:
                continue

            if any(x in name.lower() for x in [
                "policy", "terms", "login", "loading"
            ]):
                continue

            rooms.append({
                "room_type": name,
                "rate": price,
                "rooms_left": rooms_left
            })

        await browser.close()

        if not rooms:
            raise Exception("No rooms extracted AFTER full load")

        # dedupe
        unique = []
        seen = set()

        for r in rooms:
            key = (r["room_type"], r["rate"])
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return {
            "property": prop["name"],
            "scraped_at": datetime.utcnow().isoformat(),
            "rooms": unique,
            "summary": summarize(prop, unique)
        }


# ─────────────────────────────────────────────
# SUMMARY (REAL INVENTORY)
# ─────────────────────────────────────────────
def summarize(prop, rooms):
    total_rooms = prop["total"]

    total_remaining = sum(r["rooms_left"] for r in rooms)

    sold = max(total_rooms - total_remaining, 0)

    adr = sum(r["rate"] for r in rooms) / len(rooms)

    occ = int((sold / total_rooms) * 100) if total_rooms else 0

    return {
        "total_rooms_property": total_rooms,
        "total_remaining": total_remaining,
        "estimated_sold": sold,
        "estimated_occupancy_pct": occ,
        "blended_adr": round(adr, 2)
    }


# ─────────────────────────────────────────────
# SAVE
# ─────────────────────────────────────────────
def save(result):
    today = date.today().isoformat()
    file = DATA_DIR / f"{today}.json"

    if file.exists():
        data = json.load(open(file))
    else:
        data = {}

    prop = result["property"]

    if prop not in data:
        data[prop] = []

    data[prop].append(result)

    json.dump(data, open(file, "w"), indent=2)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def run():
    for prop in PROPERTIES:
        try:
            res = await scrape_property(prop)
            save(res)
            logger.info(f"{prop['name']} ✅ {len(res['rooms'])} room types")

        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")


# CLI ENTRY
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "run":
        asyncio.run(run())
        
