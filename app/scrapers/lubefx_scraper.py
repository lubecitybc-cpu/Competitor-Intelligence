"""LubeFx Plus v2 scraper (Edmonton, city_store).

Spec:
  - Cities          : Edmonton only
  - Source scope    : city_store
  - URLs            :
        https://lubefx.com/lubefx-coupons/   (image_ocr + text)
        https://lubefx.com/lubefx-rewards/   (text-only, strict qualification)
  - Taxonomy        : Battery, Oil Change, Brake, Tire Sales, Tire Rotation,
                      Transmission Fluid, Radiator Flush, Fuel System Flush,
                      Other.
  - Every kept row:
        city               = "Edmonton"
        store_name         = "LubeFx Plus"
        location           = "Edmonton"
        applicable_cities  = ["Edmonton"]
  - Real-offer signal required (discount/coupon/promo/rebate/save/free/bonus/
    expires/limited-time/financing/package price/concrete reward).
  - OCR failure on a coupon image -> needs_review row with source_image.
  - Rewards page: only concrete benefits — no generic loyalty marketing copy.

Public entry point:
    scrape_lubefx_v2(competitor_v2, *, mode="qa_expanded", enable_ocr=True) -> Dict
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from app.config.constants import DATA_DIR, IMAGES_DIR
from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.images.image_downloader import normalize_url
from app.extractors.ocr.ocr_processor import ocr_image
from app.utils.logging_utils import setup_logger
from app.utils.service_classifier import classify_service
from app.scrapers.jiffy_scraper import (
    _v2_extract_discount,
    _v2_extract_coupon_code,
    _summarize_promo_description,
    _normalize_discount,
    _confidence_from_promo,
    _signature_meaningful_tokens,
)

logger = setup_logger(__name__, "lubefx_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BUSINESS_NAME = "LubeFx Plus"
_WEBSITE = "lubefx.com"
_CITY = "Edmonton"
_STORE = "LubeFx Plus"
_LOCATION = "Edmonton"
_SOURCE_SCOPE = "city_store"

_ALLOWED_SERVICES = frozenset({
    "Battery", "Oil Change", "Brake", "Tire Sales", "Tire Rotation",
    "Transmission Fluid", "Radiator Flush", "Fuel System Flush", "Other",
})

# Loose real-offer signal — first-pass gate that lets a candidate proceed.
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?(?:\s*(?:off|=|/|in\b))?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|"
    r"\bcoupons?\b|\bpromos?\b|\brebates?\b|\bdiscounts?\b|"
    r"\bsave\b|\bbonus\b|\bfree\b|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b|\bfinancing\b|\bpackage\s+price\b|"
    r"\$\s*\d+\s*=\s*[\d,\s]+\s*(?:fx\s*)?points\b)",
    re.IGNORECASE,
)

# Concrete-offer signal — must appear in the BODY (or OCR text) of every
# kept row. Bare words like "coupon" or "discount" or "deal" don't qualify
# on their own — there must be a numeric value, an explicit free-service
# combo, a buy-N-get-N-free, or a concrete points/bonus value.
_CONCRETE_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|=|/)|"
    r"\$\s*\d+(?:\.\d{1,2})?\b|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|\bup\s+to\s+\$\s*\d|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\bfree\s+(?:oil\s+change|tire|brake|battery|service|wash|inspection)\b|"
    r"\$\s*\d+\s*=\s*[\d,\s]+\s*(?:fx\s*)?points\b|"
    r"\b\d[\d,]*\s+bonus\s+(?:fx\s*)?points\b|"
    r"\bsave\s+\$?\s*\d|\bget\s+\$\s*\d|"
    r"\bmail-?in\s+rebate\b|\brebates?\s+up\s+to\s+\$\s*\d|"
    r"\bno\s+payments?\s+for\s+\d+\s+months\b)",
    re.IGNORECASE,
)

# Coupon-page boilerplate headings that aren't real offers.
_COUPONS_TITLE_REJECT = re.compile(
    r"(?:^why\s+choose|save\s+money\s+on\s+your\s+next\s+service|"
    r"get\s+coupons\s+online|we\s+make\s+car\s+maintenance|"
    r"finance\s+your\s+service|no\s+appointment\s+needed|"
    r"put\s+your\s+savings\s+in\s+overdrive|explore\s+the\s+benefits|"
    r"hours\s+of\s+operation|get\s+in\s+touch|corporate\s+store)",
    re.IGNORECASE,
)

# Sign-up / email-coupon CTAs — useless on their own.
_FORM_CTA_PATTERNS = re.compile(
    r"(?:email\s+coupon|text\s+coupon|sign\s*up\s+now|subscribe\b|"
    r"enter\s+your\s+email|join\s+our\s+(?:club|rewards))",
    re.IGNORECASE,
)

# Vendor/SEO/footer spam to drop wherever it appears.
_SPAM_LINE = re.compile(
    r"kentucky\s+web\s+design|tapmango\.com|my\s+business\s+local|"
    r"optimal\s+health\s+bridge",
    re.IGNORECASE,
)

# A heading-block must mention "LubeFx + Coupons" to be considered a real
# coupon card (the coupons page uses that marker on every offer block).
_LUBEFX_COUPON_MARKER = re.compile(r"lubefx\s*\+\s*coupons?", re.IGNORECASE)

# Concrete-benefit signal on the rewards page.
_REWARDS_CONCRETE = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off\b|=\s*)|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\$\s*\d+\s*=\s*[\d,\s]+\s*(?:fx\s*)?points\b|"
    r"\b\d[\d,]*\s+bonus\s+points\b|"
    r"\brefer\s+\d+\s+new\b.*\$\s*\d|"
    r"\$\s*\d+\s+off\s*\+|\bbirthday\s+(?:reward|gift)\b|"
    r"\bgift\s+card\b)",
    re.IGNORECASE,
)

_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|special|discount|deal|save|lubefx|"
    r"oil[-_ ]?change|tire|banner|\$\s*\d+|\d+\s*%)",
    re.IGNORECASE,
)

_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"google-?play|app-?store|badge|qr|oil[-_]drop)",
    re.IGNORECASE,
)

# OCR text matching this is service-menu pricing, not a coupon card.
_OCR_EXCLUDE_A_C = re.compile(
    r"\b(?:express\s+a/?c|a/?c\s+recharge|refrigerant|r134|1234yf|134a)\b",
    re.IGNORECASE,
)

_EXPIRY_RE = re.compile(
    r"(?:expires?|valid\s+(?:until|through|thru))\s*[:\-]?\s*"
    r"((?:[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})|"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))",
    re.IGNORECASE,
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _fetch_page(url: str) -> Tuple[str, List[str]]:
    res = fetch_with_firecrawl(url, timeout=90)
    if res.get("html") and not res.get("error"):
        return res["html"], res.get("images") or []
    logger.warning(f"[lubefx-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


def _page_kind(url: str, link: Dict) -> str:
    k = (link.get("page_kind") or "").strip().lower()
    if k in ("coupons", "rewards"):
        return k
    u = url.lower()
    if "rewards" in u:
        return "rewards"
    return "coupons"


def _extract_title(text: str, fallback: str = "") -> str:
    line = re.split(r"[\.\n]", (text or "").strip(), 1)[0].strip()
    line = re.sub(r"\s+", " ", line)[:160]
    return line or fallback


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip() if m else None


def _lubefx_ocr_discount(ocr_text: str) -> Optional[str]:
    """Prefer the coupon-card dollar amount; ignore ``Reg. $`` list prices and
    "Additional $X off (does not apply)" disclaimer lines."""
    if not ocr_text:
        return None
    low = ocr_text.lower()
    if "tire rotation" in low:
        m = re.search(
            r"\$\s*(\d+(?:\.\d{2})?)[^\n]{0,55}tire\s+rotation",
            ocr_text, re.IGNORECASE,
        )
        if m:
            return f"${m.group(1)}"
    # Strip noise lines that pollute the discount extractor:
    #   "Reg. $79.99"
    #   "'Additional $5 OFF by closing offer' does not apply to this offer"
    cleaned_lines: List[str] = []
    raw_lines = ocr_text.splitlines()
    for idx, ln in enumerate(raw_lines):
        if re.search(r"\breg\.?\s*\$", ln, re.IGNORECASE):
            continue
        if re.search(r"\badditional\s+\$\s*\d", ln, re.IGNORECASE):
            # Drop if the same or next line carries the "does not apply" caveat.
            tail = " ".join(raw_lines[idx:idx + 2]).lower()
            if "does not apply" in tail or "not apply" in tail:
                continue
        cleaned_lines.append(ln)
    scrubbed = "\n".join(cleaned_lines)
    # LubeFx "BASIC OIL $34.99 SPECIAL" / "OIL CHANGE $X" package pricing.
    m = re.search(
        r"\bbasic\s+oil\b[^\n$]{0,40}\$\s*(\d+(?:\.\d{2})?)",
        scrubbed, re.IGNORECASE,
    )
    if m:
        return f"${m.group(1)}"
    return _v2_extract_discount(scrubbed)


_OCR_TITLE_OFFER_LINE = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|/|=)?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\b|\bsave\s+\$?\d|\bget\s+\$\d|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\brebate|\bfinancing\b|\bfree\s+oil)",
    re.IGNORECASE,
)


def _title_from_ocr(ocr_text: str, fallback: str) -> str:
    """Pick a meaningful coupon line for the title.

    OCR returns line-by-line text where the first line is often a generic
    label ("YOUR", "KEEP YOUR", "LUBE FX+"). Prefer a line that names the
    actual offer (dollar amount, percent off, rebate, buy-N-get-N-free).
    """
    if not ocr_text:
        return fallback
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", ocr_text) if ln.strip()]
    for ln in lines:
        # Skip the regular-price comparison line ("Reg. $79.99") — that's
        # not the offer title.
        if re.match(r"^\s*reg\.?\s*\$", ln, re.IGNORECASE):
            continue
        if _OCR_TITLE_OFFER_LINE.search(ln) and len(ln) >= 8:
            return re.sub(r"\s+", " ", ln).strip(" *.,'\"")[:160]
    return fallback


_SERVICE_NOISE_RE = re.compile(
    r"(?:quick\s+lube\s*\+\s*tires?|keeping\s+you\s+moving\s+forward|"
    r"car-?care\s+pros|enjoy\s+same-?day|same-?day\s+tire\s+service|"
    r"no\s+appointment\s+needed|www\.lubefx\.com|lube\s+f[zx]?\+?\b|"
    r"lube\s+fx\+?\b|do\s+more\s+save\s+more|get\s+rew(?:anded|arded))",
    re.IGNORECASE,
)


def _strip_brand_noise(text: str) -> str:
    """Strip LubeFx brand taglines so they don't pollute service classification."""
    return _SERVICE_NOISE_RE.sub(" ", text or "")


