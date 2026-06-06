import asyncio
import csv
import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
import uvicorn


# ─── Config ──────────────────────────────────────────────────────────────────

GAS_URL: str = os.environ.get("GAS_URL", "")
GAS_ADMIN_CODE: str = os.environ.get("GAS_ADMIN_CODE", "ADM-SCRAPER")
SEEN_LEADS_FILE = Path("seen_leads.json")
CACHE_FILE = Path("listing_cache.json")
OUTPUT_DIR = Path("leads_output")

# Number of concurrent browser contexts for parallel scraping
PARALLEL_CONTEXTS = int(os.environ.get("PARALLEL_CONTEXTS", "2"))


# ─── Deduplication Registry ───────────────────────────────────────────────────

def load_seen_leads() -> set:
    if SEEN_LEADS_FILE.exists():
        try:
            return set(json.loads(SEEN_LEADS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen_leads(seen: set):
    SEEN_LEADS_FILE.write_text(json.dumps(list(seen), ensure_ascii=False))


def make_lead_key(lead_dict: dict) -> str:
    return "|".join([
        (lead_dict.get("name") or "").strip().lower(),
        (lead_dict.get("phone") or "").strip().lower(),
        (lead_dict.get("address") or "").strip().lower(),
    ])


# ─── Listing Cache ────────────────────────────────────────────────────────────

def load_listing_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_listing_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, default=str))


def get_cache_key(url: str) -> str:
    """Derive a stable cache key from a Google Maps URL."""
    m = re.search(r"/maps/place/([^/]+)", url)
    return m.group(1) if m else url.split("?")[0]


# ─── Fuzzy Duplicate Detection ────────────────────────────────────────────────

def fuzzy_similar(a: str, b: str, threshold: float = 0.85) -> bool:
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio() >= threshold


def is_fuzzy_duplicate(lead_dict: dict, seen_leads_list: list) -> bool:
    """Check if a lead is a fuzzy-duplicate of any previously seen lead."""
    name = (lead_dict.get("name") or "").strip().lower()
    addr = (lead_dict.get("address") or "").strip().lower()
    for seen in seen_leads_list:
        s_name = (seen.get("name") or "").strip().lower()
        s_addr = (seen.get("address") or "").strip().lower()
        if fuzzy_similar(name, s_name) and fuzzy_similar(addr, s_addr):
            return True
    return False


# ─── Lead Scoring Engine ──────────────────────────────────────────────────────

NEGATIVE_REVIEW_PHRASES = [
    "no website", "hard to find online", "can't find online", "cannot find online",
    "no online presence", "not online", "no web", "wish they had", "wish they were online",
    "wish they had online booking", "hard to book", "can't book online", "no booking online",
    "old-fashioned", "old fashioned", "outdated", "cant find menu", "can't find menu",
    "no menu online", "no hours online", "can't find hours", "hard to find hours",
    "no online ordering", "wish they had a website", "should have a website",
]

INTENT_PHRASES = {
    "online_booking": ["wish they had online booking", "can't book online", "hard to book",
                       "no online booking", "no booking", "online appointment"],
    "menu_hours":     ["can't find menu", "no menu online", "no hours online",
                       "can't find hours", "hard to find hours", "menu not online"],
    "website_absent": ["no website", "wish they had a website", "should have a website",
                       "hard to find online", "not online", "no online presence"],
    "old_fashioned":  ["old-fashioned", "old fashioned", "outdated", "not modern",
                       "behind the times"],
}

WEBSITE_BUILDERS = {
    "wix.com": "Wix",
    "wixsite.com": "Wix",
    "wordpress.com": "WordPress (hosted)",
    ".wordpress.com": "WordPress (hosted)",
    "squarespace.com": "Squarespace",
    "weebly.com": "Weebly",
    "godaddy.com": "GoDaddy",
    "jimdo.com": "Jimdo",
    "webnode.com": "Webnode",
    "site123.com": "Site123",
    "yola.com": "Yola",
    "strikingly.com": "Strikingly",
}


def detect_website_builder(url: str) -> str:
    """Return builder name if URL matches a known template platform, else ''."""
    u = url.lower()
    for domain, name in WEBSITE_BUILDERS.items():
        if domain in u:
            return name
    return ""


