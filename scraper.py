async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        logs = []

        async def log_response(response):
            try:
                url = response.url
                ct = response.headers.get("content-type", "")
                text = await response.text()

                logs.append({
                    "url": url,
                    "content_type": ct,
                    "preview": text[:2000]
                })

            except Exception as e:
                logs.append({
                    "error": str(e)
                })

        page.on("response", log_response)

        await page.goto(prop["url"], timeout=60000)

        await asyncio.sleep(10)

        await browser.close()

        # ✅ ALWAYS WRITE DEBUG FILE — NO CONDITIONS
        from pathlib import Path
        import json

        fname = Path(f"debug_{prop['name'].replace(' ', '_')}.json")

        with open(fname, "w") as f:
            json.dump(logs, f, indent=2)

        print(f"✅ Debug file created: {fname}")

        # ✅ Stop after debug — DO NOT try to parse yet
        return None