# ---------------------------------------------------------------------------
# Image download + OCR
# ---------------------------------------------------------------------------
def _download_image(url: str, *, referer: str, dest_dir: Path = IMAGES_DIR) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = referer
    p = urlparse(referer)
    if p.scheme and p.netloc:
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fname = f"lubefx_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=20,
                         allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(f"[lubefx-v2] image fetch {r.status_code} for {url}")
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[lubefx-v2] image download error for {url}: {e}")
        return None


def _ocr_url(url: str, *, referer: str, ocr_cache: Dict[str, str]) -> str:
    if url in ocr_cache:
        return ocr_cache[url]
    img_path = _download_image(url, referer=referer)
    text = ""
    if img_path:
        try:
            text = ocr_image(img_path) or ""
        except Exception as e:
            logger.warning(f"[lubefx-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


# ---------------------------------------------------------------------------
# Heading-block extraction
# ---------------------------------------------------------------------------
def _heading_level(tag: Tag) -> int:
    if tag.name and tag.name.startswith("h") and len(tag.name) == 2 \
            and tag.name[1].isdigit():
        return int(tag.name[1])
    return 99


def _collect_until_next_heading(start: Tag) -> str:
    """Gather text from siblings after ``start`` until the next heading of
    same or higher priority."""
    parts: List[str] = []
    lvl = _heading_level(start)
    for sib in start.next_siblings:
        if isinstance(sib, NavigableString):
            continue
        if not isinstance(sib, Tag):
            continue
        if (sib.name and sib.name.startswith("h") and len(sib.name) == 2
                and sib.name[1].isdigit()):
            if _heading_level(sib) <= lvl:
                break
        if sib.name in {"section", "article", "div", "p", "ul", "ol", "span"}:
            t = _clean(sib.get_text(" ", strip=True))
            if t and not _SPAM_LINE.search(t):
                parts.append(t)
        if sum(len(p) for p in parts) > 1200:
            break
    return _clean(" ".join(parts))


def _rewards_block_qualifies(text: str) -> bool:
    if not text or len(text) < 10:
        return False
    if _SPAM_LINE.search(text):
        return False
    if not _REWARDS_CONCRETE.search(text):
        return False
    low = text.lower()
    # Member's Advantage / VIP Access — only keep when an actual benefit
    # amount or multiplier is named.
    if "member's advantage" in low and not re.search(r"\$\s*\d", text):
        return False
    if "vip access" in low and "20,000" not in text and "2x" not in low:
        return False
    return True


def _extract_heading_offer_blocks(html: str, *, page_kind: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for sel in ("header", "footer", "nav"):
        for t in soup.find_all(sel):
            t.decompose()

    candidates: List[Dict] = []
    seen: set = set()

    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        title = _clean(h.get_text(" ", strip=True))
        if len(title) < 10:
            continue
        if _SPAM_LINE.search(title):
            continue
        if page_kind == "rewards" and re.match(
            r"^oil\s+change\s+edmonton\s*$", title, re.I
        ):
            continue
        body = _collect_until_next_heading(h)
        combined = _clean(f"{title} {body}")

        if page_kind == "coupons" and _COUPONS_TITLE_REJECT.search(title):
            continue

        min_combo = 25 if page_kind == "coupons" else 10
        if len(combined) < min_combo:
            continue

        if page_kind == "coupons":
            # On the coupons page real cards are tagged with "LubeFx + Coupons".
            if not _LUBEFX_COUPON_MARKER.search(combined):
                continue
            # CTA-only blocks (no actual offer) are dropped.
            if _FORM_CTA_PATTERNS.search(combined) and len(combined) < 120:
                continue
            if not _OFFER_SIGNAL.search(combined):
                continue
        else:
            if not _rewards_block_qualifies(combined):
                continue

        block_id = hash(combined[:400])
        if block_id in seen:
            continue
        seen.add(block_id)

        candidates.append({
            "title": title[:180],
            "body": combined[:1500],
            "raw_text": combined[:2800],
            "method": "text_heading",
        })

    return candidates


# ---------------------------------------------------------------------------
# Service classification
# ---------------------------------------------------------------------------
def _refine_service(service_hint: str, text: str, *, page_kind: str) -> str:
    cleaned = _strip_brand_noise(text or "")
    classified = classify_service(cleaned) or "Other"
    low = cleaned.lower()
    if page_kind == "coupons" and re.search(r"tire\s+rotation", low):
        if "diesel" not in low and not re.search(r"differential|fuel\s+filter", low):
            return "Tire Rotation"
    if page_kind == "rewards":
        if "tire storage" in low or ("storage" in low and "tire" in low):
            return "Tire Sales"
        if "buy 7" in low and "free" in low:
            return "Oil Change"
        if "birthday" in low and classified == "Other":
            return "Oil Change"
    if service_hint and service_hint in _ALLOWED_SERVICES:
        if classified == "Other":
            return service_hint
        if classified == service_hint:
            return service_hint
        if classified in _ALLOWED_SERVICES:
            if classified == "Oil Change" and "oil" in low:
                return classified
            if classified == "Tire Sales" and "tire" in low:
                return classified
            if classified == "Battery" and "batter" in low:
                return classified
        return service_hint
    return classified


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------
def _collect_page_images(html: str, page_url: str, extra: List[str]) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, Dict] = {}
    for img in soup.find_all("img"):
        src = (
            img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            or img.get("data-original") or ""
        )
        if not src or src.startswith("data:"):
            continue
        url = normalize_url(page_url, src)
        if not url or _UI_IMAGE_SKIP.search(url):
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.IGNORECASE):
            continue
        alt = (img.get("alt") or "").strip()
        cls = " ".join(img.get("class") or [])
        parent_cls = " ".join((img.parent.get("class") or []) if img.parent else [])
        blob = " ".join([url, alt, cls, parent_cls])
        hinted = bool(_PROMO_IMAGE_HINTS.search(blob))
        found.setdefault(url, {"url": url, "alt": alt, "hinted": hinted})

    for url in extra or []:
        if not url or _UI_IMAGE_SKIP.search(url):
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.IGNORECASE):
            continue
        found.setdefault(url, {"url": url, "alt": "", "hinted": False})

    hinted = [v for v in found.values() if v["hinted"]]
    rest = [v for v in found.values() if not v["hinted"]]
    return hinted + rest[: max(0, 18 - len(hinted))]