def compute_lead_score(
    review_count: int,
    rating: float,
    has_website: bool,
    website_url: str,
    negative_review_hits: int,
    days_since_last_review: Optional[int],
    competitor_count_with_website: int,
    total_competitors: int,
    photo_count: int,
    price_level: int,
    business_hours_status: str,
) -> int:
    """
    Score 0-100. Higher = more urgent prospect.
    Only meaningful when has_website is False (or has a template site).
    """
    score = 0

    # Base: no website is the core signal
    if not has_website:
        score += 30
    elif detect_website_builder(website_url):
        score += 15   # template site, still an opportunity

    # Review count bands
    if review_count >= 200:
        score += 20
    elif review_count >= 50:
        score += 15
    elif review_count >= 10:
        score += 8
    else:
        score += 2

    # Review velocity (active recently)
    if days_since_last_review is not None:
        if days_since_last_review <= 7:
            score += 12
        elif days_since_last_review <= 30:
            score += 8
        elif days_since_last_review <= 90:
            score += 4

    # Negative review pain-point mentions
    score += min(negative_review_hits * 4, 16)

    # Competitor density (many competitors WITH websites = more urgency)
    if total_competitors > 0:
        ratio = competitor_count_with_website / total_competitors
        score += int(ratio * 10)

    # Photo activity (business cares about presentation)
    if photo_count >= 20:
        score += 5
    elif photo_count >= 5:
        score += 2

    # Price level (higher = more budget for web services)
    score += min(price_level * 2, 6)

    # Hours signals
    if business_hours_status == "24/7":
        score += 5
    elif business_hours_status == "limited":
        score += 3
    elif business_hours_status == "temporarily_closed":
        score += 4

    # Rating: very high ratings + no website = strong case
    if rating >= 4.5 and not has_website:
        score += 5

    return min(score, 100)


def classify_priority(
    lead_score: int,
    review_count: int,
    has_website: bool,
    website_builder: str,
    days_since_last_review: Optional[int],
) -> str:
    """
    Hot  : No website + 50+ reviews + active ≤30 days
    Warm : No website + 10-49 reviews
    Cold : No website, low activity
    Educate: Has template/poor website
    """
    if has_website and not website_builder:
        return "Educate"  # full custom site, educate on modernisation

    if website_builder:
        return "Educate"   # template site → upgrade opportunity

    # No website path
    recent = days_since_last_review is not None and days_since_last_review <= 30
    if review_count >= 50 and recent:
        return "Hot"
    if review_count >= 10:
        return "Warm"
    return "Cold"


def detect_intent_signals(review_snippets: List[str]) -> List[str]:
    """Return list of detected intent categories from review text."""
    found = set()
    combined = " ".join(review_snippets).lower()
    for category, phrases in INTENT_PHRASES.items():
        for phrase in phrases:
            if phrase in combined:
                found.add(category)
                break
    return sorted(found)


# ─── Phone Validation ─────────────────────────────────────────────────────────

def validate_phone(phone: str) -> Tuple[bool, str]:
    """
    Basic format validation. Returns (is_valid, normalized).
    Checks: min 7 digits, max 15 digits (ITU E.164 limit).
    """
    if not phone:
        return False, ""
    digits = re.sub(r'\D', '', phone)
    if len(digits) < 7 or len(digits) > 15:
        return False, phone
    return True, phone


# ─── Pydantic Request Model ──────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    keyword: str = "plumber"
    location: str = "Karachi"
    country: str = ""
    min_rating: float = 4.0
    min_reviews: int = 5
    max_results: int = 50
    no_website_only: bool = True
    require_phone: bool = False
    push_to_sheets: bool = True
    scrape_competitors: bool = True   # Fetch nearby competitor data
    use_cache: bool = True            # Use cached listing data when available
    parallel_contexts: int = PARALLEL_CONTEXTS


# ─── Lead Dataclass ──────────────────────────────────────────────────────────

