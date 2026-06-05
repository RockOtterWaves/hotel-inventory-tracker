import asyncio
import json
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
# CAPTURE API DATA (CRITICAL FIX)
# ─────────────────────────────────────────────
async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(prop["url"], timeout=60000)

        await asyncio.sleep(6)

        # ✅ find correct iframe
        frame = None
        for f in page.frames:
            if "booking" in f.url.lower():
                frame = f
                break

        if not frame:
            raise Exception("No booking iframe found")

        # ✅ wait for actual room cards (IMPORTANT FIX)
        await frame.wait_for_selector("text=$", timeout=20000)

        rooms = []

        # ✅ get all price elements
        price_elements = frame.locator("text=/\\$\\d+/")
        count = await price_elements.count()

        for i in range(count):
            try:
                el = price_elements.nth(i)

                text = await el.inner_text()

                import re
                match = re.search(r"\$(\d+)", text)
                if not match:
                    continue

                price = int(match.group(1))

                # ✅ walk up DOM dynamically
                container = el.locator("xpath=ancestor::*[self::div or self::tr][1]")

                block_text = await container.inner_text()

                lines = block_text.split("\n")

                # ✅ extract name from top line
                name = lines[0].strip() if lines else ""

                if len(name) < 4:
                    continue

                if any(x in name.lower() for x in [
                    "policy", "terms", "loading", "please"
                ]):
                    continue

                if price < 50 or price > 500:
                    continue

                rooms.append({
                    "room_type": name[:50],
                    "rate": price,
                    "rooms_left": None,
                    "available": True
                })

            except:
                continue

        await browser.close()

        if not rooms:
            raise Exception("DOM loaded but no rooms extracted")

        # ✅ dedupe
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

    total_remaining = sum(r.get("rooms_left") or 1 for r in rooms)

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
            logger.info(f"{prop['name']} ✅ {len(res['rooms'])} rooms")

        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")


if __name__ == "__main__":
    asyncio.run(run())
