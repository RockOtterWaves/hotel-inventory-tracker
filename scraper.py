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

        api_payloads = []

        # ✅ capture all network responses
        def handle_response(response):
            try:
                if "room" in response.url.lower():
                    api_payloads.append(response)
            except:
                pass

        page.on("response", handle_response)

        await page.goto(prop["url"], timeout=60000)

        await asyncio.sleep(8)  # give API time to fire

        rooms = []

        # ✅ parse API responses
        for resp in api_payloads:
            try:
                text = await resp.text()

                if "rate" not in text.lower():
                    continue

                data = json.loads(text)

                # Try structured extraction
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    rate = item.get("rate") or item.get("price")
                                    name = item.get("roomtype") or item.get("name")
                                    left = item.get("available") or item.get("roomsleft")

                                    if rate and name:
                                        rooms.append({
                                            "room_type": str(name),
                                            "rate": int(rate),
                                            "rooms_left": left,
                                            "available": True
                                        })

            except:
                continue

        await browser.close()

        if not rooms:
            raise Exception("No API room data found")

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
