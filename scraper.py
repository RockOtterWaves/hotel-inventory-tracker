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

# ✅ NEW: WAIT FOR REAL PRICE DATA (ACTUAL FIX)
async def wait_for_room_data(page):
    try:
        # wait for at least one visible price like $169
        await page.wait_for_selector("text=/\\$\\d+/", timeout=30000)
    except:
        raise Exception("No price elements detected (data never loaded)")

    # give UI time to fully stabilize
    await asyncio.sleep(4)


async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )

        page = await context.new_page()

        logger.info(f"{prop['name']} → Loading page")

        await page.goto(prop["url"], timeout=60000)

        # ✅ critical fix
        await wait_for_room_data(page)

        rooms = []

        price_elements = page.locator("text=/\\$\\d+/")
        count = await price_elements.count()

        for i in range(count):
            try:
                el = price_elements.nth(i)
                txt = await el.inner_text()

                price_match = re.search(r"\$([\d\.]+)", txt)
                if not price_match:
                    continue

                price = float(price_match.group(1))

                # ✅ walk up to container
                container = el.locator("xpath=ancestor::*[self::div][1]")
                block = await container.inner_text()

                lines = [l.strip() for l in block.split("\n") if l.strip()]

                if not lines:
                    continue

                name = lines[0]

                if len(name) < 4:
                    continue

                if any(x in name.lower() for x in ["policy", "login", "terms"]):
                    continue

                # ✅ extract rooms left
                left_match = re.search(r"(\d+)\s+Room", block)
                rooms_left = int(left_match.group(1)) if left_match else 0

                rooms.append({
                    "room_type": name,
                    "rate": price,
                    "rooms_left": rooms_left
                })

            except:
                continue

        await browser.close()

        if not rooms:
            raise Exception("Rooms not detected AFTER real price load")

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


async def run():
    for prop in PROPERTIES:
        try:
            res = await scrape_property(prop)
            save(res)
            logger.info(f"{prop['name']} ✅ {len(res['rooms'])} room types")

        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        asyncio.run(run())