@dataclass
class Lead:
    # ── Core fields ──
    name: str = ""
    category: str = ""
    rating: float = 0.0
    review_count: int = 0
    address: str = ""
    phone: str = ""
    phone_valid: bool = False
    website: str = ""
    has_website: bool = False
    website_builder: str = ""          # e.g. "Wix", "Squarespace"
    google_maps_url: str = ""
    country: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # ── Enriched fields ──
    email: str = ""                    # Found in profile/description
    price_level: int = 0               # 0=unknown, 1=$, 2=$$, 3=$$$, 4=$$$$
    business_hours: str = ""           # Raw hours string
    business_hours_status: str = ""    # "24/7" | "limited" | "temporarily_closed" | "normal"
    photo_count: int = 0
    days_since_last_review: Optional[int] = None

    # ── Scoring & Classification ──
    lead_score: int = 0                # 0-100
    priority: str = ""                 # Hot | Warm | Cold | Educate
    negative_review_hits: int = 0
    intent_signals: str = ""           # CSV of detected intent categories
    review_snippets: str = ""          # Sample review text (for pain point context)

    # ── Competitor Context ──
    competitors_total: int = 0
    competitors_with_website: int = 0
    competitor_names: str = ""         # CSV of nearby competitor names

    # ── Geo ──
    latitude: float = 0.0
    longitude: float = 0.0


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
            "new_leads": 0,
            "skipped_dupes": 0,
            "pushed_to_sheets": 0,
            "current_business": "",
            "leads": [],
            "priority_counts": {"Hot": 0, "Warm": 0, "Cold": 0, "Educate": 0},
            "stats": {
                "total_processed": 0,
                "has_website": 0,
                "no_website": 0,
                "below_rating": 0,
                "below_reviews": 0,
                "no_phone": 0,
                "qualified": 0,
                "skipped_errors": 0,
                "skipped_dupes": 0,
                "cache_hits": 0,
            },
            "error": None,
            "activity_log": [],   # [{ts, type, name, detail}]
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
        p = self.jobs[job_id]["priority_counts"]
        if lead.priority in p:
            p[lead.priority] += 1

    def record_filtered(self, job_id: str, lead: Lead, config: ScrapeRequest):
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

    def record_dupe(self, job_id: str):
        self.jobs[job_id]["skipped_dupes"] += 1
        self.jobs[job_id]["stats"]["skipped_dupes"] += 1

    def record_cache_hit(self, job_id: str):
        self.jobs[job_id]["stats"]["cache_hits"] += 1

    def add_log(self, job_id: str, log_type: str, name: str, detail: str = ""):
        """Append an activity log entry (max 200 kept)."""
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "type": log_type,   # "lead" | "dupe" | "filtered" | "sheets_ok" | "sheets_fail" | "error"
            "name": name,
            "detail": detail,
        }
        log = self.jobs[job_id]["activity_log"]
        log.append(entry)
        if len(log) > 200:
            log.pop(0)


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


# ─── Google Sheets Pusher ─────────────────────────────────────────────────────

async def push_lead_to_sheets(lead: Lead, gas_url: str, admin_code: str) -> bool:
    if not gas_url:
        return False
    payload = {
        "action": "appendLeads",
        "code": admin_code,
        "leads": [asdict(lead)]
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                gas_url,
                content=json.dumps(payload),
                headers={"Content-Type": "text/plain"}
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("inserted", 0) > 0 or "error" not in data
    except Exception as e:
        print(f"  [Sheets] Push failed: {e}")
    return False


async def push_lead_to_sheets_bg(job_id: str, lead: Lead, gas_url: str, admin_code: str):
    """
    Fire-and-forget wrapper: pushes a single lead to Sheets in the background
    and updates the job counter when done — never blocks the scrape loop.
    """
    pushed = await push_lead_to_sheets(lead, gas_url, admin_code)
    if pushed:
        job_manager.jobs[job_id]["pushed_to_sheets"] += 1
        job_manager.add_log(job_id, "sheets_ok", lead.name, "Pushed to Sheets ✓")
        print(f"  [Sheets] ✓ Pushed: {lead.name}")
    else:
        job_manager.add_log(job_id, "sheets_fail", lead.name, "Sheets push failed ✗")
        print(f"  [Sheets] ✗ Failed: {lead.name}")
    # Broadcast updated pushed count so the UI reflects it in real time
    try:
        await sse_manager.broadcast(job_id, job_manager.get_job(job_id))
    except Exception:
        pass


# ─── CSV Writer ───────────────────────────────────────────────────────────────

def append_to_csv(filepath: Path, lead: Lead, write_header: bool = False):
    fieldnames = list(asdict(lead).keys())
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(asdict(lead))


# ─── Adaptive Throttle ───────────────────────────────────────────────────────

class AdaptiveThrottle:
    """
    Tracks average response latency and adjusts sleep intervals.
    If Google responds fast → we speed up slightly.
    If responses slow (throttling) → we back off.
    """
    def __init__(self, base_min=1.0, base_max=2.5):
        self.base_min = base_min
        self.base_max = base_max
        self.latencies: List[float] = []
        self._lock = asyncio.Lock()

    def record(self, latency: float):
        self.latencies.append(latency)
        if len(self.latencies) > 10:
            self.latencies.pop(0)

    async def sleep(self):
        if self.latencies:
            avg = sum(self.latencies) / len(self.latencies)
            if avg > 5.0:
                # Slow responses → back off
                min_s, max_s = self.base_min * 2, self.base_max * 2.5
            elif avg < 1.5:
                # Fast responses → speed up slightly
                min_s, max_s = max(0.5, self.base_min * 0.7), self.base_max * 0.8
            else:
                min_s, max_s = self.base_min, self.base_max
        else:
            min_s, max_s = self.base_min, self.base_max
        await asyncio.sleep(random.uniform(min_s, max_s))


# ─── Scraper Engine ──────────────────────────────────────────────────────────

SKIP_DOMAINS = [
    "google.com", "goo.gl", "maps.google", "support.google",
    "accounts.google", "play.google", "facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "youtu.be", "linkedin.com",
    "wa.me", "whatsapp.com", "t.me", "telegram.me",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1280, "height": 900},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]


