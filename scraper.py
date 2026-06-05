import asyncio
import json
import re
import random
import logging
from datetime import datetime, date
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

PROPERTIES = [
    {"name": "Tarzana Inn", "url": "https://live.ipms247.com/booking/book-rooms-tarzanainn", "total": 49},
    {"name": "Sea Air Inn", "url": "https://live.ipms247.com/booking/book-rooms-seaairinn", "total": 24},
    {"name": "Blufftop Inn", "url": "https://book.ipms247.com/booking/book-rooms-blufftopinnsuiteswharfrestaurantdistrict", "total": 32},
]

# ─────────────────────────────────────────────
# WAIT FOR REAL DATA
# ─────────────────────────────────────────────
async def wait_for_rates(page):
    await page.wait_for_selector(".vres-prog-wrap", state="hidden", timeout=20000)
    await page.wait_for_function(
        "() => document.body.innerText.includes('$')",
        timeout=20000
    )
    await asyncio.sleep(2)

# ─────────────────────────────────────────────
# CLEAN PARSER
# ─────────────────────────────────────────────
async def extract_rooms(page):
    rooms = []

    nodes = page.locator("text=/\\$\\d+/")
    count = await nodes.count()

    for i in range(count):
        try:
            el = nodes.nth(i)
            txt = await el.inner_text()

            price = int(re.search(r"\$(\d+)", txt).group(1))

            parent = el.locator("xpath=ancestor::div[1]")
            block = await parent.inner_text()

            if len(block) < 10 or len(block) > 120:
                continue

            if any(x in block.lower() for x in ["please wait", "policy", "terms"]):
                continue

            if price < 50 or price > 500:
                continue

            name = re.sub(r"\$.*", "", block).strip()

            rooms.append({
                "room_type": name[:50],
                "rate": price,
                "available": True
            })

        except:
            continue

    return rooms

# ─────────────────────────────────────────────
# SUMMARY CALCULATION
# ─────────────────────────────────────────────
def summarize(prop, rooms):
    total_rooms = prop["total"]
    avg_rate = sum(r["rate"] for r in rooms) / len(rooms) if rooms else 0

    # assume 1 per type (IPMS limitation fallback)
    remaining = len(rooms)
    sold = total_rooms - remaining

    occ = int((sold / total_rooms) * 100) if total_rooms else 0

    return {
        "total_rooms_property": total_rooms,
        "total_remaining": remaining,
        "estimated_sold": sold,
        "estimated_occupancy_pct": occ,
        "blended_adr": round(avg_rate, 2) if avg_rate else None
    }

# ─────────────────────────────────────────────
# SCRAPE
# ─────────────────────────────────────────────
async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        page = await context.new_page()

        await page.goto(prop["url"])
        await wait_for_rates(page)

        rooms = await extract_rooms(page)

        if not rooms:
            raise Exception("No rooms parsed")

        summary = summarize(prop, rooms)

        await browser.close()

        return {
            "property": prop["name"],
            "scraped_at": datetime.utcnow().isoformat(),
            "rooms": rooms,
            "summary": summary
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
            logger.info(f"{prop['name']} ✅")

        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")

if __name__ == "__main__":
    asyncio.run(run())
