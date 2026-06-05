async def scrape_property(prop):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        responses = []

        async def log_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "text" in ct:
                    text = await response.text()

                    # dump only large payloads (likely data)
                    if len(text) > 500:
                        responses.append({
                            "url": response.url,
                            "text": text[:2000]  # preview
                        })
            except:
                pass

        page.on("response", log_response)

        await page.goto(prop["url"], timeout=60000)

        await asyncio.sleep(10)

        await browser.close()

        # ✅ SAVE DEBUG FILE
        import json, time
        fname = f"debug_{prop['name'].replace(' ', '_')}.json"
        with open(fname, "w") as f:
            json.dump(responses, f, indent=2)

        raise Exception(f"Debug saved → {fname}")