class GoogleMapsLeadScraper:
    def __init__(self, config: ScrapeRequest, job_id: str, seen_keys: set, csv_path: Path):
        self.config = config
        self.job_id = job_id
        self.leads: list[Lead] = []
        self._seen_names: set[str] = set()
        self._seen_keys: set[str] = seen_keys
        self._seen_leads_list: list[dict] = []   # For fuzzy dedup
        self._csv_path: Path = csv_path
        self._csv_header_written: bool = False
        self._throttle = AdaptiveThrottle()
        self._listing_cache: dict = load_listing_cache() if config.use_cache else {}
        self._cache_dirty: bool = False

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

    async def _check_website_deep(self, page: Page) -> Tuple[bool, str]:
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

    async def _extract_email(self, page: Page) -> str:
        """Scan page content for email addresses."""
        try:
            content = await page.content()
            emails = re.findall(
                r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", content
            )
            # Filter out Google's own addresses
            filtered = [e for e in emails if "google" not in e.lower() and "example" not in e.lower()]
            return filtered[0] if filtered else ""
        except Exception:
            return ""

    async def _extract_price_level(self, page: Page) -> int:
        """Extract $/$$/$$$/$$$$  price level. Returns 0 if unknown."""
        try:
            content = await page.content()
            m = re.search(r'aria-label="Price: ([\$]+)"', content)
            if m:
                return len(m.group(1))
            # Alternative: look for price level text
            text = await page.inner_text('div[role="main"]')
            match = re.search(r'·\s*([\$]{1,4})\s*·', text)
            if match:
                return len(match.group(1))
        except Exception:
            pass
        return 0

    async def _extract_hours(self, page: Page) -> Tuple[str, str]:
        """
        Returns (raw_hours_text, status).
        Status: "24/7" | "limited" | "temporarily_closed" | "normal"
        """
        try:
            content = await page.content()
            text = await page.inner_text('div[role="main"]')

            # Check temporarily closed
            if re.search(r"temporarily closed", text, re.IGNORECASE):
                return "Temporarily Closed", "temporarily_closed"

            # Check 24/7
            if re.search(r"open 24 hours|24/7|24 hours", text, re.IGNORECASE):
                return "Open 24/7", "24/7"

            # Try to extract hours string
            hours_el = await page.query_selector('[data-item-id="oh"] .fontBodyMedium')
            if hours_el:
                hours_text = await hours_el.inner_text()
                if hours_text:
                    # Detect limited hours (e.g. only a few days per week)
                    day_count = len(re.findall(
                        r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                        hours_text, re.IGNORECASE
                    ))
                    status = "limited" if day_count <= 3 else "normal"
                    return hours_text.strip(), status

            return "", "normal"
        except Exception:
            return "", "normal"

    async def _extract_photo_count(self, page: Page) -> int:
        """Extract number of photos from the listing."""
        try:
            text = await page.inner_text('div[role="main"]')
            m = re.search(r"([\d,]+)\s*photos?", text, re.IGNORECASE)
            if m:
                return int(m.group(1).replace(",", ""))
            # Try button aria-labels
            for btn in await page.query_selector_all('button[aria-label*="photo"]'):
                aria = await btn.get_attribute("aria-label") or ""
                m = re.search(r"([\d,]+)", aria)
                if m:
                    return int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return 0

    async def _extract_review_snippets(self, page: Page) -> Tuple[List[str], int, Optional[int]]:
        """
        Returns (snippets_list, negative_hits, days_since_last_review).
        Scrapes top review texts and scans for pain-point phrases.
        """
        snippets = []
        negative_hits = 0
        days_since = None

        try:
            # Click "Reviews" tab if available
            for sel in ['button[aria-label*="Reviews"]', 'div[role="tab"][aria-label*="Reviews"]']:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass

            # Grab review text elements
            review_els = await page.query_selector_all(
                'span[data-expandable-section] span, .wiI7pd, [class*="review-full-text"]'
            )
            for el in review_els[:15]:
                try:
                    txt = (await el.inner_text()).strip()
                    if txt and len(txt) > 20:
                        snippets.append(txt)
                except Exception:
                    pass

            # Scan for negative phrases
            combined = " ".join(snippets).lower()
            for phrase in NEGATIVE_REVIEW_PHRASES:
                if phrase in combined:
                    negative_hits += 1

            # Try to extract date from most recent review
            date_els = await page.query_selector_all(
                'span[class*="date"], .rsqaWe, [class*="review-date"], span[aria-label*="ago"]'
            )
            for el in date_els[:3]:
                try:
                    aria = await el.get_attribute("aria-label") or ""
                    text = aria or await el.inner_text()
                    days = _parse_relative_date(text)
                    if days is not None:
                        days_since = days
                        break
                except Exception:
                    pass

        except Exception:
            pass

        return snippets, negative_hits, days_since

    async def _extract_lat_lng(self, page: Page, url: str) -> Tuple[float, float]:
        """Extract coordinates from the URL or page."""
        try:
            current_url = page.url
            m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", current_url)
            if m:
                return float(m.group(1)), float(m.group(2))
            m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
            if m:
                return float(m.group(1)), float(m.group(2))
        except Exception:
            pass
        return 0.0, 0.0

    async def _scrape_nearby_competitors(self, page: Page, category: str, location: str) -> Tuple[int, int, List[str]]:
        """
        Open a second Maps search for similar businesses nearby.
        Returns (total_competitors, count_with_website, list_of_names).
        Limited to 5 nearby results to stay fast.
        """
        if not category or not self.config.scrape_competitors:
            return 0, 0, []

        names = []
        with_website = 0
        total = 0

        try:
            q = f"{category} near {location}".replace(" ", "+")
            comp_url = f"https://www.google.com/maps/search/{q}/"
            await page.goto(comp_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)

            links = await page.query_selector_all('a[href*="/maps/place/"]')
            seen_hrefs: set = set()
            unique_hrefs: list = []
            for lnk in links:
                h = (await lnk.get_attribute("href") or "").split("?")[0]
                if h and h not in seen_hrefs:
                    seen_hrefs.add(h)
                    unique_hrefs.append(h)

            # Navigate by URL — never re-use stale handles after page navigation
            for comp_href in unique_hrefs[:5]:
                try:
                    await page.goto(comp_href, wait_until="domcontentloaded", timeout=20_000)
                    await asyncio.sleep(2)
                    name = await self._try_selectors(page, [
                        'h1[class*="fontHeadlineLarge"]', '.DUwDvf', 'div[role="main"] h1'
                    ])
                    if name:
                        names.append(name)
                    has_web, _ = await self._check_website_deep(page)
                    if has_web:
                        with_website += 1
                    total += 1
                except Exception:
                    continue

        except Exception as e:
            print(f"  [Competitors] Error: {e}")

        return total, with_website, names

    async def _parse_listing(self, page: Page, href: str, context_idx: int = 0) -> Optional[Lead]:
        lead = Lead()
        t_start = time.monotonic()
        try:
            lead.google_maps_url = href

            # ── Cache check ──
            cache_key = get_cache_key(href)
            if self.config.use_cache and cache_key in self._listing_cache:
                cached = self._listing_cache[cache_key]
                for k, v in cached.items():
                    if hasattr(lead, k):
                        setattr(lead, k, v)
                job_manager.record_cache_hit(self.job_id)
                print(f"  [Cache] Hit: {lead.name}")
                self._throttle.record(time.monotonic() - t_start)
                return lead

            # Navigate directly by URL — never touch stale ElementHandles
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await self._human_delay(2.0, 3.0)
            self._throttle.record(time.monotonic() - t_start)

            # ── Name ──
            name = await self._try_selectors(page, [
                'h1[class*="fontHeadlineLarge"]', '.DUwDvf.lfPIob',
                '.DUwDvf', 'div[role="main"] h1', 'h1',
            ])
            if not name or name in self._seen_names:
                return None
            self._seen_names.add(name)
            lead.name = name

            # ── Category ──
            lead.category = await self._try_selectors(page, [
                'button[jsaction*="category"]', '.DUwDvf + span button',
            ])

            # ── Rating ──
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

            # ── Review count ──
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

            # ── Address ──
            lead.address = await self._try_selectors(page, [
                'button[data-item-id="address"] .fontBodyMedium',
                '[data-item-id="address"] div',
                'button[aria-label*="Address"]',
            ])

            # ── Phone ──
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
                raw = re.sub(r'[^\d+\s\-\(\)]', '', phone).strip()
                lead.phone_valid, lead.phone = validate_phone(raw)

            # ── Website ──
            lead.has_website, lead.website = await self._check_website_deep(page)
            if lead.website:
                lead.website_builder = detect_website_builder(lead.website)

            # ── Email ──
            lead.email = await self._extract_email(page)

            # ── Price level ──
            lead.price_level = await self._extract_price_level(page)

            # ── Hours ──
            lead.business_hours, lead.business_hours_status = await self._extract_hours(page)

            # ── Photos ──
            lead.photo_count = await self._extract_photo_count(page)

            # ── Review snippets, pain-points, velocity ──
            snippets, neg_hits, days_since = await self._extract_review_snippets(page)
            lead.negative_review_hits = neg_hits
            lead.days_since_last_review = days_since
            lead.review_snippets = " | ".join(snippets[:3])  # store top 3

            # ── Intent signals ──
            intent = detect_intent_signals(snippets)
            lead.intent_signals = ",".join(intent)

            # ── Coordinates ──
            lead.latitude, lead.longitude = await self._extract_lat_lng(page, href or "")

            # ── Country ──
            lead.country = self.config.country or self._extract_country(lead.address)

            # ── Lead score & priority ──
            lead.lead_score = compute_lead_score(
                review_count=lead.review_count,
                rating=lead.rating,
                has_website=lead.has_website,
                website_url=lead.website,
                negative_review_hits=lead.negative_review_hits,
                days_since_last_review=lead.days_since_last_review,
                competitor_count_with_website=0,   # filled after competitor scrape
                total_competitors=0,
                photo_count=lead.photo_count,
                price_level=lead.price_level,
                business_hours_status=lead.business_hours_status,
            )
            lead.priority = classify_priority(
                lead_score=lead.lead_score,
                review_count=lead.review_count,
                has_website=lead.has_website,
                website_builder=lead.website_builder,
                days_since_last_review=lead.days_since_last_review,
            )

            # ── Competitor context ──
            if self.config.scrape_competitors and lead.category:
                total_c, with_web_c, comp_names = await self._scrape_nearby_competitors(
                    page, lead.category, self.config.location
                )
                lead.competitors_total = total_c
                lead.competitors_with_website = with_web_c
                lead.competitor_names = ",".join(comp_names[:5])

                # Re-score with competitor data
                lead.lead_score = compute_lead_score(
                    review_count=lead.review_count,
                    rating=lead.rating,
                    has_website=lead.has_website,
                    website_url=lead.website,
                    negative_review_hits=lead.negative_review_hits,
                    days_since_last_review=lead.days_since_last_review,
                    competitor_count_with_website=with_web_c,
                    total_competitors=total_c,
                    photo_count=lead.photo_count,
                    price_level=lead.price_level,
                    business_hours_status=lead.business_hours_status,
                )

                # Navigate back to this listing after competitor detour
                if lead.google_maps_url:
                    await page.goto(lead.google_maps_url, wait_until="domcontentloaded", timeout=20_000)
                    await asyncio.sleep(1.5)

            # ── Cache write ──
            if self.config.use_cache and cache_key:
                self._listing_cache[cache_key] = asdict(lead)
                self._cache_dirty = True

            return lead

        except PlaywrightTimeout:
            return None
        except Exception as e:
            print(f"Parse error: {e}")
            return None

    def _extract_country(self, address: str) -> str:
        if not address:
            return ""
        parts = address.split(",")
        return parts[-1].strip() if parts else ""

    def _is_qualified(self, lead: Lead) -> bool:
        cfg = self.config
        if cfg.no_website_only and lead.has_website and not lead.website_builder:
            return False
        if lead.rating < cfg.min_rating:
            return False
        if lead.review_count < cfg.min_reviews:
            return False
        if cfg.require_phone and not lead.phone:
            return False
        return True

    def _is_duplicate(self, lead: Lead) -> bool:
        """Exact key check + fuzzy name+address check."""
        key = make_lead_key(asdict(lead))
        if key in self._seen_keys:
            return True
        return is_fuzzy_duplicate(asdict(lead), self._seen_leads_list)

    async def run(self):
        cfg = self.config
        jid = self.job_id

        n_contexts = max(1, min(cfg.parallel_contexts, 3))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"]
            )

            # Create N contexts with varied fingerprints
            contexts = []
            for i in range(n_contexts):
                ctx = await browser.new_context(
                    viewport=VIEWPORTS[i % len(VIEWPORTS)],
                    user_agent=USER_AGENTS[i % len(USER_AGENTS)],
                    locale="en-US",
                )
                page = await ctx.new_page()
                await page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                contexts.append((ctx, page))

            # Use the first context for the main search/scroll
            main_page = contexts[0][1]
            url = self._build_search_url()
            job_manager.update_job(jid, status="running", current_business="Loading Google Maps…")
            await sse_manager.broadcast(jid, job_manager.get_job(jid))

            await main_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await self._human_delay(2, 3)

            for sel in ['button[aria-label*="Accept all"]', 'button[aria-label*="Accept"]',
                        'button[jsname="b3VHJd"]']:
                try:
                    btn = await main_page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._human_delay(1, 2)
                        break
                except Exception:
                    pass

            job_manager.update_job(jid, current_business="Scrolling for listings…")
            await sse_manager.broadcast(jid, job_manager.get_job(jid))
            await self._scroll_results(main_page, cfg.max_results)

            links = await main_page.query_selector_all('a[href*="/maps/place/"]')
            unique_hrefs, seen_hrefs = [], set()
            for lnk in links:
                h = (await lnk.get_attribute("href") or "").split("?")[0]
                if h and h not in seen_hrefs:
                    seen_hrefs.add(h)
                    unique_hrefs.append(h)

            total = min(len(unique_hrefs), cfg.max_results)
            job_manager.update_job(jid, total=total, progress=5)
            await sse_manager.broadcast(jid, job_manager.get_job(jid))

            # ── Parallel processing via semaphore ──
            sem = asyncio.Semaphore(n_contexts)
            context_queue: asyncio.Queue = asyncio.Queue()
            for ctx_tuple in contexts:
                await context_queue.put(ctx_tuple)

            async def process_one(i: int, href: str):
                async with sem:
                    ctx_tuple = await context_queue.get()
                    _, page = ctx_tuple
                    try:
                        progress = 5 + int((i / total) * 90)
                        job_manager.update_job(
                            jid, processed=i, progress=progress,
                            current_business=f"Scanning listing {i} of {total}…"
                        )
                        await sse_manager.broadcast(jid, job_manager.get_job(jid))

                        lead = await self._parse_listing(page, href, i % n_contexts)
                        if lead is None:
                            job_manager.jobs[jid]["stats"]["skipped_errors"] += 1
                            job_manager.add_log(jid, "error", f"Listing #{i}", "Parse failed / timeout")
                            return

                        if self._is_duplicate(lead):
                            job_manager.record_dupe(jid)
                            job_manager.add_log(jid, "dupe", lead.name)
                            print(f"  [DUPE] Skipping: {lead.name}")
                            return

                        if self._is_qualified(lead):
                            self.leads.append(lead)
                            job_manager.add_lead(jid, lead)
                            job_manager.add_log(jid, "lead", lead.name,
                                                f"{lead.priority} · score {lead.lead_score}")
                            key = make_lead_key(asdict(lead))
                            self._seen_keys.add(key)
                            self._seen_leads_list.append(asdict(lead))

                            append_to_csv(
                                self._csv_path,
                                lead,
                                write_header=not self._csv_header_written
                            )
                            self._csv_header_written = True

                            if cfg.push_to_sheets and GAS_URL:
                                # Fire-and-forget — never blocks scraping
                                asyncio.create_task(
                                    push_lead_to_sheets_bg(jid, lead, GAS_URL, GAS_ADMIN_CODE)
                                )

                            print(f"  [{lead.priority}|{lead.lead_score}] {lead.name}")
                        else:
                            job_manager.record_filtered(jid, lead, cfg)
                            job_manager.add_log(jid, "filtered", lead.name,
                                                f"rating {lead.rating} · {lead.review_count} reviews")

                        await sse_manager.broadcast(jid, job_manager.get_job(jid))
                        await self._throttle.sleep()

                    finally:
                        await context_queue.put(ctx_tuple)

            tasks = [
                asyncio.create_task(process_one(i, href))
                for i, href in enumerate(unique_hrefs[:total], 1)
            ]
            await asyncio.gather(*tasks)

            for ctx, _ in contexts:
                await ctx.close()
            await browser.close()

        # Persist dedup keys and listing cache
        save_seen_leads(self._seen_keys)
        if self._cache_dirty:
            save_listing_cache(self._listing_cache)

        job_manager.update_job(jid, status="done", progress=100, current_business="")
        await sse_manager.broadcast(jid, job_manager.get_job(jid))
        return self.leads


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_relative_date(text: str) -> Optional[int]:
    """Convert '3 days ago', '2 weeks ago', 'a month ago' → integer days."""
    text = text.lower().strip()
    if not text:
        return None
    # "X days/weeks/months/years ago"
    m = re.search(r"(\d+|a|an)\s+(day|week|month|year)s?\s+ago", text)
    if not m:
        return None
    qty_str, unit = m.group(1), m.group(2)
    qty = 1 if qty_str in ("a", "an") else int(qty_str)
    multipliers = {"day": 1, "week": 7, "month": 30, "year": 365}
    return qty * multipliers.get(unit, 1)


# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="LeadHunt API v3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\n  GAS_URL: {'✓ Set' if GAS_URL else '✗ Not set (Sheets push disabled)'}")
    print(f"  Dedup registry: {len(load_seen_leads())} known leads")
    print(f"  Listing cache: {len(load_listing_cache())} entries")
    print(f"  Parallel contexts: {PARALLEL_CONTEXTS}\n")


@app.post("/api/scrape")
async def start_scrape(request: ScrapeRequest):
    job_id = job_manager.create_job()
    job_manager.update_job(job_id, status="starting")
    asyncio.create_task(run_scrape_job(job_id, request))
    return {"job_id": job_id, "status": "started"}


async def run_scrape_job(job_id: str, config: ScrapeRequest):
    seen_keys = load_seen_leads()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"leads_{ts}.csv"
    try:
        scraper = GoogleMapsLeadScraper(config, job_id, seen_keys, csv_path)
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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = OUTPUT_DIR / f"leads_{ts}_download.csv"
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=leads[0].keys())
        writer.writeheader()
        writer.writerows(leads)
    return FileResponse(filepath, media_type="text/csv", filename=filepath.name)


@app.get("/api/dedup/stats")
async def dedup_stats():
    seen = load_seen_leads()
    return {"total_known_leads": len(seen)}


