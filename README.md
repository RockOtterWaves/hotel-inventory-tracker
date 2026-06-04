# Hotel Inventory & Rate Tracker

Automatically tracks **same-day remaining inventory** and **blended ADR** for IPMS247-powered hotel booking pages — five times a day — and produces a **7 AM morning report** with estimated occupancy for each property.

---

## Features

- ✅ **Multi-property** support (add as many hotels as you like in `config.json`)
- ✅ **5 scheduled daily runs** — 9:00 AM, 3:00 PM, 6:00 PM, 9:30 PM, 11:59 PM (Pacific)
- ✅ **Manual on-demand run** at any time: `python scraper.py run`
- ✅ **7 AM morning report** — blended ADR, occupancy estimate, per-property timeline
- ✅ **Anti-bot / anti-fingerprinting** — random UA rotation, viewport randomisation, human-like delays, webdriver flag removal
- ✅ **Date rollover protection** — if the booking page no longer shows today's check-in (past midnight) the run is silently skipped and previous data is used
- ✅ **Retry logic** — 3 attempts per property with exponential back-off
- ✅ **Error isolation** — one property's failure never stops the others
- ✅ **CSV + TXT reports** saved to `/reports/`
- ✅ **Runs free on GitHub Actions** (see workflow below)

---

## Supported Properties (default config)

| Property | Rooms |
|---|---|
| Tarzana Inn | 49 |
| Sea Air Inn | 24 |
| Blufftop Inn | 32 |

Add your own in `config.json`.

---

## Quick Start (local)

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/hotel-inventory-tracker.git
cd hotel-inventory-tracker

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Run immediately (manual check)
python scraper.py run

# 4. Generate last night's report now
python scraper.py report

# 5. Start the full scheduled daemon
python scraper.py
```

---

## GitHub Actions (free, automated)

Create `.github/workflows/tracker.yml` — see the included file.

The workflow runs at each scheduled time (UTC-converted from Pacific), commits data files back to the repo, and uploads reports as artifacts.

---

## Configuration (`config.json`)

Auto-generated on first run. Edit to add/remove properties:

```json
{
  "properties": [
    {
      "name": "My Hotel",
      "url": "https://live.ipms247.com/booking/book-rooms-YOURHOTEL",
      "total_rooms": 40
    }
  ],
  "schedule": {
    "intraday_times": ["09:00", "15:00", "18:00", "21:30", "23:59"],
    "morning_report_time": "07:00"
  },
  "scraper": {
    "min_delay_sec": 3,
    "max_delay_sec": 8,
    "page_timeout_ms": 45000,
    "retry_attempts": 3,
    "retry_delay_sec": 15
  }
}
```

---

## Output Files

| File | Description |
|---|---|
| `data/YYYY-MM-DD.json` | Raw snapshots for each run of the day |
| `reports/morning_report_YYYY-MM-DD.txt` | Human-readable overnight report |
| `reports/data_YYYY-MM-DD.csv` | Spreadsheet-friendly timeline for analysis |
| `logs/tracker_YYYY-MM-DD.log` | Full debug log |

---

## How Occupancy Is Estimated

```
Rooms Sold = Total Rooms in Property − Sum of All Remaining Rooms (last snapshot)
Occupancy % = Rooms Sold / Total Rooms × 100
```

The **last valid snapshot before midnight** is used. If the midnight (11:59 PM) scrape catches a date rollover (booking page switched to tomorrow), that snapshot is discarded and the previous one is used instead.

---

## Blended ADR

Simple average of the nightly rate across all **available** room types at time of scrape. Weighted averaging is not possible without per-room historical booking data.

---

## Anti-Bot Notes

- Random User-Agent from a pool of 5 real browser strings
- Random viewport (1366–1920 px)
- `navigator.webdriver` property removed
- Human-like delays (1.5–8 seconds) between interactions
- Random 5–15 second gap between properties
- Exponential back-off on retries

---

## Disclaimer

This tool is for operational analytics of your own properties only. Ensure your use complies with the booking engine's terms of service.
