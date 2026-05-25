"""Google Ads scraper — two-step ATC extraction via Firecrawl.

Step 1: Fetch the competitor's ATC page to extract creative detail URLs.
Step 2: Fetch each creative detail page to extract headline, description,
        displayed link, and other fields.

Public entry point:
    scrape_google_ads(competitors, *, mode="qa_expanded") -> Dict
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from app.config.constants import DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "google_ads_scraper.log")

ADS_DIR = DATA_DIR / "ads"
ADS_DIR.mkdir(parents=True, exist_ok=True)

_BASE_ATC = "https://adstransparency.google.com"
_FIRECRAWL_V1 = "https://api.firecrawl.dev/v1/scrape"

# ---------------------------------------------------------------------------
# Competitor registry
# ---------------------------------------------------------------------------

COMPETITORS: List[Dict] = [
    {"name": "Midas",                     "domain": "midas.com"},
    {"name": "Lube Town",                 "domain": "lubetown.com"},
    {"name": "Jiffy Lube",                "domain": "jiffylubeservice.ca"},
    {"name": "Great Canadian Oil Change", "domain": "gcoc.ca"},
    {"name": "Quick Lane",                "domain": "quicklane.com"},
    {"name": "Valvoline",                 "advertiser_id": "AR06652422686691557377"},
    {"name": "Econo Lube",               "domain": "econolube.ca"},
    {"name": "Lube FX Plus",             "domain": "lubefx.com"},
    {"name": "Mr. Lube + Tires",         "domain": "mrlube.com"},
]


def _build_url(competitor: Dict) -> str:
    aid = competitor.get("advertiser_id")
    if aid:
        return f"{_BASE_ATC}/advertiser/{aid}?region=CA"
    return f"{_BASE_ATC}/?region=CA&domain={competitor['domain']}"


# ---------------------------------------------------------------------------
# Firecrawl helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
    return os.getenv("FIRECRAWL_API_KEY", "")


def _firecrawl_fetch(url: str, wait_ms: int = 6000, scroll: bool = True) -> Dict:
    api_key = _get_api_key()
    if not api_key:
        return {"html": "", "markdown": "", "error": "FIRECRAWL_API_KEY not set"}

    actions = [{"type": "wait", "milliseconds": wait_ms}]
    if scroll:
        actions += [
            {"type": "scroll", "direction": "down", "amount": 500},
            {"type": "wait", "milliseconds": 2000},
        ]

    payload = {
        "url": url,
        "onlyMainContent": False,
        "formats": ["html"],
        "waitFor": wait_ms,
        "actions": actions,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(_FIRECRAWL_V1, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
    except Exception as exc:
        return {"html": "", "error": str(exc)}

    data = resp.json().get("data", resp.json())
    return {"html": data.get("html", "") or "", "error": None}


# ---------------------------------------------------------------------------
# Step 1 — extract creative detail URLs from the main ATC page
# ---------------------------------------------------------------------------

_CREATIVE_URL_RE = re.compile(
    r"https://adstransparency\.google\.com/advertiser/[^\"'\s>]+/creative/[^\"'\s>]+"
)


def _get_creative_urls(atc_url: str) -> List[str]:
    """Fetch the ATC landing page and return all creative detail page URLs."""
    res = _firecrawl_fetch(atc_url, wait_ms=6000, scroll=True)
    if res.get("error") or not res["html"]:
        logger.warning(f"[ads] Failed to fetch ATC page: {atc_url}")
        return []

    urls = _CREATIVE_URL_RE.findall(res["html"])
    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    logger.info(f"[ads] Found {len(unique)} creative URLs at {atc_url}")
    return unique


# ---------------------------------------------------------------------------
# Step 2 — extract ad content from a creative detail page
# ---------------------------------------------------------------------------

_NOISE_LINES = {
    "ads transparency center", "go to ads transparency center home page.",
    "sign in", "all topics", "political ads", "find the ads you've seen",
    "advertiser details", "ad details", "all platforms", "all formats",
    "any time", "see more results", "see all ads", "visit site", "directions",
    "website", "report this ad", "our ad policies", "privacy", "terms",
    "ads policies", "principles", "ads blog", "main menu", "google apps",
    "shown in canada", "last shown:", "format:", "see more ads by this advertiser",
    "sponsored", "verified", "chevron_left", "chevron_right", "arrow_forward",
    "arrow_drop_down", "keyboard_arrow_right", "how_to_reg", "calendar_today",
    "dismiss", "learn more", "cancel", "apply", "hide_image",
    "advertiser has verified their identity",
    "some advertisers show ads with age restricted content.",
    "the information about this ad may vary by location",
}
_NOISE_PREFIX_RE = re.compile(
    r"^(local ad rendering|moroch|redistricting|the boundaries|removed for a|"
    r"sorry, we|our ad policies|prohibited|restricted content|editorial|"
    r"legal name:|based in:|~\d|\d+ ads|plus$|1 of \d|sign in to)",
    re.IGNORECASE,
)
_TEMPLATE_RE = re.compile(r"<[A-Z]")
_ICON_RE = re.compile(r"^[a-z_]+$")
# Matches store-hours strings like "8 AM–6 PM" or "Open 24 hours"
_HOURS_RE = re.compile(r"\d+\s*(AM|PM|am|pm)|open\s+\d+|closes?\s+at", re.I)


def _is_noise(text: str) -> bool:
    t = text.strip()
    if not t or len(t) < 4:
        return True
    if t.lower() in _NOISE_LINES:
        return True
    if _TEMPLATE_RE.match(t):
        return True
    if _ICON_RE.match(t):
        return True
    if _NOISE_PREFIX_RE.match(t):
        return True
    if _HOURS_RE.search(t):
        return True
    return False


_DOMAIN_RE = re.compile(r"^(www\.[\w.-]+\.\w{2,6}|[\w-]+\.(ca|com|net|org)(/\S*)?)$", re.I)
# Unicode bidi control characters inserted by ATC around ad copy
_BIDI_RE = re.compile(r"[⁦⁧⁨⁩‪-‮  ]")
_URL_PATH_RE = re.compile(r"^/\S+$")  # bare URL paths like /oil_change
_PHONE_RE = re.compile(r"\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b|call\s+\(?\d", re.I)


def _strip_bidi(text: str) -> str:
    return _BIDI_RE.sub("", text).strip()


def _parse_creative_page(html: str) -> Optional[Dict]:
    """Extract ad_title, ad_description, and displayed_link from a creative detail page."""
    soup = BeautifulSoup(html, "html.parser")
    # Strip bidi control characters before any processing
    texts = [_strip_bidi(t) for t in soup.stripped_strings if _strip_bidi(t)]

    if not texts:
        return None

    lower_texts = [t.lower() for t in texts]

    # displayed_link: first domain-like line in the original text list
    displayed_link = ""
    for t in texts:
        if _DOMAIN_RE.match(t) and len(t) < 80:
            displayed_link = t
            break

    # Restrict search to the creative detail panel only.
    # The page has TWO "Ad details" nodes: a nav breadcrumb near the top and
    # the actual panel header.  "See all ads" appears immediately before the
    # real panel, so use it as a reliable start marker.
    see_all_idx = next(
        (i for i, t in enumerate(lower_texts) if t == "see all ads"),
        -1,
    )
    if see_all_idx == -1:
        # Fallback: last occurrence of "ad details"
        all_ad_details = [i for i, t in enumerate(lower_texts) if t == "ad details"]
        see_all_idx = all_ad_details[-1] if all_ad_details else 0

    see_more_idx = next(
        (i for i, t in enumerate(lower_texts) if "see more ads" in t),
        len(texts),
    )

    # Search for "Visit site" in the ORIGINAL (unfiltered) texts inside the panel.
    # "visit site" is in _NOISE_LINES so it would be removed from `clean` —
    # searching original texts avoids that.
    visit_indices = [
        i for i, t in enumerate(lower_texts)
        if t in ("visit site", "visit\xa0site")
        and i > see_all_idx
        and i < see_more_idx
    ]

    if not visit_indices:
        # Fallback for call/single-ad format ads (no "Visit site" button).
        # These use "Single Ad Rendering Service" as the content header.
        single_idx = next(
            (i for i, t in enumerate(lower_texts)
             if t == "single ad rendering service" and i > see_all_idx and i < see_more_idx),
            -1,
        )
        if single_idx == -1:
            return None
        content: List[str] = []
        j = single_idx + 1
        while j < see_more_idx and len(content) < 2:
            candidate = texts[j]
            if (len(candidate) >= 8
                    and not _is_noise(candidate)
                    and not _DOMAIN_RE.match(candidate)
                    and not _URL_PATH_RE.match(candidate)
                    and not _PHONE_RE.search(candidate)
                    and "·" not in candidate):
                content.append(candidate)
            j += 1
        if not content:
            return None
        return {
            "ad_title":       content[0],
            "ad_description": content[1] if len(content) > 1 else "",
            "displayed_link": displayed_link,
        }

    # Use the last "Visit site" in the panel (multiple variations may exist).
    target_visit = visit_indices[-1]

    # Collect up to 2 content lines going backwards from "Visit site"
    content: List[str] = []
    j = target_visit - 1
    while j >= 0 and len(content) < 2:
        candidate = texts[j]
        if (len(candidate) >= 8
                and not _is_noise(candidate)
                and not _DOMAIN_RE.match(candidate)
                and "·" not in candidate
                and candidate.lower() not in ("call", "directions", "website")):
            content.insert(0, candidate)
        j -= 1

    if not content:
        return None

    return {
        "ad_title":       content[0],
        "ad_description": content[1] if len(content) > 1 else "",
        "displayed_link": displayed_link,
    }


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

_DISCOUNT_RE = re.compile(
    r"(\$\s*\d+(?:\.\d+)?(?:\s*off)?|\d+\s*%\s*off|free\s+\w+|buy\s+\d+\s+get\s+\d+)",
    re.IGNORECASE,
)


def _discount(title: str, desc: str) -> str:
    for text in (title, desc):
        m = _DISCOUNT_RE.search(text)
        if m:
            return m.group(0).strip()
    return ""


# ---------------------------------------------------------------------------
# Per-competitor scraper
# ---------------------------------------------------------------------------

def _scrape_one_competitor(competitor: Dict) -> Dict:
    name = competitor["name"]
    domain = competitor.get("domain", "")
    atc_url = _build_url(competitor)
    today = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"[ads] {name}: Step 1 — getting creative URLs from {atc_url}")
    creative_urls = _get_creative_urls(atc_url)

    if not creative_urls:
        logger.warning(f"[ads] {name}: no creative URLs found")
        return {"competitor": name, "url": atc_url, "status": "no_creatives", "ads": []}

    MAX_CREATIVES = 15
    if len(creative_urls) > MAX_CREATIVES:
        logger.info(f"[ads] {name}: capping at {MAX_CREATIVES} of {len(creative_urls)} creatives")
        creative_urls = creative_urls[:MAX_CREATIVES]

    logger.info(f"[ads] {name}: Step 2 — fetching {len(creative_urls)} creative pages")
    enriched: List[Dict] = []
    seen_titles: set = set()

    for creative_url in creative_urls:
        res = _firecrawl_fetch(creative_url, wait_ms=6000, scroll=False)
        if res.get("error") or not res["html"]:
            logger.debug(f"[ads] {name}: failed to fetch {creative_url}")
            continue

        ad = _parse_creative_page(res["html"])
        if not ad:
            logger.debug(f"[ads] {name}: no ad content parsed from {creative_url}")
            continue

        title = ad["ad_title"]
        desc = ad["ad_description"]
        key = title.lower()[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)

        enriched.append({
            "business_name":  name,
            "ad_title":       title,
            "ad_description": desc,
            "discount_value": _discount(title, desc),
            "ad_link":        creative_url,
            "displayed_link": ad["displayed_link"] or domain,
            "date_scraped":   today,
        })
        time.sleep(1)  # polite pause between creative page fetches

    logger.info(f"[ads] {name}: {len(enriched)} unique ads extracted")
    return {
        "competitor": name,
        "url":        atc_url,
        "status":     "ok" if enriched else "no_ads",
        "ads":        enriched,
        "creatives_found": len(creative_urls),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def scrape_google_ads(
    competitors: Optional[List[Dict]] = None,
    *,
    mode: str = "qa_expanded",
) -> Dict:
    if competitors is None:
        competitors = COMPETITORS

    all_ads: List[Dict] = []
    url_log: List[Dict] = []

    for comp in competitors:
        result = _scrape_one_competitor(comp)
        all_ads.extend(result["ads"])
        url_log.append({
            "competitor":       result["competitor"],
            "url":              result["url"],
            "status":           result["status"],
            "ads_found":        len(result["ads"]),
            "creatives_found":  result.get("creatives_found", 0),
        })
        time.sleep(2)

    if mode == "final_deduped":
        seen: set = set()
        deduped: List[Dict] = []
        for ad in all_ads:
            key = (ad["business_name"], ad["ad_title"].lower()[:80])
            if key not in seen:
                seen.add(key)
                deduped.append(ad)
        all_ads = deduped

    by_competitor: Dict[str, int] = {}
    for ad in all_ads:
        c = ad["business_name"]
        by_competitor[c] = by_competitor.get(c, 0) + 1

    result_out = {
        "scraped_at":    datetime.now().isoformat(),
        "mode":          mode,
        "ads":           all_ads,
        "count":         len(all_ads),
        "by_competitor": by_competitor,
        "validation": {
            "competitor_count":     len(competitors),
            "competitors_with_ads": sum(1 for e in url_log if e["status"] == "ok"),
            "competitors_no_ads":   sum(1 for e in url_log if e["status"] == "no_ads"),
            "total_ads_extracted":  len(all_ads),
            "url_log":              url_log,
        },
    }

    output_file = ADS_DIR / "google_ads.json"
    output_file.write_text(json.dumps(result_out, indent=2, default=str), encoding="utf-8")
    logger.info(f"[ads] Saved {len(all_ads)} ads to {output_file}")
    return result_out
