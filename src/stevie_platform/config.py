"""
Configuration for the Stevie Platform acquisition pipeline.

Endpoints, selectors and the label map below are CONFIRMED against the live
site (verified in the StevieIntel reference repo, 2026-06) and re-used here as
research — not as inherited code. The site is a Drupal Views exposed-filter
form (GET) gated by a *math* question (not a real CAPTCHA), with a captcha-free
fast-path for detail pages.

Phase 1 reuses only what acquisition needs. LLM / RAG / dashboard / worker
config from the reference is intentionally out of scope here.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # optional: keep config (and the pure parser) importable without runtime deps
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

BASE_DIR = Path(__file__).resolve().parents[2]

# --- Database ---------------------------------------------------------------
# Use 127.0.0.1, not "localhost": localhost can resolve to IPv6 ::1 first, on
# which the async pool hangs because the Dockerized Postgres only listens on
# IPv4 (0.0.0.0:5432). Forcing IPv4 avoids that intermittent connect stall.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://stevie:stevie@127.0.0.1:5432/stevie_platform"
)

# --- Pipeline / operational --------------------------------------------------
# The target site's tolerance is an OPERATIONAL setting, not an architectural
# constant. Stevie's WAF throttles parallel harvest+fetch from one IP (verified
# empirically: 33×HTTP403 in 90s), so default to sequential. An API-backed source
# could set 'parallel'. In sequential mode harvest/fetch hold a shared advisory
# lock so they can't accidentally overlap.
PIPELINE_MODE = os.environ.get("STEVIE_PIPELINE_MODE", "sequential")  # sequential | parallel
# Single knob for the politeness ceiling; drives the detail-fetch rate limiter.
MAX_GLOBAL_RPS = float(os.environ.get("STEVIE_MAX_GLOBAL_RPS", "2.0"))  # empirically sustainable; 3.0 trips the site's throttle
NETWORK_LOCK_KEY = 770042  # advisory-lock id for "one network stage at a time"


def _load_proxies() -> list[str]:
    """Proxy pool for the detail fetch (http://user:pass@host:port per line).
    From STEVIE_PROXIES (comma-sep) or proxies.txt in the repo root. Empty =
    direct connection. Each proxy = its own rate-limited 'lane', so the IP
    throttle is dodged by spreading load across many exit IPs."""
    env = os.environ.get("STEVIE_PROXIES")
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    f = BASE_DIR / "proxies.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    return []


PROXIES = _load_proxies()
# Per-proxy politeness (req/s). Each exit IP gets its own ~1 req/s budget, so
# total throughput ≈ PER_PROXY_RPS × len(PROXIES). Lower if proxies see 403s.
PER_PROXY_RPS = float(os.environ.get("STEVIE_PER_PROXY_RPS", "1.0"))

# --- Endpoints --------------------------------------------------------------
ORIGIN = "https://stevieawards.com"
SEARCH_PATH = "/search-past-winners-and-finalists"
TARGET_URL = ORIGIN + SEARCH_PATH
# Fast-path detail endpoint. {id} == the row's `rel` attr (Drupal node id).
# Returns HTTP 200 with NO cookies / session / captcha and the full record.
DETAIL_URL = ORIGIN + "/past-winners-and-finalists/view-details/{id}"

# Listing dropdown maxes at 60 rows/page; Drupal `?page=` is ZERO-indexed.
ITEMS_PER_PAGE = 60
LISTING_QUERY = {
    "site_type": "", "year": "", "company_name": "", "award": "",
    "nomination_title": "", "country": "", "category_group": "", "state": "",
    "category": "", "city": "", "submitted_by": "", "industry": "",
    "items_per_page": str(ITEMS_PER_PAGE),
}

# --- Pacing / anti-ban (proven safe on a single IP) -------------------------
HEADLESS = True
BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
DELAY_BETWEEN_LISTING_PAGES = (1.5, 4.0)   # Playwright harvest pacing (seconds)

DETAIL_CONCURRENCY = int(os.environ.get("STEVIE_DETAIL_CONCURRENCY", "6"))
# AIMD adaptive pacing for the detail fast-path: ~1.3 req/s is the proven-safe
# ceiling on a single IP; ramp toward it, back off hard on 403/429.
RATE_START_GAP = 1.0 / MAX_GLOBAL_RPS  # seconds between request starts (from the RPS knob)
RATE_MIN_GAP = 1.0 / MAX_GLOBAL_RPS    # floor — never overshoot the configured ceiling
RATE_MAX_GAP = 2.5     # cap the penalty: a 403 must not strand us at 0.12 req/s
RATE_GAP_STEP = 0.10   # recover faster after a clean streak (was 0.05 = too slow)
RATE_BACKOFF = 1.5     # gentler backoff so one block doesn't collapse throughput
RATE_SPEEDUP_AFTER = 8 # start speeding back up sooner
RETRY_HTTP_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}

NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 20_000
HTTP_TIMEOUT_S = 30.0
MAX_PAGE_ATTEMPTS = 4
MAX_DETAIL_ATTEMPTS = 4
RETRY_BACKOFF_BASE = 2.0    # seconds; exponential per attempt on a slow/blocked page

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
VIEWPORT = {"width": 1440, "height": 900}

# --- Selectors (CONFIRMED against the live DOM) -----------------------------
SELECTORS = {
    "form":              'form[id^="views-exposed-form-sa-past-winners"]',
    # The math question ("17 + 3 =") is a BARE TEXT NODE in this container.
    "captcha_container": ".form-item-captcha-response",
    "captcha_input":     "#edit-captcha-response",
    "items_per_page":    "#edit-items-per-page",
    "apply_button":      "#edit-submit-sa-past-winners-and-finalists",
    "result_rows":       "table.views-table tbody tr",
    "detail_trigger":    "a.a-view-past-winner-details",   # rel = node id
}

# Detail-page label text -> structured field. Matched lowercased, stripped,
# trailing ':' removed.
MODAL_LABEL_MAP = {
    "organization name": "organization_name",
    "year": "year",
    "award programs": "award_programs",
    "award": "award",
    "category": "category",
    "category group": "category_group",
    "industry": "industry",
    "city": "city",
    "state/province": "state_province",
    "country": "country",
    "submitting agency": "submitting_agency",
    "notes": "notes",
}
