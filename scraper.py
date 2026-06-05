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
    """Waits for core layout structures to hydrate and paints text nodes safely."""
    # Step 1: Wait for loading wheels/screens to explicitly hide
    try:
        await page.wait_for_selector(".vres-prog-wrap, #squaresWaveG, .loading", state="hidden", timeout=10000)
    except:
        pass

    # Step 2: Wait for known structural IPMS wrapper containers instead of regex text strings
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
            await page.wait_for_selector(sel, state="attached", timeout=4000)
            hydrated = True
            break
        except:
            continue
            
    if not hydrated:
        # Fallback: check if page body contains basic plain text text clues
        logger.warning("Target structural classes not found. Checking document stream fallback...")
        await page.wait_for_load_state("networkidle")

    # Give Javascript bindings an extra cushion to safely map variables onto elements
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

        # Execute our custom layout validation framework
        await wait_for_room_data(page)

        rooms = []

        # Target the primary card/row wrappers directly
        room_cards = page.locator(".vres_room_infoBg, .vres_roomInfo, .roomTypeRow, [id*='roomType']")
        card_count = await room_cards.count()

        # Final structural fallback: loop over text-bearing row tags if classes are dynamic
        if card_count == 0:
            room_cards = page.locator("tr, div").filter(has_text="$")
            card_count = await room_cards.count()

        logger.info(f"{prop['name']} → Processing {card_count} parsing targets")

        for i in range(card_count):
            try:
                card = room_cards.nth(i)
                block_text = await card.inner_text()
                
                if not block_text.strip():
                    continue

                # 1. Parse Room Rate
                price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", block_text)
                if not price_match:
                    continue
                price = float(price_match.group(1).replace(",", ""))

                # 2. Extract Room Title
                lines = [l.strip() for l in block_text.split("\n") if l.strip()]
                name = lines[0]
                
                # Scan lines for explicit hotel classifications to isolate actual name text
                for line in lines:
                    if any(kw in line.lower() for kw in ["king", "queen", "suite", "room", "standard", "deluxe", "twin"]):
                        name = line
                        break

                # Strip trailing cleanups
                name = re.sub(r'(?i)(no pets|non-smoking|non smoking|smoking|view details)\s*[,\-]?\s*', '', name).strip(' -,')

                if len(name) < 4 or any(x in name.lower() for x in ["policy", "login", "terms", "total"]):
                    continue

                # 3. Parse Allocation Inventory Left
                left_match = re.search(r"(\d+)\s*[Rr]oom[s]?\s*[Ll]eft|only\s*(\d+)\s*[Rr]oom|(\d+)\s*[Ll]eft", block_text, re.I)
                
                rooms_left = 1  # Standard fallback default assuming 1 room remains available
                if left_match:
                    val = next(g for g in left_match.groups() if g is not None)
                    rooms_left = int(val)
                elif any(x in block_text.lower() for x in ["sold out", "not available", "unavailable"]):
                    rooms_left = 0

                rooms.append({
                    "room_type": name,
                    "rate": price,
                    "rooms_left": rooms_left
                })

            except Exception as card_err:
                continue

        await browser.close()

        if not rooms:
            raise Exception("No room configurations extracted after system load")

        # Deduplicate results records cleanly
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
            logger.info(f"{prop['name']} ✅ Execution complete ({len(res['rooms'])} types gathered)")
        except Exception as e:
            logger.error(f"{prop['name']} ❌ {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        asyncio.run(run())
