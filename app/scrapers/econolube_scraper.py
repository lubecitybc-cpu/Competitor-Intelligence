"""Econo Lube v2 scraper (Edmonton, city_store).

Sources: 7 econolube.ca pages (homepage + services + 5 individual service
pages). All carry the same sitewide announcement-bar offer; the duplicate-
group machinery surfaces all per-URL occurrences in qa_expanded mode and
collapses them to one row in final_deduped mode.

Every kept row uses:
    city               = "Edmonton"
    store_name         = "Econo Lube"
    location           = "Edmonton"
    applicable_cities  = ["Edmonton"]
    source_scope       = "city_store"

Extraction is text-only by default. OCR is supported as a fallback but is
skipped unless ``enable_ocr=True``.

Public entry point:
    scrape_econolube_v2(competitor_v2, *,
                        mode="qa_expanded",
                        enable_ocr=False) -> Dict
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

logger = setup_logger(__name__, "econolube_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BUSINESS_NAME = "Econo Lube"
_WEBSITE = "econolube.ca"
_CITY = "Edmonton"
_STORE = "Econo Lube"
_LOCATION = "Edmonton"
_SOURCE_SCOPE = "city_store"

_ALLOWED_SERVICES = frozenset({
    "Battery", "Oil Change", "Brake", "Tire Sales", "Tire Rotation",
    "Transmission Fluid", "Radiator Flush", "Fuel System Flush", "Other",
})

# First-pass offer signal — any real promo keyword or numeric cue.
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?(?:\s*(?:off|=|/|in\b))?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|"
    r"\bcoupons?\b|\bpromos?\b|\brebates?\b|\bdiscounts?\b|"
    r"\bsave\s+\$?\d|\bbonus\b|\bfree\b|\breward\b|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b|\boffer\s+ends?\b|\bends?\s+[A-Z][a-z]+\s+\d+\b|"
    r"\bfinancing\b|\bpackage\s+price\b)",
    re.IGNORECASE,
)

# Concrete-offer signal — must be present on every kept row. Bare "free" or
# "save" is insufficient; we need a free-service combo, a numeric value, a
# buy-N-get-N-free, or an explicit rebate/financing/package.
_CONCRETE_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|=|/)|"
    r"\$\s*\d+(?:\.\d{1,2})?\b|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|\bup\s+to\s+\$\s*\d|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\bfree\s+(?:tire(?:s|\s+changeover|\s+rotation|\s+mount)?|"
    r"oil\s+change|brake\s+(?:inspection|check)|battery\s+(?:check|test)|"
    r"alignment|wheel\s+alignment|wiper)\b"
    r"(?:\s+with\s+(?:your\s+|any\s+)?[A-Za-z][A-Za-z ]+)?|"
    r"\bsave\s+\$?\s*\d|\bget\s+\$\s*\d|"
    r"\bmail-?in\s+rebate\b|\brebates?\s+up\s+to\s+\$\s*\d|"
    r"\bno\s+payments?\s+for\s+\d+\s+months\b)",
    re.IGNORECASE,
)

# Lines that pretend to be offers but aren't: "free quote", "free coffee",
# "free wifi", "save time", phone-number CTAs, etc.
_NOISE_OFFER_PHRASES = re.compile(
    r"^\s*(?:get\s+directions?|give\s+us\s+a\s+call|book\s+a\s+repair|"
    r"skip\s+to\s+content|menu|scroll\s+to\s+top|book\s+now)",
    re.IGNORECASE,
)

_OFFER_NOISE_SUBSTRINGS = re.compile(
    r"(?:"
    # "free (X) quote/estimate/consultation/etc" — sales-tool fluff, even
    # when an intermediate word is present (e.g. "Free Tire Quote").
    r"free\s+(?:\w+\s+){0,2}(?:quote|estimate|consultation|coffee|water|wifi|"
    r"bottled\s+water|tea|snack|tool|wi-?fi)|"
    r"save\s+(?:time|money\b(?!\s+on\s+(?:any\s+)?[A-Za-z]+\s+(?:oil|tire|brake|battery|service)))|"
    r"stress[- ]free|free\s+online\s+(?:tool|quoting)|"
    r"free\s+(?:to\s+|of\s+charge)|feel\s+free|free\s+process"
    r")",
    re.IGNORECASE,
)

# A coupon image hint (image_ocr fallback if/when the homepage adds image
# coupons in the future).
_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|special|discount|deal|save|"
    r"banner|\$\s*\d+|\d+\s*%)",
    re.IGNORECASE,
)

_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"badge|qr|emblem|review-star|wp-includes|themes/[^/]+/(?:img|images)/)",
    re.IGNORECASE,
)

_EXPIRY_RE = re.compile(
    r"(?:offer\s+ends?|expires?|valid\s+(?:until|through|thru)|ends?)\s*[:\-]?\s*"
    r"((?:[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)|"
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
# Helpers
# ---------------------------------------------------------------------------
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _fetch_page(url: str) -> Tuple[str, List[str]]:
    res = fetch_with_firecrawl(url, timeout=90)
    if res.get("html") and not res.get("error"):
        return res["html"], res.get("images") or []
    logger.warning(f"[econolube-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip(" .,") if m else None


def _split_sentences(body_text: str) -> List[str]:
    """Split body text into sentence-like chunks.

    The site separates the announcement-bar offer from following copy with
    a "?" (rendered emoji), so we split on . ! ? as well as those bar-style
    separators.
    """
    parts = re.split(r"[.!?]\s+|\s+\?\s+|[\n\r]+", body_text or "")
    return [_clean(p) for p in parts if _clean(p)]


def _refine_service(text: str) -> str:
    """Map free text onto the 9-item taxonomy."""
    low = (text or "").lower()
    # The Econo Lube banner is "Free Tire Changeover With Your Oil Change" —
    # the *required* purchase is the oil change, the freebie is the tire
    # changeover. Classify by the qualifying purchase.
    if re.search(r"\bfree\b.*\bwith\s+(?:your\s+)?(.+)", low):
        m = re.search(r"\bwith\s+(?:your\s+)?([a-z][a-z &/\-]{2,40})", low)
        if m:
            after_with = m.group(1)
            classified = classify_service(after_with)
            if classified in _ALLOWED_SERVICES and classified != "Other":
                return classified
    # Fallback — classify on the whole text.
    classified = classify_service(text) or "Other"
    return classified if classified in _ALLOWED_SERVICES else "Other"


# ---------------------------------------------------------------------------
# Text-based offer extraction
# ---------------------------------------------------------------------------
def _extract_text_offers(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    body_text = _clean(soup.get_text(" ", strip=True))

    # Page-wide expiry — the Econo Lube banner splits the offer line and the
    # "Offer Ends May 30" tail into separate chunks, so search the whole body.
    page_expiry = _extract_expiry(body_text)

    found: List[Dict] = []
    seen: set = set()

    for sentence in _split_sentences(body_text):
        # First: strip leading boilerplate that masks the offer
        # (e.g. "Skip to content (780)... info@econolube.ca Free Tire ...").
        sentence_clean = re.sub(
            r"^(?:.*?)(?=(?:\$\s*\d|\bfree\b|\bsave\b|"
            r"\d+\s*%|\bbuy\s+\d|\boffer\b))",
            "",
            sentence,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if not sentence_clean:
            sentence_clean = sentence

        if not _OFFER_SIGNAL.search(sentence_clean):
            continue
        if _NOISE_OFFER_PHRASES.match(sentence_clean):
            continue
        if _OFFER_NOISE_SUBSTRINGS.search(sentence_clean):
            continue
        if not _CONCRETE_OFFER_SIGNAL.search(sentence_clean):
            continue
        if len(sentence_clean) > 220:
            continue

        title = sentence_clean[:160]
        block_id = hash(_signature_meaningful_tokens(title.lower())[:240])
        if block_id in seen:
            continue
        seen.add(block_id)
        found.append({
            "title": title,
            "body": sentence_clean,
            "raw_text": sentence_clean,
            "page_expiry": page_expiry,
        })
    return found


# ---------------------------------------------------------------------------
# Optional image OCR fallback (off by default per spec)
# ---------------------------------------------------------------------------
def _download_image(url: str, *, referer: str, dest_dir: Path = IMAGES_DIR) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = referer
    p = urlparse(referer)
    if p.scheme and p.netloc:
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fname = f"econolube_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=20,
                         allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(f"[econolube-v2] image fetch {r.status_code} for {url}")
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[econolube-v2] image download error for {url}: {e}")
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
            logger.warning(f"[econolube-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


def _collect_hinted_images(html: str, page_url: str, extra: List[str]) -> List[Dict]:
    """Return only IMAGES tagged as likely coupons. We deliberately do NOT
    fall back to "any image" because the spec says OCR is for image-only
    coupons on the homepage, not decorative photos.
    """
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
        if _PROMO_IMAGE_HINTS.search(blob):
            found.setdefault(url, {"url": url, "alt": alt, "hinted": True})
    return list(found.values())


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
# Per-URL scrape
# ---------------------------------------------------------------------------
def _summarize_econolube(
    *, title: str, discount: Optional[str], service: str, expiry: Optional[str],
) -> str:
    """Produce a short factual summary for an Econo Lube offer.

    The shared helper `_summarize_promo_description` doesn't know about
    "free X with Y oil change" phrasing, so we special-case the common
    Econo Lube banner.
    """
    low = title.lower()
    m = re.match(
        r"^\s*free\s+([a-z][a-z\s/\-]+?)\s+with\s+(?:your\s+)?([a-z][a-z\s/\-]+?)"
        r"(?:\s*(?:[-–—]|offer\s+ends?|expires?|valid\b).*)?$",
        low, re.IGNORECASE,
    )
    if m:
        freebie = _clean(m.group(1))
        purchase = _clean(m.group(2))
        # Title-case the nouns; "with your" is implicit.
        s = f"Free {freebie} with any {purchase} at Econo Lube"
        if expiry:
            s += f" (expires {expiry})"
        return s + "."
    # Generic fallback.
    parts: List[str] = []
    if discount:
        if discount.lower() == "free":
            parts.append("Free")
        else:
            parts.append(f"{discount} off")
    if service and service != "Other":
        parts.append(service.lower())
    parts.append("at Econo Lube")
    s = " ".join(parts)
    if expiry:
        s += f" (expires {expiry})"
    return s + "."


def _scrape_one_url(
    *,
    url: str,
    service_hint: str,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
    enable_ocr: bool,
    is_homepage: bool,
) -> Dict:
    logger.info(f"[econolube-v2] Fetch | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return {
            "url": url, "status": "fetch_failed", "rows": [],
            "excluded": 0, "cards_on_page": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
            "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
            "is_homepage": is_homepage, "service_hint": service_hint,
        }

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    text_count = 0
    image_count = 0
    image_failed_nr = 0
    ocr_attempted = ocr_success = ocr_failed = 0

    # ---- Text candidates --------------------------------------------------
    for cand in _extract_text_offers(html):
        raw_text = cand["raw_text"]
        title = cand["title"]
        body = cand["body"]

        # Already gated on offer-signal + concrete-signal inside extractor.
        service = _refine_service(raw_text)
        if service not in _ALLOWED_SERVICES:
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "service_outside_taxonomy",
                "source_image": "",
                "raw_text": raw_text[:240],
            })
            continue

        discount = _v2_extract_discount(raw_text)
        if not discount and re.search(r"\bfree\b", raw_text, re.IGNORECASE):
            discount = "free"
        code = _v2_extract_coupon_code(raw_text)
        # Prefer the per-page expiry (the "Offer Ends May 30" tail may live
        # in a separate sentence from the offer headline).
        expiry = cand.get("page_expiry") or _extract_expiry(raw_text)

        sig_local = _signature_base(
            title=title, discount=discount, expiry=expiry, service=service,
        ) + f"|u={url}|m=text"
        if sig_local in seen_local:
            continue
        seen_local.add(sig_local)

        summary = _summarize_econolube(
            title=title, discount=discount, service=service, expiry=expiry,
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

    # ---- Optional OCR fallback (homepage only, when enable_ocr=True) ----
    if enable_ocr and is_homepage:
        for img in _collect_hinted_images(html, url, fc_images):
            img_url = img["url"]
            ocr_attempted += 1
            ocr_text = _ocr_url(img_url, referer=url, ocr_cache=ocr_cache)
            if not ocr_text or len(ocr_text.strip()) < 8:
                ocr_failed += 1
                image_failed_nr += 1
                cross_sig = _signature_base(
                    title=img_url, discount=None, expiry=None,
                    service=service_hint if service_hint in _ALLOWED_SERVICES
                    else "Other",
                )
                row = _build_row(
                    page_url=url,
                    service=service_hint if service_hint in _ALLOWED_SERVICES
                    else "Other",
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
                continue

            ocr_success += 1
            if not (_OFFER_SIGNAL.search(ocr_text)
                    and _CONCRETE_OFFER_SIGNAL.search(ocr_text)):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "ocr_no_concrete_offer",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            ot = re.split(r"[\n\r]+", ocr_text.strip(), 1)[0][:160]
            service = _refine_service(ocr_text)
            if service not in _ALLOWED_SERVICES:
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "service_outside_taxonomy",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue
            discount = _v2_extract_discount(ocr_text) or (
                "free" if re.search(r"\bfree\b", ocr_text, re.IGNORECASE) else None
            )
            code = _v2_extract_coupon_code(ocr_text)
            expiry = _extract_expiry(ocr_text)
            summary = _summarize_econolube(
                title=ot, discount=discount, service=service, expiry=expiry,
            )
            cross_sig = _signature_base(
                title=ot, discount=discount, expiry=expiry, service=service,
            )
            sig_local = cross_sig + f"|u={url}|m=ocr|img={img_url}"
            if sig_local in seen_local:
                continue
            seen_local.add(sig_local)
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
        f"[econolube-v2] {url}: text={text_count} img={image_count} "
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
        "is_homepage": is_homepage,
        "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_econolube_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = False,
) -> Dict:
    """Scrape Econo Lube (Edmonton, city_store)."""
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
            hint = link.get("service_hint") or "Other"
            is_homepage = bool(link.get("is_homepage")) or url.rstrip("/") == "https://econolube.ca"
        else:
            url = link
            hint = "Other"
            is_homepage = url.rstrip("/") == "https://econolube.ca"
        expected_urls.append(url)

        res = _scrape_one_url(
            url=url,
            service_hint=hint,
            excluded_log=excluded_log,
            ocr_cache=ocr_cache,
            enable_ocr=enable_ocr,
            is_homepage=is_homepage,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url,
            "scope": _SOURCE_SCOPE,
            "service_hint": hint,
            "is_homepage": is_homepage,
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

    # Strict service taxonomy.
    kept: List[Dict] = []
    for r in all_rows:
        if r.get("service_name") in _ALLOWED_SERVICES:
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
            sig_to_group[sig] = f"econolube-{len(sig_to_group)+1:03d}"
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

    output_file = PROMOTIONS_DIR / "econolube_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[econolube-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
