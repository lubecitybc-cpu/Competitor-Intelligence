"""Google Ads scraper — SerpAPI-based ATC extraction.

Step 1: Call google_ads_transparency_center with advertiser_id → get list of text creatives.
Step 2: For each text creative (up to MAX_PER_COMPETITOR), call
        google_ads_transparency_center_ad_details → extract title, snippet, visible_link.

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
from dotenv import load_dotenv

from app.config.constants import DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "google_ads_scraper.log")

ADS_DIR = DATA_DIR / "ads"
ADS_DIR.mkdir(parents=True, exist_ok=True)

_SERPAPI_BASE = "https://serpapi.com/search"
MAX_PER_COMPETITOR = 3

# ---------------------------------------------------------------------------
# Competitor registry
# ---------------------------------------------------------------------------

COMPETITORS: List[Dict] = [
    # advertiser_ids: list of AR IDs to try (national/agency level).
    # brand_keywords: at least one must appear in title, snippet, or visible_link
    #   (case-insensitive) otherwise the ad is skipped as unrelated.
    {"name": "Midas",                     "advertiser_ids": ["AR04579314025283715073"],
     "brand_keywords": ["midas"]},
    {"name": "Lube Town",                 "advertiser_ids": ["AR07923297702782173185"],
     "brand_keywords": ["lube town", "lubetown"]},
    {"name": "Jiffy Lube",                "advertiser_ids": ["AR18032045877266219009"],
     "brand_keywords": ["jiffy lube", "jiffylube"]},
    {"name": "Great Canadian Oil Change", "advertiser_ids": ["AR06652422686691557377"],
     "brand_keywords": ["great canadian", "gcoc"]},
    {"name": "Quick Lane",                "advertiser_ids": ["AR01146826087020363777", "AR08559186921127936001", "AR17827749435640119297"],
     "brand_keywords": ["quick lane", "quicklane"]},
    {"name": "Valvoline",                 "advertiser_ids": ["AR06652422686691557377"],
     "brand_keywords": ["valvoline"]},
    {"name": "Econo Lube",               "advertiser_ids": ["AR03728714169130680321"],
     "brand_keywords": ["econo lube", "econolube"]},
    {"name": "Lube FX Plus",             "advertiser_ids": ["AR07682908778362044417"],
     "brand_keywords": ["lube fx", "lubefx"]},
    {"name": "Mr. Lube + Tires",         "advertiser_ids": ["AR00685141515294474241"],
     "brand_keywords": ["mr. lube", "mr lube", "mrlube"]},
]


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
    return os.getenv("SERPAPI_KEY", "")


# ---------------------------------------------------------------------------
# SerpAPI helpers
# ---------------------------------------------------------------------------

def _serpapi_get(params: Dict) -> Dict:
    """Make a GET request to SerpAPI and return the parsed JSON."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "SERPAPI_KEY not set"}
    params = {**params, "api_key": api_key}
    try:
        resp = requests.get(_SERPAPI_BASE, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


def _get_text_creatives(advertiser_id: str) -> List[Dict]:
    """Return a list of text-format creative dicts for the advertiser."""
    data = _serpapi_get({
        "engine": "google_ads_transparency_center",
        "advertiser_id": advertiser_id,
    })
    if "error" in data:
        logger.warning(f"[ads] SerpAPI error for {advertiser_id}: {data['error']}")
        return []
    creatives = data.get("ad_creatives", [])
    text_creatives = [c for c in creatives if c.get("format", "").lower() == "text"]
    logger.info(
        f"[ads] {advertiser_id}: {len(creatives)} total creatives, "
        f"{len(text_creatives)} text"
    )
    return text_creatives


def _get_ad_details(advertiser_id: str, creative_id: str) -> Optional[Dict]:
    """Fetch ad details and return the first item with a title and snippet."""
    data = _serpapi_get({
        "engine": "google_ads_transparency_center_ad_details",
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
    })
    if "error" in data:
        logger.warning(f"[ads] Details error for {creative_id}: {data['error']}")
        return None

    for item in data.get("ad_creatives", []):
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if title and snippet:
            return {
                "ad_title":       title,
                "ad_description": snippet,
                "displayed_link": (item.get("visible_link") or "").strip(),
            }
    return None


# ---------------------------------------------------------------------------
# Discount extractor
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

def _is_brand_match(ad: Dict, keywords: List[str]) -> bool:
    """Return True if any brand keyword appears in title, description, or displayed_link."""
    haystack = " ".join([
        ad.get("ad_title", ""),
        ad.get("ad_description", ""),
        ad.get("displayed_link", ""),
    ]).lower()
    return any(kw in haystack for kw in keywords)


def _scrape_one_competitor(competitor: Dict) -> Dict:
    name = competitor["name"]
    ar_ids: List[str] = competitor.get("advertiser_ids") or [competitor.get("advertiser_id", "")]
    ar_ids = [a for a in ar_ids if a]
    brand_keywords: List[str] = [k.lower() for k in competitor.get("brand_keywords", [])]
    today = datetime.now().strftime("%Y-%m-%d")
    primary_url = f"https://adstransparency.google.com/advertiser/{ar_ids[0]}" if ar_ids else ""

    enriched: List[Dict] = []
    seen_titles: set = set()
    total_creatives = 0

    for advertiser_id in ar_ids:
        if len(enriched) >= MAX_PER_COMPETITOR:
            break

        logger.info(f"[ads] {name} ({advertiser_id}): fetching text creatives")
        text_creatives = _get_text_creatives(advertiser_id)
        total_creatives += len(text_creatives)

        fetched = 0
        for creative in text_creatives:
            if len(enriched) >= MAX_PER_COMPETITOR or fetched >= MAX_PER_COMPETITOR:
                break

            creative_id = creative.get("ad_creative_id", "")
            if not creative_id:
                continue

            ad = _get_ad_details(advertiser_id, creative_id)
            fetched += 1
            time.sleep(0.5)

            if not ad:
                logger.debug(f"[ads] {name}: no content in {creative_id}")
                continue

            # Skip ads that don't mention the brand anywhere
            if brand_keywords and not _is_brand_match(ad, brand_keywords):
                logger.debug(f"[ads] {name}: skipping unrelated ad {ad['ad_title']!r}")
                continue

            title = ad["ad_title"]
            key = title.lower()[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)

            enriched.append({
                "business_name":  name,
                "ad_title":       title,
                "ad_description": ad["ad_description"],
                "discount_value": _discount(title, ad["ad_description"]),
                "ad_link":        creative.get("details_link", primary_url),
                "displayed_link": ad["displayed_link"],
                "date_scraped":   today,
            })

    logger.info(f"[ads] {name}: {len(enriched)} unique ads extracted")
    return {
        "competitor":      name,
        "url":             primary_url,
        "status":          "ok" if enriched else "no_ads",
        "ads":             enriched,
        "creatives_found": total_creatives,
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
            "competitor":      result["competitor"],
            "url":             result["url"],
            "status":          result["status"],
            "ads_found":       len(result["ads"]),
            "creatives_found": result.get("creatives_found", 0),
        })
        time.sleep(1)

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