@app.delete("/api/dedup/reset")
async def reset_dedup():
    if SEEN_LEADS_FILE.exists():
        SEEN_LEADS_FILE.unlink()
    return {"reset": True}


@app.delete("/api/cache/reset")
async def reset_cache():
    """Clear the listing cache to force fresh scrapes."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    return {"reset": True}


@app.get("/api/cache/stats")
async def cache_stats():
    cache = load_listing_cache()
    return {"total_cached_listings": len(cache)}


@app.get("/api/leads/{job_id}/hot")
async def get_hot_leads(job_id: str):
    """Return only Hot-priority leads for a job."""
    job = job_manager.get_job(job_id)
    hot = [l for l in job.get("leads", []) if l.get("priority") == "Hot"]
    return {"count": len(hot), "leads": hot}


@app.get("/api/leads/{job_id}/by_priority")
async def leads_by_priority(job_id: str):
    """Return leads grouped and sorted by priority tier and score."""
    job = job_manager.get_job(job_id)
    all_leads = job.get("leads", [])
    order = {"Hot": 0, "Warm": 1, "Cold": 2, "Educate": 3}
    sorted_leads = sorted(
        all_leads,
        key=lambda l: (order.get(l.get("priority", "Cold"), 99), -l.get("lead_score", 0))
    )
    grouped: dict = {"Hot": [], "Warm": [], "Cold": [], "Educate": []}
    for lead in sorted_leads:
        p = lead.get("priority", "Cold")
        grouped.setdefault(p, []).append(lead)
    return {"priority_counts": job.get("priority_counts", {}), "leads": grouped}


if __name__ == "__main__":
    print("\n🚀 LeadHunt Server v3 starting…")
    print("   API  → http://localhost:8000")
    print("   Docs → http://localhost:8000/docs")
    print("   Open → index.html in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")