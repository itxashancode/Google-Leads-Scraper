"""
Run: python server.py
"""

import asyncio
import csv
import json
import random
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
import uvicorn


# ─── Pydantic Request Model ──────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    keyword: str = "plumber"
    location: str = "Karachi"
    min_rating: float = 4.0
    min_reviews: int = 5
    max_results: int = 50
    no_website_only: bool = True
    require_phone: bool = False


# ─── Lead Dataclass ──────────────────────────────────────────────────────────

@dataclass
class Lead:
    name: str = ""
    category: str = ""
    rating: float = 0.0
    review_count: int = 0
    address: str = ""
    phone: str = ""
    website: str = ""
    has_website: bool = False
    google_maps_url: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ─── Job Manager ─────────────────────────────────────────────────────────────

class JobManager:
    def __init__(self):
        self.jobs: Dict[str, dict] = {}

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())[:8]
        self.jobs[job_id] = {
            "status": "pending",
            "progress": 0,
            "processed": 0,
            "total": 0,
            "qualified": 0,
            "current_business": "",
            "leads": [],
            "stats": {
                "total_processed": 0,
                "has_website": 0,
                "no_website": 0,
                "below_rating": 0,
                "below_reviews": 0,
                "no_phone": 0,
                "qualified": 0,
                "skipped_errors": 0,
            },
            "error": None,
        }
        return job_id

    def get_job(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        return self.jobs[job_id]

    def update_job(self, job_id: str, **kwargs):
        self.jobs[job_id].update(kwargs)

    def add_lead(self, job_id: str, lead: Lead):
        self.jobs[job_id]["leads"].append(asdict(lead))
        self.jobs[job_id]["qualified"] += 1
        s = self.jobs[job_id]["stats"]
        s["qualified"] += 1
        s["total_processed"] += 1
        if lead.has_website:
            s["has_website"] += 1
        else:
            s["no_website"] += 1

    def record_filtered(self, job_id: str, lead: Lead, config: ScrapeRequest):
        """Record a lead that was parsed but didn't qualify."""
        s = self.jobs[job_id]["stats"]
        s["total_processed"] += 1
        if lead.has_website:
            s["has_website"] += 1
        else:
            s["no_website"] += 1
        if lead.rating < config.min_rating:
            s["below_rating"] += 1
        if lead.review_count < config.min_reviews:
            s["below_reviews"] += 1
        if config.require_phone and not lead.phone:
            s["no_phone"] += 1


job_manager = JobManager()


# ─── SSE Manager ─────────────────────────────────────────────────────────────

class SSEManager:
    def __init__(self):
        self.connections: Dict[str, List[asyncio.Queue]] = {}

    async def connect(self, job_id: str) -> asyncio.Queue:
        if job_id not in self.connections:
            self.connections[job_id] = []
        q = asyncio.Queue()
        self.connections[job_id].append(q)
        return q

    def disconnect(self, job_id: str, q: asyncio.Queue):
        if job_id in self.connections:
            try:
                self.connections[job_id].remove(q)
            except ValueError:
                pass
            if not self.connections[job_id]:
                del self.connections[job_id]

    async def broadcast(self, job_id: str, data: dict):
        if job_id in self.connections:
            dead = []
            for q in self.connections[job_id]:
                try:
                    await q.put(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self.disconnect(job_id, q)


sse_manager = SSEManager()


# ─── Scraper Engine ──────────────────────────────────────────────────────────

class GoogleMapsLeadScraper:
    def __init__(self, config: ScrapeRequest, job_id: str):
        self.config = config
        self.job_id = job_id
        self.leads: list[Lead] = []
        self._seen_names: set[str] = set()

    def _build_search_url(self) -> str:
        q = f"{self.config.keyword} in {self.config.location}"
        encoded = q.replace(" ", "+").replace(",", "%2C")
        return f"https://www.google.com/maps/search/{encoded}/"

    async def _human_delay(self, min_s=0.8, max_s=1.8):
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _scroll_results(self, page: Page, target_count: int):
        try:
            await page.wait_for_selector('[role="feed"]', timeout=15_000)
        except PlaywrightTimeout:
            return
        stall = 0
        while stall < 6:
            prev = await page.eval_on_selector_all('a[href*="/maps/place/"]', "els => els.length")
            await page.eval_on_selector('[role="feed"]', "el => el.scrollBy(0, 1000)")
            await asyncio.sleep(2.0)
            curr = await page.eval_on_selector_all('a[href*="/maps/place/"]', "els => els.length")
            if curr >= target_count:
                break
            stall = stall + 1 if curr == prev else 0

    async def _try_selectors(self, page: Page, selectors: list, attr: str = None) -> str:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    val = await el.get_attribute(attr) if attr else await el.inner_text()
                    if val and val.strip():
                        return val.strip()
            except Exception:
                continue
        return ""

    async def _check_website_deep(self, page: Page) -> tuple:
        SKIP_DOMAINS = [
            "google.com", "goo.gl", "maps.google", "support.google",
            "accounts.google", "play.google", "facebook.com", "fb.com",
            "instagram.com", "twitter.com", "x.com", "tiktok.com",
            "youtube.com", "youtu.be", "linkedin.com",
            "wa.me", "whatsapp.com", "t.me", "telegram.me",
        ]
        try:
            panel = await page.query_selector('div[role="main"]')
            if panel:
                await panel.evaluate("el => el.scrollBy(0, 600)")
                await asyncio.sleep(0.8)
                await panel.evaluate("el => el.scrollBy(0, 600)")
                await asyncio.sleep(0.6)
        except Exception:
            pass

        for sel in ['a[data-item-id*="authority"]', '[data-item-id*="authority"] a',
                    'a[aria-label="Website"]', 'a[aria-label="Open website"]']:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    href = (await el.get_attribute("href") or "").strip()
                    if href and not any(d in href for d in SKIP_DOMAINS):
                        return True, href
            except Exception:
                continue

        try:
            for btn in await page.query_selector_all('div[role="main"] a[target="_blank"]'):
                aria = (await btn.get_attribute("aria-label") or "").lower()
                if "website" in aria:
                    href = (await btn.get_attribute("href") or "").strip()
                    if href and not any(d in href for d in SKIP_DOMAINS):
                        return True, href
        except Exception:
            pass

        return False, ""

    async def _parse_listing(self, page: Page, link_el) -> Optional[Lead]:
        lead = Lead()
        try:
            href = await link_el.get_attribute("href")
            if href:
                lead.google_maps_url = href

            await link_el.click()
            await self._human_delay(2.5, 3.5)
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)

            name = await self._try_selectors(page, [
                'h1[class*="fontHeadlineLarge"]', '.DUwDvf.lfPIob',
                '.DUwDvf', 'div[role="main"] h1', 'h1',
            ])
            if not name or name in self._seen_names:
                return None
            self._seen_names.add(name)
            lead.name = name

            lead.category = await self._try_selectors(page, [
                'button[jsaction*="category"]', '.DUwDvf + span button',
            ])

            rating_text = await self._try_selectors(page, [
                'span[aria-hidden="true"][class*="fontDisplayLarge"]', '.fontDisplayLarge',
            ])
            if rating_text:
                m = re.search(r"(\d+[.,]\d+|\d+)", rating_text)
                if m:
                    try:
                        lead.rating = float(m.group(1).replace(",", "."))
                    except ValueError:
                        pass

            if lead.rating == 0:
                aria = await self._try_selectors(page, [
                    'div[role="main"] span[aria-label*="stars"]',
                    'span[aria-label*="star"]',
                ], attr="aria-label")
                if aria:
                    m = re.search(r"(\d+[.,]\d+|\d+)", aria)
                    if m:
                        try:
                            lead.rating = float(m.group(1).replace(",", "."))
                        except ValueError:
                            pass

            review_aria = await self._try_selectors(page, [
                'span[aria-label*="review"]', 'button[aria-label*="review"]',
            ], attr="aria-label")
            if review_aria:
                nums = re.findall(r"[\d,]+", review_aria)
                if nums:
                    try:
                        lead.review_count = int(nums[0].replace(",", ""))
                    except ValueError:
                        pass

            if lead.review_count == 0:
                m = re.search(r"([\d,]+)\s*reviews", await page.content(), re.IGNORECASE)
                if m:
                    try:
                        lead.review_count = int(m.group(1).replace(",", ""))
                    except ValueError:
                        pass

            lead.address = await self._try_selectors(page, [
                'button[data-item-id="address"] .fontBodyMedium',
                '[data-item-id="address"] div',
                'button[aria-label*="Address"]',
            ])

            phone = await self._try_selectors(page, [
                'button[data-item-id*="phone"] .fontBodyMedium',
                '[data-item-id*="phone:"]',
                'button[aria-label*="Phone"]',
            ])
            if not phone:
                el = await page.query_selector('a[href^="tel:"]')
                if el:
                    phone = (await el.get_attribute("href") or "").replace("tel:", "")
            if phone:
                lead.phone = re.sub(r'[^\d+\s\-\(\)]', '', phone).strip()

            lead.has_website, lead.website = await self._check_website_deep(page)
            return lead

        except PlaywrightTimeout:
            return None
        except Exception as e:
            print(f"Parse error: {e}")
            return None

    def _is_qualified(self, lead: Lead) -> bool:
        cfg = self.config
        if cfg.no_website_only and lead.has_website:
            return False
        if lead.rating < cfg.min_rating:
            return False
        if lead.review_count < cfg.min_reviews:
            return False
        if cfg.require_phone and not lead.phone:
            return False
        return True

    async def run(self):
        cfg = self.config
        jid = self.job_id

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            url = self._build_search_url()
            job_manager.update_job(jid, status="running", current_business="Loading Google Maps…")
            await sse_manager.broadcast(jid, job_manager.get_job(jid))

            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await self._human_delay(2, 3)

            for sel in ['button[aria-label*="Accept all"]', 'button[aria-label*="Accept"]',
                        'button[jsname="b3VHJd"]']:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._human_delay(1, 2)
                        break
                except Exception:
                    pass

            job_manager.update_job(jid, current_business="Scrolling for listings…")
            await sse_manager.broadcast(jid, job_manager.get_job(jid))
            await self._scroll_results(page, cfg.max_results)

            links = await page.query_selector_all('a[href*="/maps/place/"]')
            unique_links, seen_hrefs = [], set()
            for lnk in links:
                h = (await lnk.get_attribute("href") or "").split("?")[0]
                if h and h not in seen_hrefs:
                    seen_hrefs.add(h)
                    unique_links.append(lnk)

            total = min(len(unique_links), cfg.max_results)
            job_manager.update_job(jid, total=total, progress=5)
            await sse_manager.broadcast(jid, job_manager.get_job(jid))

            for i, link_el in enumerate(unique_links[:total], 1):
                progress = 5 + int((i / total) * 90)
                job_manager.update_job(
                    jid, processed=i, progress=progress,
                    current_business=f"Scanning listing {i} of {total}…"
                )
                await sse_manager.broadcast(jid, job_manager.get_job(jid))

                lead = await self._parse_listing(page, link_el)
                if lead is None:
                    job_manager.jobs[jid]["stats"]["skipped_errors"] += 1
                    continue

                if self._is_qualified(lead):
                    self.leads.append(lead)
                    job_manager.add_lead(jid, lead)
                else:
                    job_manager.record_filtered(jid, lead, cfg)

                await sse_manager.broadcast(jid, job_manager.get_job(jid))
                await self._human_delay(1.2, 2.5)

            await browser.close()

        job_manager.update_job(jid, status="done", progress=100, current_business="")
        await sse_manager.broadcast(jid, job_manager.get_job(jid))
        return self.leads


# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="LeadHunt API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/scrape")
async def start_scrape(request: ScrapeRequest):
    job_id = job_manager.create_job()
    job_manager.update_job(job_id, status="starting")
    asyncio.create_task(run_scrape_job(job_id, request))
    return {"job_id": job_id, "status": "started"}


async def run_scrape_job(job_id: str, config: ScrapeRequest):
    try:
        scraper = GoogleMapsLeadScraper(config, job_id)
        await scraper.run()
    except Exception as e:
        job_manager.update_job(job_id, status="error", error=str(e))
        await sse_manager.broadcast(job_id, job_manager.get_job(job_id))


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    return job_manager.get_job(job_id)


@app.get("/api/stream/{job_id}")
async def stream_updates(job_id: str, request: Request):
    queue = await sse_manager.connect(job_id)

    async def event_generator():
        try:
            # Send initial state immediately
            yield f"data: {json.dumps(job_manager.get_job(job_id))}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(data, default=str)}\n\n"
                    if data.get("status") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type':'heartbeat'})}\n\n"
        finally:
            sse_manager.disconnect(job_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{job_id}")
async def download_csv(job_id: str):
    job = job_manager.get_job(job_id)
    leads = job.get("leads", [])
    if not leads:
        raise HTTPException(status_code=404, detail="No leads found")

    Path("leads_output").mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = Path(f"leads_output/leads_{ts}.csv")
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=leads[0].keys())
        writer.writeheader()
        writer.writerows(leads)

    return FileResponse(
        filepath,
        media_type="text/csv",
        filename=filepath.name,
    )


if __name__ == "__main__":
    print("\n🚀 LeadHunt Server starting…")
    print("   API  → http://localhost:8000")
    print("   Open → index.html in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")