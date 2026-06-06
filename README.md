# LeadHunt — Google Maps Lead Scraper

A production-grade, asynchronous **Google Maps lead scraper** built with **FastAPI**, **Playwright**, and **Server-Sent Events**. Designed for freelancers and agencies who want to identify local businesses that lack a web presence and qualify them as outreach prospects.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Key Features](#key-features)
3. [Architecture](#architecture)
4. [Data Model](#data-model)
5. [Lead Scoring & Priority System](#lead-scoring--priority-system)
6. [Prerequisites](#prerequisites)
7. [Installation](#installation)
8. [Configuration & Environment Variables](#configuration--environment-variables)
9. [Running the Server](#running-the-server)
10. [API Reference](#api-reference)
11. [ScrapeRequest Parameters](#scraperequest-parameters)
12. [Google Sheets Integration](#google-sheets-integration)
13. [Front-End Usage](#front-end-usage)
14. [Persistence & Deduplication](#persistence--deduplication)
15. [Caching Layer](#caching-layer)
16. [Adaptive Throttle](#adaptive-throttle)
17. [Extending the Scraper](#extending-the-scraper)
18. [Troubleshooting](#troubleshooting)
19. [License](#license)

---

## What It Does

LeadHunt scrapes Google Maps for local businesses matching a keyword + location query, then scores and classifies each result as a sales prospect. It is purpose-built to surface businesses that:

- Have no website, or use a low-quality template builder (Wix, Squarespace, etc.)
- Have an established review base, signaling an active customer flow
- Show review text that mentions pain points like "no online booking" or "can't find their hours"

Leads are streamed live to a browser UI via SSE and written to CSV incrementally—so you start seeing results within seconds, not after the full job completes.

---

## Key Features

- **Async parallel scraping** — configurable number of concurrent Playwright browser contexts (`PARALLEL_CONTEXTS`)
- **Lead scoring engine** — 0–100 score based on review count, recency, photo activity, price level, competitor density, and pain-point review signals
- **Priority classification** — Hot / Warm / Cold / Educate tiers with defined thresholds
- **Fuzzy deduplication** — `SequenceMatcher`-based name + address comparison across and within jobs, backed by a persistent `seen_leads.json` registry
- **Listing cache** — parsed listing data is cached by Google Maps URL slug; repeated scrapes of the same business skip the browser round-trip
- **Adaptive throttle** — tracks average response latency and scales sleep intervals up/down automatically to reduce bot-detection risk
- **Intent signal detection** — scans review snippets for phrases that indicate a specific web-service need (booking, menu, hours, no website, old-fashioned)
- **Website builder detection** — identifies Wix, Squarespace, WordPress.com, Weebly, and 8 other template platforms in the website URL
- **Google Sheets push** — optional fire-and-forget push of each qualified lead to a Google Apps Script webhook
- **Activity log** — up to 200 entries per job, each tagged by type: `lead`, `dupe`, `filtered`, `sheets_ok`, `sheets_fail`, `error`
- **Real-time SSE** — all connected clients receive live state updates; heartbeat every 25 s to keep connections alive

---

## Architecture

```
Browser (index.html)
    │
    ├─ POST /api/scrape ────────────────────────────────────────────────────────┐
    │                                                                           │
    │                          FastAPI (server.py)                              │
    │                                                                           │
    │   JobManager ◄──── GoogleMapsLeadScraper ────► SSEManager                │
    │       │                    │                       │                      │
    │       │             Playwright (Chromium)          │                      │
    │       │             ┌──────────────────┐           │                      │
    │       │             │ N parallel ctxs  │           │                      │
    │       │             │ per job          │           │                      │
    │       │             └──────────────────┘           │                      │
    │       │                    │                       │                      │
    │       ▼                    ▼                       │                      │
    │   leads[],           seen_leads.json         asyncio.Queue                │
    │   priority_counts    listing_cache.json       per client                  │
    │   stats              leads_output/*.csv                                   │
    │                                                                           │
    ├─ GET /api/stream/{job_id}  ◄──────────────────────┘  (SSE)               │
    ├─ GET /api/status/{job_id}                                                 │
    └─ GET /api/download/{job_id}  →  CSV file response                        │
                                                                                │
    asyncio.create_task(run_scrape_job(...)) ◄─────────────────────────────────┘
```

All I/O—Playwright browser operations, HTTP calls to the Sheets webhook, SSE broadcasts—runs in the same asyncio event loop. No threads, no subprocess pools.

---

## Data Model

Each scraped business is represented as a `Lead` dataclass. All fields are written to CSV and returned in the job JSON.

| Field | Type | Description |
|---|---|---|
| `name` | str | Business name |
| `category` | str | Google Maps category (e.g., "Plumber") |
| `rating` | float | Average star rating |
| `review_count` | int | Total review count |
| `address` | str | Full address string |
| `phone` | str | Phone number as displayed |
| `phone_valid` | bool | ITU E.164 digit-count check (7–15 digits) |
| `website` | str | Website URL if present |
| `has_website` | bool | True if a non-social website was found |
| `website_builder` | str | Detected template platform name, or `""` |
| `google_maps_url` | str | Direct Maps link |
| `country` | str | From `ScrapeRequest.country` |
| `scraped_at` | str | ISO-8601 timestamp |
| `email` | str | Email found in description/profile |
| `price_level` | int | 0 = unknown, 1–4 = $ to $$$$ |
| `business_hours` | str | Raw hours string |
| `business_hours_status` | str | `24/7` \| `limited` \| `temporarily_closed` \| `normal` |
| `photo_count` | int | Number of photos on the listing |
| `days_since_last_review` | int? | Calculated from relative date text |
| `lead_score` | int | 0–100 composite score |
| `priority` | str | `Hot` \| `Warm` \| `Cold` \| `Educate` |
| `negative_review_hits` | int | Count of pain-point phrases found |
| `intent_signals` | str | Comma-separated detected intent categories |
| `review_snippets` | str | Sample review text for context |
| `competitors_total` | int | Nearby businesses in same category |
| `competitors_with_website` | int | How many competitors have a website |
| `competitor_names` | str | Comma-separated competitor names |
| `latitude` | float | Geo coordinate |
| `longitude` | float | Geo coordinate |

---

## Lead Scoring & Priority System

### Score (0–100)

The `compute_lead_score()` function assembles a score from these components:

| Signal | Max Points |
|---|---|
| No website | 30 |
| Template/builder website | 15 |
| Review count band (≥200 / ≥50 / ≥10 / <10) | 20 |
| Review recency (≤7 days / ≤30 / ≤90) | 12 |
| Pain-point phrases in reviews (×4 per hit, capped) | 16 |
| Competitor web density ratio | 10 |
| Photo count | 5 |
| Price level (×2 per level) | 6 |
| Hours status (24/7 / limited / temp closed) | 5 |
| High rating + no website bonus | 5 |

### Priority Tier

| Tier | Criteria |
|---|---|
| **Hot** | No website + ≥50 reviews + last review ≤30 days |
| **Warm** | No website + ≥10 reviews |
| **Cold** | No website + <10 reviews |
| **Educate** | Has a website (custom or template) — upgrade/modernisation pitch |

### Intent Signals

Review text is scanned for these categories:

| Category | Example trigger phrases |
|---|---|
| `website_absent` | "no website", "hard to find online", "wish they had a website" |
| `online_booking` | "can't book online", "wish they had online booking" |
| `menu_hours` | "no menu online", "can't find hours" |
| `old_fashioned` | "old-fashioned", "outdated", "behind the times" |

---

## Prerequisites

| Requirement | Minimum |
|---|---|
| Python | 3.10+ |
| pip | 22+ |
| Playwright Chromium | installed via `playwright install chromium` |

---

## Installation

```bash
# 1. Clone
git clone https://github.com/itxashancode/Google-Leads-Scraper.git
cd Google-Leads-Scraper

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's headless browser
playwright install chromium
```

---

## Configuration & Environment Variables

All runtime config is read from environment variables. None are required for basic local use.

| Variable | Default | Description |
|---|---|---|
| `GAS_URL` | `""` | Google Apps Script webhook URL. If empty, Sheets push is silently disabled. |
| `GAS_ADMIN_CODE` | `"ADM-SCRAPER"` | Shared secret sent in the Sheets push payload for webhook auth. |
| `PARALLEL_CONTEXTS` | `2` | Number of concurrent Playwright browser contexts per scrape job. Increase for speed, decrease if getting blocked. |

Set variables before launching:

```bash
# Linux / macOS
export GAS_URL="https://script.google.com/macros/s/YOUR_SCRIPT_ID/exec"
export GAS_ADMIN_CODE="my-secret"
export PARALLEL_CONTEXTS=3

# Windows PowerShell
$env:GAS_URL = "https://script.google.com/macros/s/YOUR_SCRIPT_ID/exec"
```

---

## Running the Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

Or run the module directly (also works):

```bash
python server.py
```

The startup log will confirm environment state:

```
  GAS_URL: ✓ Set
  Dedup registry: 142 known leads
  Listing cache: 87 entries
  Parallel contexts: 2
```

Open `index.html` in any modern browser to use the UI, or call the API directly.

Interactive API docs are available at `http://localhost:8000/docs` (Swagger UI).

---

## API Reference

### `POST /api/scrape`

Start a new scrape job. Returns a `job_id` immediately; the scrape runs in the background.

**Request body:** `ScrapeRequest` JSON (see next section)

**Response:**
```json
{ "job_id": "a3f9c1b2", "status": "started" }
```

---

### `GET /api/status/{job_id}`

Returns the full job state as JSON, including all leads collected so far, filter stats, priority counts, and the activity log.

---

### `GET /api/stream/{job_id}`

Server-Sent Events stream. The client receives a full job state snapshot on every meaningful update. Format:

```
data: {"status":"running","progress":42,"qualified":7,...}\n\n
```

A heartbeat is sent every 25 seconds if there are no updates, to prevent proxy/browser timeouts:

```
data: {"type":"heartbeat"}\n\n
```

The stream closes automatically when `status` reaches `done` or `error`.

---

### `GET /api/download/{job_id}`

Returns a CSV file of all qualified leads for that job. Returns HTTP 404 if no leads were found.

---

### `GET /api/leads/{job_id}/hot`

Returns only leads classified as `Hot` priority.

**Response:**
```json
{ "count": 3, "leads": [...] }
```

---

### `GET /api/leads/{job_id}/by_priority`

Returns all leads grouped and sorted by priority tier (Hot → Warm → Cold → Educate), then by score descending within each tier.

**Response:**
```json
{
  "priority_counts": { "Hot": 3, "Warm": 9, "Cold": 2, "Educate": 1 },
  "leads": { "Hot": [...], "Warm": [...], ... }
}
```

---

### `GET /api/dedup/stats`

Returns the count of leads in the persistent dedup registry.

---

### `DELETE /api/dedup/reset`

Clears `seen_leads.json`. All businesses become eligible to appear in future scrapes again.

---

### `GET /api/cache/stats`

Returns the count of listings currently cached.

---

### `DELETE /api/cache/reset`

Clears `listing_cache.json`. Forces fresh Playwright page loads for all listings on the next scrape.

---

## ScrapeRequest Parameters

```json
{
  "keyword": "plumber",
  "location": "Karachi",
  "country": "",
  "min_rating": 4.0,
  "min_reviews": 5,
  "max_results": 50,
  "no_website_only": true,
  "require_phone": false,
  "push_to_sheets": true,
  "scrape_competitors": true,
  "use_cache": true,
  "parallel_contexts": 2
}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `keyword` | string | `"plumber"` | Business type to search (e.g., "dentist", "restaurant") |
| `location` | string | `"Karachi"` | City, neighborhood, or address |
| `country` | string | `""` | Written to each lead's `country` field; not used in the search query |
| `min_rating` | float | `4.0` | Minimum average star rating to qualify |
| `min_reviews` | int | `5` | Minimum review count to qualify |
| `max_results` | int | `50` | Stop after qualifying this many leads |
| `no_website_only` | bool | `true` | If true, only leads without a real website are returned |
| `require_phone` | bool | `false` | If true, leads without a phone number are filtered out |
| `push_to_sheets` | bool | `true` | Enable Google Sheets push (requires `GAS_URL` to be set) |
| `scrape_competitors` | bool | `true` | Fetch nearby businesses in the same category for competitor context |
| `use_cache` | bool | `true` | Read/write the listing cache for faster repeat scrapes |
| `parallel_contexts` | int | env var | Override `PARALLEL_CONTEXTS` for this job only |

---

## Google Sheets Integration

The scraper can push each qualified lead to a Google Sheet in real time as it is found.

**Setup:**

1. Create a Google Apps Script bound to a Sheet with a `doPost(e)` handler that accepts JSON and appends rows.
2. Deploy it as a Web App with "Execute as: Me" and "Who has access: Anyone".
3. Set `GAS_URL` to the deployment URL and `GAS_ADMIN_CODE` to any shared secret you validate in the script.

**Payload format sent by the scraper:**

```json
{
  "action": "appendLeads",
  "code": "ADM-SCRAPER",
  "leads": [ { ...Lead fields... } ]
}
```

Push attempts are fire-and-forget — a failed push never blocks or slows down the scrape. The activity log records `sheets_ok` or `sheets_fail` for each attempt, and the job's `pushed_to_sheets` counter is updated in real time via SSE.

---

## Front-End Usage

Open `index.html` directly in a browser (no build step or server required for the HTML itself).

The UI:
- Submits `POST /api/scrape` and stores the returned `job_id`
- Opens an `EventSource` to `GET /api/stream/{job_id}` and renders live stats
- Shows a progress bar, priority breakdown, and an activity feed
- Activates a **Download CSV** button once `status === "done"`

The front-end is intentionally dependency-free vanilla HTML/JS so it can be opened from the filesystem without a web server.

---

## Persistence & Deduplication

Two files are written to disk automatically and read on startup:

**`seen_leads.json`** — A flat JSON array of dedup keys (composite of `name|phone|address`). Any lead whose key already appears here is skipped without opening a browser page for it. Persists across server restarts.

**Within a job**, a second layer of fuzzy deduplication uses `SequenceMatcher` on name + address with an 0.85 similarity threshold to catch near-duplicates (e.g., same business listed slightly differently).

Use `DELETE /api/dedup/reset` to clear the registry when you want to rescrape a market from scratch.

---

## Caching Layer

**`listing_cache.json`** — Stores the parsed data from individual Google Maps listing pages, keyed by the URL slug (`/maps/place/<slug>`). On subsequent scrapes, if a listing's slug is in the cache, the scraper returns the cached data instantly instead of launching a browser context.

Cache entries are written per-job. The `_cache_dirty` flag ensures the file is only written to disk if at least one new entry was added.

Use `DELETE /api/cache/reset` if you want fresh data (e.g., after a business updates their profile).

---

## Adaptive Throttle

The `AdaptiveThrottle` class tracks a rolling window of the last 10 response latencies and adjusts sleep intervals between requests:

| Condition | Sleep range |
|---|---|
| Average latency > 5 s (slow, likely throttled) | `base_min × 2` to `base_max × 2.5` |
| Average latency < 1.5 s (fast, safe to push) | `max(0.5, base_min × 0.7)` to `base_max × 0.8` |
| Normal | `base_min` (1.0 s) to `base_max` (2.5 s) |

---

## Extending the Scraper

**Add a new lead field:**
1. Add the field to the `Lead` dataclass in `server.py`.
2. Populate it inside `GoogleMapsLeadScraper` where the listing data is parsed.
3. The CSV writer uses `asdict(lead).keys()` dynamically, so the new column appears automatically.

**Add a new intent signal category:**
Add an entry to `INTENT_PHRASES` dict with a list of trigger phrases. The `detect_intent_signals()` function will pick it up automatically.

**Add a new website builder:**
Add a `"domain.com": "Builder Name"` entry to the `WEBSITE_BUILDERS` dict.

**Change scoring weights:**
Edit `compute_lead_score()`. All score contributions are isolated additions; changing one does not affect others.

**Change priority thresholds:**
Edit `classify_priority()`. The current thresholds (Hot: ≥50 reviews + ≤30 days; Warm: ≥10 reviews) are at the top of the function.

---

## Troubleshooting

**Playwright can't find Chromium**
Run `playwright install chromium` again. Ensure the process has write access to `~/.cache/ms-playwright` (Linux/macOS) or `%USERPROFILE%\.cache\ms-playwright` (Windows).

**Zero leads returned**
- Set `no_website_only: false` and lower `min_rating` / `min_reviews` to confirm the scraper is reaching listings at all.
- Check the server console for Playwright timeout messages — the Maps page may not be loading.
- Verify the keyword + location combination returns results in a real browser.

**Google blocking requests**
- Increase `base_min` / `base_max` in `AdaptiveThrottle.__init__`.
- Lower `PARALLEL_CONTEXTS` to 1.
- Use a VPN or residential proxy (proxy injection via Playwright `launch(proxy=...)` in `_make_context`).

**SSE stream disconnects immediately**
- Check browser dev tools for CORS errors — ensure `server.py` is reachable on the same host/port shown in `index.html`.
- Some ad-blockers block `EventSource` connections; disable them for localhost.

**Sheets push not working**
- Confirm `GAS_URL` is set in the environment before starting the server; it is read at import time.
- Verify the Apps Script deployment is set to "Anyone" access.
- Check the activity log for `sheets_fail` entries; the error message is printed to the server console.

**`uvicorn.run(app, host="0.0.a0.0", ...)` typo**
The last line of `server.py` has `"0.0.a0.0"` instead of `"0.0.0.0"`. This only affects the `python server.py` launch path; `uvicorn server:app ...` from the command line is unaffected. Fix it before deploying.

---

## License

MIT — see [LICENSE](LICENSE) for full text.