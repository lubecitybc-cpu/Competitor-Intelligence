"""Great Canadian Oil Change (GCOC) v2 scraper.

Site:        https://www.gcoc.ca/
Cities:      Calgary, Edmonton, Grande Prairie (fan-out, ``national_service``)
Extraction:  ``text`` + ``image_ocr``
URLs:        7 (1 home + 6 service pages)

Per spec each URL is scraped once, then every valid offer is fanned out to
all three cities. Duplicate offers across URLs are **kept** (no cross-URL
dedup); they are tagged via ``duplicate_group_id`` / ``duplicate_group_total``
so QA can see the repeats without losing the original page coverage.

Public entry point:
    scrape_gcoc_v2(competitor_v2, *, mode="qa_expanded") -> Dict
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

logger = setup_logger(__name__, "gcoc_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

_BUSINESS_NAME = "Great Canadian Oil Change"
_WEBSITE = "gcoc.ca"
_TARGET_CITIES = ["Calgary", "Edmonton", "Grande Prairie"]

# URL slug → standard service taxonomy hint.
_SERVICE_HINT_FROM_URL = {
    "": "Other",
    "oil-change": "Oil Change",
    "battery-service": "Battery",
    "tire-services": "Tire Sales",          # may flip to Tire Rotation by text
    "fuel-system-cleaning": "Fuel System Flush",
    "transmission-fluid-service": "Transmission Fluid",
    "radiator-fluid-service": "Radiator Flush",
}

# Strong "real offer" signal — must hit one of these for the row to count.
# Plain mentions of the word "coupon" or "offer" alone are NOT enough (those
# appear in newsletter-signup CTAs and footers everywhere on the site).
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?|"                      # $X / $X.XX
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|"          # X% off / up to X%
    r"\brebates?\b|\bfinancing\b|\bpackage\s+price\b|"
    r"\bsave\s+(?:up\s+to\s+)?\$?\d|"                  # save $X / save up to $X
    r"\bget\s+\$\d|\bbonus\b|\bfree\s+(?:with|on|when|service|oil)|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b)",
    re.IGNORECASE,
)

# Disclaimer / boilerplate fragments — drop a candidate that looks like one of
# these even if the regex above accidentally matches.
_DISCLAIMER_PATTERNS = re.compile(
    r"(?:not\s+valid\s+with|see\s+store(?:s)?\s+for|cash\s+value|"
    r"registered\s+in\s+various\s+countries|good\s+only\s+at|"
    r"plus\s+tax|terms\s+(?:and|&)\s+conditions|"
    r"price\s+\$0\.00|cash\s+or\s+credit\s+back|"
    r"diesel\s+litres\s+may\s+vary)",
    re.IGNORECASE,
)

# Newsletter / form CTA fragments to drop.
_FORM_CTA_PATTERNS = re.compile(
    r"(?:get\s+coupon\s*(?:email|text)?$|"
    r"sign\s*up|subscribe|enter\s+your\s+email|email\s+address|"
    r"first\s+name|last\s+name)",
    re.IGNORECASE,
)

# Common navigation/footer lines we never want to treat as offers.
_BOILERPLATE = re.compile(
    r"^(?:home|about|services|contact(?:\s+us)?|locations?|"
    r"find\s+a\s+location|book\s+(?:now|appointment)|menu|search|"
    r"sign\s+in|log\s+in|privacy(?:\s+policy)?|"
    r"terms(?:\s+(?:of\s+use|and\s+conditions))?|cookie\s+policy|"
    r"accessibility|©.*|all\s+rights\s+reserved.*)\s*$",
    re.IGNORECASE,
)

# Image URL / filename / alt hints that suggest promo content.
_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|sale|special|discount|deal|save|saving|"
    r"banner|hero|feature|seasonal|winter|summer|spring|fall|"
    r"(?:\d+\s*%|\$\s*\d+))",
    re.IGNORECASE,
)

# Generic UI / branding / social icons to skip.
_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"google-?play|app-?store|badge)",
    re.IGNORECASE,
)

_EXPIRY_RE = re.compile(
    r"(?:expires?|valid\s+(?:until|through|thru))\s*[:\-]?\s*"
    r"((?:[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})|"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# URL / fetch helpers
# ---------------------------------------------------------------------------
def _service_hint_for_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    slug = parts[-1] if parts else ""
    return _SERVICE_HINT_FROM_URL.get(slug, "Other")


def _fetch_page(url: str) -> Tuple[str, List[str]]:
    """Return ``(html, image_urls)`` via Firecrawl."""
    res = fetch_with_firecrawl(url, timeout=90)
    if res.get("html") and not res.get("error"):
        return res["html"], res.get("images", []) or []
    logger.warning(f"[gcoc-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_title(text: str, fallback: str = "") -> str:
    line = re.split(r"[\.\n]", (text or "").strip(), 1)[0].strip()
    line = re.sub(r"\s+", " ", line)[:160]
    return line or fallback


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip() if m else None


_OFFER_TITLE_RE = re.compile(
    r"(\$\s*\d+(?:\.\d{1,2})?\s*Off\*?\s+[A-Z][A-Za-z0-9 &/\-]{2,50}?)"
    r"(?=\s*(?:Expires?\b|\*|\.|$|\n))",
    re.IGNORECASE,
)


def _better_offer_title(raw_text: str, fallback: str) -> str:
    """If the body contains a clean ``$X Off <thing>`` pattern, prefer that
    over a generic ``<h1>`` like "Coupon"."""
    m = _OFFER_TITLE_RE.search(raw_text or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(" *.,")[:160]
    return fallback


_BARCODE_FILENAME_RE = re.compile(r"barcode-([A-Z0-9]{3,16})\b", re.IGNORECASE)


def _extract_barcode_codes(html: str) -> List[str]:
    """Pull coupon-code tokens out of barcode image filenames in the page.

    GCOC encodes the actual coupon/barcode value in the image filename, e.g.
    ``/wp-content/.../barcode-999W01-<hash>.png``.
    """
    return list({m.group(1).upper() for m in _BARCODE_FILENAME_RE.finditer(html or "")})


# ---------------------------------------------------------------------------
# Text-block candidates
# ---------------------------------------------------------------------------
def _extract_text_candidates(html: str) -> List[Dict]:
    """Pull text-based offer candidates from the page.

    Pass 1: elements whose class/id signals a coupon/offer card.
    Pass 2: heading-anchored fallback when no class signals matched.
    Only candidates that hit ``_OFFER_SIGNAL`` are returned.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    candidates: List[Dict] = []
    seen_blocks: set = set()

    class_pat = re.compile(
        r"(?:coupon|offer|promo|rebate|special|deal|saving|sale|banner)",
        re.IGNORECASE,
    )
    for el in soup.find_all(["div", "section", "article", "li"], class_=class_pat):
        # Skip form-shaped blocks (newsletter signup, "Get Coupon" CTA, etc.).
        if el.find(["form", "input", "textarea"]):
            continue
        text = _clean(el.get_text(" ", strip=True))
        if not text or len(text) < 25:
            continue
        if _FORM_CTA_PATTERNS.search(text) and len(text) < 80:
            continue
        # Disclaimer footnotes ("*Includes 5 or 6 litres...") match the offer
        # regex via "$0.001" but are not coupons themselves.
        if text.startswith("*") and _DISCLAIMER_PATTERNS.search(text):
            continue
        if _DISCLAIMER_PATTERNS.search(text) and not re.search(
            r"\$\s*\d+(?:\.\d{1,2})?\s+off|\d+\s*%\s*off|\bsave\s+\$?\d", text, re.IGNORECASE
        ):
            continue
        block_id = hash(text[:300])
        if block_id in seen_blocks:
            continue
        seen_blocks.add(block_id)
        if not _OFFER_SIGNAL.search(text):
            continue

        h = el.find(["h1", "h2", "h3", "h4", "strong"])
        title = _clean(h.get_text(" ", strip=True)) if h else ""
        if not title:
            title = _extract_title(text)
        candidates.append({
            "title": title,
            "body": text[:1500],
            "raw_text": text[:2500],
            "method": "text_card",
        })

    if not candidates:
        for h in soup.find_all(["h1", "h2", "h3"]):
            title = _clean(h.get_text(" ", strip=True))
            if not title or _BOILERPLATE.match(title):
                continue
            body_parts: List[str] = []
            for sib in h.find_all_next(limit=10):
                if sib.name in {"h1", "h2", "h3"}:
                    break
                t = _clean(sib.get_text(" ", strip=True))
                if t and not _BOILERPLATE.match(t):
                    body_parts.append(t)
                if sum(len(p) for p in body_parts) > 600:
                    break
            body = " ".join(body_parts)[:1500]
            combined = (title + " " + body).strip()
            if len(combined) < 25 or not _OFFER_SIGNAL.search(combined):
                continue
            if _FORM_CTA_PATTERNS.search(combined) and len(combined) < 100:
                continue
            if _DISCLAIMER_PATTERNS.search(combined) and not re.search(
                r"\$\s*\d+(?:\.\d{1,2})?\s+off|\d+\s*%\s*off|\bsave\s+\$?\d",
                combined, re.IGNORECASE,
            ):
                continue
            block_id = hash(combined[:300])
            if block_id in seen_blocks:
                continue
            seen_blocks.add(block_id)
            candidates.append({
                "title": title[:160],
                "body": body,
                "raw_text": combined[:2500],
                "method": "text_heading",
            })

    return candidates