# ---------------------------------------------------------------------------
# Row builder + signatures
# ---------------------------------------------------------------------------
def _signature_base(
    *, title: str, discount: Optional[str], expiry: Optional[str], service: str,
) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"s={service}|d={d}|e={e}|t={t}"


def _build_row(
    *,
    page_url: str,
    service: str,
    title: str,
    offer_details: str,
    raw_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    extraction_method: str,
    source_image: Optional[str],
    promo_description: str,
    needs_review_reason: Optional[str],
) -> Dict:
    row: Dict = {
        # Sheet-compatible columns first
        "website": _WEBSITE,
        "page_url": page_url,
        "business_name": _BUSINESS_NAME,
        "google_reviews": "",
        "service_name": service,
        "promo_description": promo_description,
        "category": service,
        "contact": "",
        "location": _LOCATION,
        "offer_details": (offer_details or "")[:1000],
        "ad_title": title,
        "ad_text": (raw_text or "")[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA / meta columns
        "city": _CITY,
        "store_name": _STORE,
        "source_scope": _SOURCE_SCOPE,
        "extraction_method": extraction_method,
        "confidence": None,
        "needs_review": bool(needs_review_reason),
        "needs_review_reason": needs_review_reason or "",
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": title,
        "normalized_title": re.sub(r"\s+", " ", (title or "").lower().strip()),
        "applicable_cities": [_CITY],
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
        "source_image": source_image or "",
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


# ---------------------------------------------------------------------------
# Per-page scrape
# ---------------------------------------------------------------------------
def _scrape_one_page(
    *,
    url: str,
    service_hint: str,
    page_kind: str,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
    enable_ocr: bool,
) -> Dict:
    logger.info(f"[lubefx-v2] Fetch {page_kind} | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return {
            "url": url, "status": "fetch_failed", "rows": [],
            "excluded": 0, "cards_on_page": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
            "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
            "page_kind": page_kind, "service_hint": service_hint,
        }

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    text_count = 0
    image_count = 0
    image_failed_nr = 0
    ocr_attempted = ocr_success = ocr_failed = 0

    # ---- Text candidates --------------------------------------------------
    for cand in _extract_heading_offer_blocks(html, page_kind=page_kind):
        raw_text = cand["raw_text"]
        title = cand["title"]
        body = cand["body"]

        # Hard offer-signal gate. Real offer only.
        text_ok = bool(_OFFER_SIGNAL.search(raw_text))
        if page_kind == "rewards":
            text_ok = text_ok and _rewards_block_qualifies(raw_text)
        if not text_ok:
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "no_offer_signal" if page_kind == "coupons"
                          else "rewards_not_concrete_benefit",
                "source_image": "",
                "raw_text": raw_text[:260],
            })
            continue

        # Body-level concrete-offer gate: bare headings like "LUBEFX+ COUPONS
        # TIRE SERVICES DISCOUNT" pass _OFFER_SIGNAL on the word "discount"
        # alone but carry no real promo — kick them out unless the body has
        # a real numeric/free-service/buy-N-get-N-free signal.
        if page_kind == "coupons":
            body_no_marker = _LUBEFX_COUPON_MARKER.sub(" ", raw_text)
            if not _CONCRETE_OFFER_SIGNAL.search(body_no_marker):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "text",
                    "reason": "no_concrete_offer_in_body",
                    "source_image": "",
                    "raw_text": raw_text[:260],
                })
                continue

        # Rewards "$1 = 1000 FX Points" — a points-conversion explainer, not
        # a service offer worth a row.
        if page_kind == "rewards" and re.match(
            r"^\s*\$\s*\d+\s*=\s*\d", title.strip(), re.IGNORECASE
        ):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "rewards_points_conversion_not_service_offer",
                "source_image": "",
                "raw_text": raw_text[:260],
            })
            continue

        service = _refine_service(service_hint, raw_text, page_kind=page_kind)
        # Reject vague "additional service" coupons with no specific service.
        if page_kind == "coupons" and re.search(
            r"\badditional\s+service\b", raw_text, re.I,
        ) and not re.search(
            r"\b(oil|tire|brake|battery|transmission|coolant|radiator|fuel|rotation)\b",
            raw_text, re.I,
        ):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "unspecific_additional_service",
                "source_image": "",
                "raw_text": raw_text[:260],
            })
            continue

        discount = _v2_extract_discount(raw_text)
        code = _v2_extract_coupon_code(raw_text)
        expiry = _extract_expiry(raw_text)

        sig_local = _signature_base(
            title=title, discount=discount, expiry=expiry, service=service,
        ) + f"|u={url}|m=text"
        if sig_local in seen_local:
            continue
        seen_local.add(sig_local)

        summary = _summarize_promo_description(
            promotion_title=title,
            offer_details=body,
            discount=discount,
            code=code,
            std_service=service,
            ad_text=raw_text,
            brand=_BUSINESS_NAME,
        )
        if page_kind == "rewards" and re.search(
            r"buy\s+\d+\s+get\s+\d+\s+free", title, re.IGNORECASE,
        ):
            summary = (
                "Buy 7 qualifying visits, get 1 free oil change via LubeFx Plus rewards."
            )

        cross_sig = _signature_base(
            title=title, discount=discount, expiry=expiry, service=service,
        )
        row = _build_row(
            page_url=url,
            service=service,
            title=title,
            offer_details=body,
            raw_text=raw_text,
            discount=discount,
            code=code,
            expiry=expiry,
            extraction_method="text",
            source_image=None,
            promo_description=summary,
            needs_review_reason=None,
        )
        row["_signature_base"] = cross_sig
        rows.append(row)
        text_count += 1

    # ---- OCR candidates (coupons page only) -------------------------------
    if enable_ocr and page_kind == "coupons":
        for img in _collect_page_images(html, url, fc_images):
            img_url = img["url"]
            ocr_attempted += 1
            ocr_text = _ocr_url(img_url, referer=url, ocr_cache=ocr_cache)
            if not ocr_text or len(ocr_text.strip()) < 8:
                ocr_failed += 1
                if img["hinted"]:
                    # Spec: OCR failure on coupon image -> needs_review row,
                    # include source_image + page_url; never silently drop.
                    image_failed_nr += 1
                    fallback_service = (
                        service_hint if service_hint in _ALLOWED_SERVICES
                        else "Oil Change"
                    )
                    cross_sig = _signature_base(
                        title=img_url, discount=None, expiry=None,
                        service=fallback_service,
                    )
                    row = _build_row(
                        page_url=url,
                        service=fallback_service,
                        title="(coupon image, OCR failed)",
                        offer_details="",
                        raw_text="",
                        discount=None,
                        code=None,
                        expiry=None,
                        extraction_method="image_ocr",
                        source_image=img_url,
                        promo_description="",
                        needs_review_reason="image_ocr_failed",
                    )
                    row["_signature_base"] = (
                        cross_sig + "|img="
                        + hashlib.md5(img_url.encode()).hexdigest()[:12]
                    )
                    rows.append(row)
                else:
                    excluded_here += 1
                    excluded_log.append({
                        "url": url, "scope": _SOURCE_SCOPE,
                        "extraction_method": "image_ocr",
                        "reason": "no_ocr_text",
                        "source_image": img_url,
                        "raw_text": "",
                    })
                continue

            ocr_success += 1

            if _OCR_EXCLUDE_A_C.search(ocr_text):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "a_c_menu_not_coupon",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            # Multi-price grids are service menus, not coupons.
            if ocr_text.count("$") >= 7:
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "ocr_multi_price_grid",
                    "source_image": img_url,
                    "raw_text": ocr_text[:280],
                })
                continue

            if not _OFFER_SIGNAL.search(ocr_text):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "no_offer_signal_in_ocr",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            if not _CONCRETE_OFFER_SIGNAL.search(ocr_text):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "ocr_no_concrete_offer",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            ot = _title_from_ocr(ocr_text, _extract_title(ocr_text))
            service = _refine_service(service_hint, ocr_text, page_kind="coupons")
            ilow = img_url.lower()
            if "tire-oil-deal" in ilow or "tire-oil" in ilow:
                service = "Tire Sales"
            # Diesel oil packages — URL gives stronger signal than OCR body.
            if "diesel-oil" in ilow or "oil-package" in ilow:
                service = "Oil Change"
            # 34.99 basic-oil coupon — its URL doesn't name the service, but
            # the OCR body mentions "BASIC OIL ... $34.99" — keep as Oil Change
            # even when brand tagline "QUICK LUBE + TIRES" tries to drag it
            # towards Tire Sales.
            if re.search(r"\bbasic\s+oil\b", ocr_text, re.IGNORECASE):
                service = "Oil Change"
            discount = _lubefx_ocr_discount(ocr_text)
            code = _v2_extract_coupon_code(ocr_text)
            expiry = _extract_expiry(ocr_text)

            sig_local = _signature_base(
                title=ot, discount=discount, expiry=expiry, service=service,
            ) + f"|u={url}|m=ocr|img={img_url}"
            if sig_local in seen_local:
                continue
            seen_local.add(sig_local)

            summary = _summarize_promo_description(
                promotion_title=ot,
                offer_details=ocr_text[:1000],
                discount=discount,
                code=code,
                std_service=service,
                ad_text=ocr_text,
                brand=_BUSINESS_NAME,
            )
            if "tire-oil-deal" in ilow or "tire-oil" in ilow:
                if discount:
                    summary = f"{discount} off tire + oil package at LubeFx Plus."
                else:
                    summary = "Tire + oil package promotion at LubeFx Plus."
            # "BASIC OIL $34.99 SPECIAL" — package price, not a discount.
            if re.search(r"\bbasic\s+oil\b", ocr_text, re.IGNORECASE) and discount:
                summary = f"Basic oil change special for {discount} at LubeFx Plus."

            cross_sig = _signature_base(
                title=ot, discount=discount, expiry=expiry, service=service,
            )
            row = _build_row(
                page_url=url,
                service=service,
                title=ot,
                offer_details=ocr_text[:1000],
                raw_text=ocr_text,
                discount=discount,
                code=code,
                expiry=expiry,
                extraction_method="image_ocr",
                source_image=img_url,
                promo_description=summary,
                needs_review_reason=None,
            )
            row["_signature_base"] = cross_sig
            rows.append(row)
            image_count += 1

    cards = text_count + image_count + image_failed_nr
    logger.info(
        f"[lubefx-v2] {url}: text={text_count} img={image_count} "
        f"nr_fail={image_failed_nr} excl={excluded_here}"
    )

    return {
        "url": url,
        "status": "ok",
        "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": cards,
        "text_extracted_count": text_count,
        "image_ocr_extracted_count": image_count,
        "image_ocr_failed_needs_review_count": image_failed_nr,
        "ocr_attempted": ocr_attempted,
        "ocr_success": ocr_success,
        "ocr_failed": ocr_failed,
        "page_kind": page_kind,
        "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_lubefx_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = True,
) -> Dict:
    """Scrape LubeFx Plus (Edmonton, city_store).

    Args:
        competitor_v2: Entry from ``app/config/competitors.v2.json``.
        mode: ``"qa_expanded"`` (default) keeps every kept row; rows that share
              a ``duplicate_group_id`` remain visible.
              ``"final_deduped"`` collapses to one row per group.
        enable_ocr: When False, skip coupon-image OCR.
    """
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError(f"mode must be qa_expanded or final_deduped, got {mode!r}")

    competitor_name = competitor_v2.get("competitor", _BUSINESS_NAME)
    all_rows: List[Dict] = []
    url_log: List[Dict] = []
    excluded_log: List[Dict] = []
    expected_urls: List[str] = []
    ocr_cache: Dict[str, str] = {}

    for link in competitor_v2.get("promo_links", []):
        if isinstance(link, dict):
            url = link["url"]
            hint = link.get("service_hint") or "Oil Change"
        else:
            url = link
            hint = "Oil Change"
        pk = _page_kind(url, link if isinstance(link, dict) else {})
        expected_urls.append(url)

        res = _scrape_one_page(
            url=url,
            service_hint=hint,
            page_kind=pk,
            excluded_log=excluded_log,
            ocr_cache=ocr_cache,
            enable_ocr=enable_ocr,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url,
            "scope": _SOURCE_SCOPE,
            "page_kind": pk,
            "service_hint": hint,
            "status": res["status"],
            "cards_on_page": res.get("cards_on_page", 0),
            "added_rows": len(res["rows"]),
            "excluded_count": res.get("excluded", 0),
            "text_extracted_count": res.get("text_extracted_count", 0),
            "image_ocr_extracted_count": res.get("image_ocr_extracted_count", 0),
            "image_ocr_failed_needs_review_count":
                res.get("image_ocr_failed_needs_review_count", 0),
            "ocr_attempted": res.get("ocr_attempted", 0),
            "ocr_success": res.get("ocr_success", 0),
            "ocr_failed": res.get("ocr_failed", 0),
        })

    # Strict service taxonomy — drop anything outside the allowed 9.
    kept: List[Dict] = []
    for r in all_rows:
        svc = r.get("service_name")
        if svc in _ALLOWED_SERVICES:
            kept.append(r)
        else:
            excluded_log.append({
                "url": r.get("page_url", ""),
                "scope": _SOURCE_SCOPE,
                "extraction_method": r.get("extraction_method", ""),
                "reason": "service_outside_taxonomy",
                "source_image": r.get("source_image", ""),
                "raw_text": (r.get("ad_text") or "")[:320],
            })
    all_rows = kept

    # duplicate_group_id / duplicate_group_total
    sig_to_group: Dict[str, str] = {}
    sig_counts: Dict[str, int] = {}
    for r in all_rows:
        sig = r["_signature_base"]
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        if sig not in sig_to_group:
            sig_to_group[sig] = f"lubefx-{len(sig_to_group)+1:03d}"
    for r in all_rows:
        sig = r.pop("_signature_base")
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]

    if mode == "final_deduped":
        kept_dedup: List[Dict] = []
        seen_gid: set = set()
        for r in all_rows:
            gid = r.get("duplicate_group_id")
            if gid in seen_gid:
                continue
            seen_gid.add(gid)
            kept_dedup.append(r)
        all_rows = kept_dedup

    # ---- Validation -------------------------------------------------------
    processed = {e["url"] for e in url_log if e["status"] == "ok"}
    failed = [e["url"] for e in url_log if e["status"] == "fetch_failed"]
    missing = sorted(set(expected_urls) - {e["url"] for e in url_log})

    row_count_by_url: Dict[str, int] = {}
    row_count_by_city: Dict[str, int] = {}
    svc_counts: Dict[str, int] = {}
    method_counts: Dict[str, int] = {}
    for r in all_rows:
        row_count_by_url[r["page_url"]] = row_count_by_url.get(r["page_url"], 0) + 1
        c = r.get("city") or ""
        row_count_by_city[c] = row_count_by_city.get(c, 0) + 1
        s = r.get("service_name") or ""
        svc_counts[s] = svc_counts.get(s, 0) + 1
        m = r.get("extraction_method") or ""
        method_counts[m] = method_counts.get(m, 0) + 1

    excl_reason_counts: Dict[str, int] = {}
    for x in excluded_log:
        excl_reason_counts[x["reason"]] = excl_reason_counts.get(x["reason"], 0) + 1

    unique_descs = sorted({
        (r.get("promo_description") or "").strip()
        for r in all_rows
        if r.get("promo_description")
    })

    ocr_attempted = sum(e.get("ocr_attempted", 0) for e in url_log)
    ocr_success = sum(e.get("ocr_success", 0) for e in url_log)
    ocr_failed = sum(e.get("ocr_failed", 0) for e in url_log)

    result = {
        "competitor": competitor_name,
        "scraped_at": datetime.now().isoformat(),
        "config_version": "v2",
        "mode": mode,
        "promotions": all_rows,
        "count": len(all_rows),
        "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
        "by_city": row_count_by_city,
        "validation": {
            "expected_url_count": len(expected_urls),
            "processed_url_count": len(processed),
            "failed_url_count": len(failed),
            "failed_urls": failed,
            "missing_urls": missing,
            "row_count_by_url": row_count_by_url,
            "row_count_by_city": row_count_by_city,
            "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
            "excluded_row_count": len(excluded_log),
            "excluded_reason_counts": excl_reason_counts,
            "extraction_method_counts": method_counts,
            "service_count_by_category": svc_counts,
            "unique_promo_descriptions": unique_descs,
            "duplicate_group_total": len(sig_to_group),
            "ocr_attempted": ocr_attempted,
            "ocr_success": ocr_success,
            "ocr_failed": ocr_failed,
            "url_log": url_log,
            "excluded_rows": excluded_log,
        },
    }

    output_file = PROMOTIONS_DIR / "lubefx_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[lubefx-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