# ---------------------------------------------------------------------------
# Image candidates
# ---------------------------------------------------------------------------
def _collect_page_images(html: str, page_url: str, extra: List[str]) -> List[Dict]:
    """Return a filtered list of ``{url, alt, hinted}`` image dicts.

    Hinted images (filename/alt/class match promo cues) come first; up to
    12 non-hinted images are appended as a safety net.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["header", "footer", "nav"]):
        tag.decompose()

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
        hint_blob = " ".join([url, alt, cls, parent_cls])
        hinted = bool(_PROMO_IMAGE_HINTS.search(hint_blob))
        found.setdefault(url, {"url": url, "alt": alt, "hinted": hinted})

    for url in extra or []:
        if not url or _UI_IMAGE_SKIP.search(url):
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.IGNORECASE):
            continue
        found.setdefault(url, {"url": url, "alt": "", "hinted": False})

    hinted = [v for v in found.values() if v["hinted"]]
    rest = [v for v in found.values() if not v["hinted"]]
    return hinted + rest[: max(0, 12 - len(hinted))]


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


def _download_image(url: str, *, referer: str, dest_dir: Path = IMAGES_DIR) -> Optional[Path]:
    """Download an image using browser-like headers + Referer.

    GCOC's WordPress backend returns 403 to the shared downloader's thin
    User-Agent and missing Referer, so we use a dedicated path here.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = referer
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fname = f"gcoc_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(f"[gcoc-v2] image fetch {r.status_code} for {url}")
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[gcoc-v2] image download error for {url}: {e}")
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
            logger.warning(f"[gcoc-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


# ---------------------------------------------------------------------------
# Service hint refinement
# ---------------------------------------------------------------------------
def _refine_service(service_hint: str, text: str) -> str:
    """Trust hint unless text clearly says a different category."""
    if not text:
        return service_hint or "Other"

    text_lc = text.lower()
    if service_hint == "Tire Sales" and re.search(
        r"\btire\s+rotation\b|\brotate\s+tires?\b", text_lc
    ):
        return "Tire Rotation"

    classified = classify_service(text)
    if not service_hint or service_hint == "Other":
        return classified or "Other"
    if classified and classified not in ("Other", service_hint):
        if (
            (classified == "Battery" and "batter" in text_lc)
            or (classified == "Brake" and "brake" in text_lc)
            or (classified == "Oil Change" and "oil change" in text_lc)
            or (classified == "Tire Sales" and "tire" in text_lc)
            or (classified == "Tire Rotation" and "rotation" in text_lc)
            or (classified == "Radiator Flush" and ("radiator" in text_lc or "coolant" in text_lc))
            or (classified == "Fuel System Flush" and "fuel" in text_lc)
            or (classified == "Transmission Fluid" and "transmission" in text_lc)
        ):
            return classified
    return service_hint


# ---------------------------------------------------------------------------
# Row + signature
# ---------------------------------------------------------------------------
def _signature_local(*, title: str, discount: Optional[str], expiry: Optional[str],
                     service: str, page_url: str) -> str:
    """Per-page signature: collapses repeats inside a single page."""
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"u={page_url}|s={service}|d={d}|e={e}|t={t}"


def _signature_cross_url(*, title: str, discount: Optional[str], expiry: Optional[str],
                         service: str) -> str:
    """Cross-page signature: groups the same offer that appears on multiple
    URLs. We DO NOT drop these — they only get a shared ``duplicate_group_id``."""
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"s={service}|d={d}|e={e}|t={t}"


def _build_row(
    *,
    page_url: str,
    city: str,
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
        # Sheet-compatible columns
        "website": _WEBSITE,
        "page_url": page_url,
        "business_name": _BUSINESS_NAME,
        "google_reviews": "",
        "service_name": service,
        "promo_description": promo_description,
        "category": service,
        "contact": "National",
        "location": "National",
        "offer_details": offer_details[:1000],
        "ad_title": title,
        "ad_text": (raw_text or "")[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA / meta
        "city": city,
        "store_name": "National",
        "source_scope": "national_service",
        "extraction_method": extraction_method,
        "confidence": None,
        "needs_review": bool(needs_review_reason),
        "needs_review_reason": needs_review_reason or "",
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": title,
        "normalized_title": re.sub(r"\s+", " ", (title or "").lower().strip()),
        "applicable_cities": list(_TARGET_CITIES),
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
        "source_image": source_image or "",
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


# ---------------------------------------------------------------------------
# Per-page scraper
# ---------------------------------------------------------------------------
def _scrape_one_page(
    *,
    url: str,
    service_hint: str,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
    enable_ocr: bool = True,
) -> Dict:
    logger.info(f"[gcoc-v2] Fetching {service_hint} | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return {
            "url": url, "status": "fetch_failed", "rows": [],
            "excluded": 0, "cards_on_page": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
            "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
            "service_hint": service_hint,
        }

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    text_count = 0
    image_count = 0
    image_failed_count = 0
    ocr_attempted = 0
    ocr_success = 0
    ocr_failed = 0

    # Page-level enrichment: barcode coupon codes pulled from image filenames.
    page_barcode_codes = _extract_barcode_codes(html)

    # ---- Text candidates ----------------------------------------------------
    for cand in _extract_text_candidates(html):
        raw_text = cand["raw_text"]
        generic_title = cand["title"] or _extract_title(raw_text)
        title = _better_offer_title(raw_text, generic_title)
        body = cand["body"]

        if not _OFFER_SIGNAL.search(raw_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": "national_service",
                "extraction_method": "text",
                "reason": "no_offer_signal", "raw_text": raw_text[:240],
            })
            continue

        service = _refine_service(service_hint, raw_text)
        discount = _v2_extract_discount(raw_text)
        code = _v2_extract_coupon_code(raw_text)
        # Fallback: standalone alphanumeric token like "999W01" inside the
        # offer text (no "code:" prefix), or the value baked into the
        # barcode-<CODE>-*.png filename next to this card.
        if not code:
            m_inline = re.search(
                r"\b([A-Z0-9]{4,12})\s+\$\s*\d+\s*Off",
                raw_text, re.IGNORECASE,
            )
            if m_inline:
                code = m_inline.group(1).upper()
            elif len(page_barcode_codes) == 1:
                code = page_barcode_codes[0]
        expiry = _extract_expiry(raw_text)

        sig = _signature_local(
            title=title, discount=discount, expiry=expiry,
            service=service, page_url=url,
        )
        if sig in seen_local:
            continue
        seen_local.add(sig)

        summary = _summarize_promo_description(
            promotion_title=title,
            offer_details=body,
            discount=discount,
            code=code,
            std_service=service,
            ad_text=raw_text,
            brand="Great Canadian Oil Change",
        )

        cross_sig = _signature_cross_url(
            title=title, discount=discount, expiry=expiry, service=service,
        )
        for city in _TARGET_CITIES:
            row = _build_row(
                page_url=url, city=city, service=service,
                title=title, offer_details=body, raw_text=raw_text,
                discount=discount, code=code, expiry=expiry,
                extraction_method="text", source_image=None,
                promo_description=summary, needs_review_reason=None,
            )
            row["_signature_base"] = cross_sig
            rows.append(row)
        text_count += 1

    # ---- Image OCR candidates ----------------------------------------------
    if enable_ocr:
        for img in _collect_page_images(html, url, fc_images):
            img_url = img["url"]
            ocr_attempted += 1
            ocr_text = _ocr_url(img_url, referer=url, ocr_cache=ocr_cache)
            if not ocr_text or len(ocr_text.strip()) < 8:
                ocr_failed += 1
                if img["hinted"]:
                    # Hinted promo image with no OCR → flag for manual review.
                    image_failed_count += 1
                    cross_sig = _signature_cross_url(
                        title=img_url, discount=None, expiry=None,
                        service=service_hint,
                    )
                    for city in _TARGET_CITIES:
                        row = _build_row(
                            page_url=url, city=city, service=service_hint,
                            title="(image-only offer, OCR failed)",
                            offer_details="", raw_text="",
                            discount=None, code=None, expiry=None,
                            extraction_method="image_ocr_failed",
                            source_image=img_url,
                            promo_description="",
                            needs_review_reason="image_ocr_failed",
                        )
                        row["_signature_base"] = cross_sig
                        rows.append(row)
                else:
                    excluded_here += 1
                    excluded_log.append({
                        "url": url, "scope": "national_service",
                        "extraction_method": "image_ocr",
                        "reason": "no_ocr_text",
                        "source_image": img_url, "raw_text": "",
                    })
                continue

            ocr_success += 1
            if not _OFFER_SIGNAL.search(ocr_text):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": "national_service",
                    "extraction_method": "image_ocr",
                    "reason": "no_offer_signal_in_ocr",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            title = _extract_title(ocr_text)
            service = _refine_service(service_hint, ocr_text)
            discount = _v2_extract_discount(ocr_text)
            code = _v2_extract_coupon_code(ocr_text)
            expiry = _extract_expiry(ocr_text)

            sig = _signature_local(
                title=title, discount=discount, expiry=expiry,
                service=service, page_url=url,
            )
            if sig in seen_local:
                continue
            seen_local.add(sig)

            summary = _summarize_promo_description(
                promotion_title=title,
                offer_details=ocr_text[:1000],
                discount=discount,
                code=code,
                std_service=service,
                ad_text=ocr_text,
                brand="Great Canadian Oil Change",
            )

            cross_sig = _signature_cross_url(
                title=title, discount=discount, expiry=expiry, service=service,
            )
            for city in _TARGET_CITIES:
                row = _build_row(
                    page_url=url, city=city, service=service,
                    title=title, offer_details=ocr_text[:1000],
                    raw_text=ocr_text,
                    discount=discount, code=code, expiry=expiry,
                    extraction_method="image_ocr", source_image=img_url,
                    promo_description=summary, needs_review_reason=None,
                )
                row["_signature_base"] = cross_sig
                rows.append(row)
            image_count += 1

    cards = text_count + image_count
    logger.info(
        f"[gcoc-v2] {url}: text={text_count} images={image_count} "
        f"excluded={excluded_here} ocr_attempted={ocr_attempted} "
        f"ocr_success={ocr_success} ocr_failed={ocr_failed}"
    )

    return {
        "url": url, "status": "ok", "rows": rows,
        "excluded": excluded_here, "cards_on_page": cards,
        "text_extracted_count": text_count,
        "image_ocr_extracted_count": image_count,
        "image_ocr_failed_needs_review_count": image_failed_count,
        "ocr_attempted": ocr_attempted,
        "ocr_success": ocr_success,
        "ocr_failed": ocr_failed,
        "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_gcoc_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = True,
) -> Dict:
    """Scrape Great Canadian Oil Change pages and fan offers out to 3 cities.

    Args:
        competitor_v2: Entry from ``app/config/competitors.v2.json`` with
            ``promo_links`` items shaped as ``{"url": ..., "service_hint": ...}``.
        mode: ``"qa_expanded"`` (default — keep every per-URL row) or
            ``"final_deduped"`` (collapse cross-URL duplicates per city).
        enable_ocr: When False, skip image OCR entirely (text-only run).
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
            hint = link.get("service_hint") or _service_hint_for_url(url)
        else:
            url = link
            hint = _service_hint_for_url(url)
        expected_urls.append(url)

        res = _scrape_one_page(
            url=url, service_hint=hint,
            excluded_log=excluded_log, ocr_cache=ocr_cache,
            enable_ocr=enable_ocr,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url,
            "scope": "national_service",
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

    # duplicate_group_id / total via cross-URL signature.
    # Per spec we DO NOT drop these in qa_expanded mode.
    sig_to_group: Dict[str, str] = {}
    sig_counts: Dict[str, int] = {}
    for r in all_rows:
        sig = r["_signature_base"]
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        if sig not in sig_to_group:
            sig_to_group[sig] = f"gcoc-{len(sig_to_group)+1:03d}"
    for r in all_rows:
        sig = r.pop("_signature_base")
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]

    if mode == "final_deduped":
        kept: List[Dict] = []
        seen: set = set()
        for r in all_rows:
            key = (r["duplicate_group_id"], r["city"])
            if key in seen:
                continue
            seen.add(key)
            kept.append(r)
        all_rows = kept

    # ---- Validation --------------------------------------------------------
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

    output_file = PROMOTIONS_DIR / "gcoc_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[gcoc-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
