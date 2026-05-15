"""Mobil 1 Lube Express v2 scraper.

Sources:
  1. https://www.mobil1calgary.com/mobileoffer/   (text, Calgary only)
  2. https://mobil1express.ca/coupons/            (image_ocr, Calgary + Edmonton)

Every kept row uses ``source_scope="city_store"`` and
``store_name="Mobil 1 Lube Express"``. Rows are never labeled "regional" /
"nationwide" / "fallback" in any output column.

Public entry point:
    scrape_mobil1_lube_express_v2(competitor_v2, *,
                                  mode="qa_expanded",
                                  enable_ocr=True) -> Dict
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

logger = setup_logger(__name__, "mobil1_lube_express_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BUSINESS_NAME = "Mobil 1 Lube Express"
_WEBSITE = "mobil1express.ca"
_STORE = "Mobil 1 Lube Express"
_SOURCE_SCOPE = "city_store"
_TARGET_CITIES_SHARED: Tuple[str, ...] = ("Calgary", "Edmonton")

_ALLOWED_SERVICES = frozenset({
    "Battery", "Oil Change", "Brake", "Tire Sales", "Tire Rotation",
    "Transmission Fluid", "Radiator Flush", "Fuel System Flush", "Other",
})

# First-pass offer signal — any real promo keyword or numeric cue.
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?(?:\s*(?:off|=|/|in\b))?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|"
    r"\bcoupons?\b|\bpromos?\b|\brebates?\b|\bdiscounts?\b|"
    r"\bsave\b|\bbonus\b|\bfree\b|\breward\b|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b|\bfinancing\b|\bpackage\s+price\b)",
    re.IGNORECASE,
)

# Concrete-offer signal — must appear in the body / OCR text of any row we
# keep. Bare words like "coupon" or "promo" don't qualify on their own.
_CONCRETE_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|=|/)|"
    r"\$\s*\d+(?:\.\d{1,2})?\b|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|\bup\s+to\s+\$\s*\d|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\bfree\s+(?:oil\s+change|tire|brake|battery|service|wash|inspection)\b|"
    r"\bsave\s+\$?\s*\d|\bget\s+\$\s*\d|"
    r"\bmail-?in\s+rebate\b|\brebates?\s+up\s+to\s+\$\s*\d)",
    re.IGNORECASE,
)

# Out-of-taxonomy keyword set for coupons that are real but not relevant
# (fog lights, headlights, accessories, car wash etc.).
_OUT_OF_TAXONOMY_PATTERNS = re.compile(
    r"(?:fog\s+lights?|head[-\s]?lights?|wiper\s+blade|window\s+tint|"
    r"car\s+wash|detailing|accessor(?:y|ies)|gift\s+card|fleet\s+(?:info|service)|"
    r"warranty|employment|career)",
    re.IGNORECASE,
)

_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|special|discount|deal|save|mobil|"
    r"oil[-_ ]?change|tire|banner|\$\s*\d+|\d+\s*%)",
    re.IGNORECASE,
)

_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"call\.png|mail\.png|location-?left|breadcrumb|arrow|join|"
    r"aweber|forms?\.aweber|/flag\.png|/themes/.*?/img/)",
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
    logger.warning(f"[mobil1-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


def _extract_title(text: str, fallback: str = "") -> str:
    line = re.split(r"[\.\n]", (text or "").strip(), 1)[0].strip()
    line = re.sub(r"\s+", " ", line)[:160]
    return line or fallback


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip() if m else None


_OCR_TITLE_OFFER_LINE = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|/|=)?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\b|\bsave\s+\$?\d|\bget\s+\$\d|"
    r"\bbuy\s+\d+\s+get\s+\d+\s+free\b|"
    r"\brebate|\bfinancing\b|\bfree\s+oil)",
    re.IGNORECASE,
)


def _title_from_ocr(ocr_text: str, fallback: str) -> str:
    """Prefer a line that names the offer (price/percent/buy-N-get-N-free).

    Priority:
      1. Compose "$N OFF <next line>" when an OCR line is literally "$N OFF"
         (multi-line coupons render that way — line 1 is "$5 OFF",
          line 2 is the product/service name).
      2. A line matching the offer-cue regex (dollar amount, %, buy-N-get,
         rebate, etc.), skipping "Reg. $" and "Includes up to ..." lines.
      3. Fallback.
    """
    if not ocr_text:
        return fallback
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", ocr_text) if ln.strip()]
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(\$\s*\d+(?:\.\d{1,2})?\s*(?:OFF|off)|\d+\s*%\s*off)\s*$",
                     ln, re.IGNORECASE)
        if not m:
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            nxt = lines[j]
            if re.match(r"^(?:mobil|lube\s+express)\b", nxt, re.IGNORECASE):
                continue
            if _OCR_TITLE_OFFER_LINE.search(nxt):
                continue
            if len(nxt) >= 4:
                amount = re.sub(r"\s+", " ", m.group(1)).strip().upper()
                return re.sub(r"\s+", " ", f"{amount} {nxt}").strip(" *.,'\"")[:160]
    for ln in lines:
        if re.match(r"^\s*reg\.?\s*\$", ln, re.IGNORECASE):
            continue
        if re.match(r"^includes?\s+up\s+to\b", ln, re.IGNORECASE):
            continue
        if _OCR_TITLE_OFFER_LINE.search(ln) and len(ln) >= 8:
            return re.sub(r"\s+", " ", ln).strip(" *.,'\"")[:160]
    return fallback


def _segment_ocr_coupons(ocr_text: str) -> List[str]:
    """Split OCR text into one segment per stacked coupon.

    Mobil 1 Lube Express bundles two coupons per image, each starting on a
    line that is just ``$N OFF`` (or ``N% OFF``). Splitting on those
    boundaries gives one self-contained body per coupon so service
    classification and discount extraction don't bleed across them.
    """
    if not ocr_text:
        return []
    lines = ocr_text.splitlines()
    start_re = re.compile(
        r"^\s*(?:\$\s*\d+(?:\.\d{1,2})?\s*OFF|\d+\s*%\s*OFF)\s*$",
        re.IGNORECASE,
    )
    starts = [i for i, ln in enumerate(lines) if start_re.match(ln.strip())]
    if not starts:
        return [ocr_text]
    starts.append(len(lines))
    segments: List[str] = []
    for i in range(len(starts) - 1):
        seg = "\n".join(lines[starts[i]:starts[i + 1]]).strip()
        if seg:
            segments.append(seg)
    return segments


def _refine_service(service_hint: str, text: str) -> str:
    """Map free text + URL hint onto the 9-item taxonomy."""
    low = (text or "").lower()

    # Multi-system "Fluid Maintenance" coupons (covers transmission +
    # radiator + differential + transfer case) don't fit any single
    # taxonomy bucket — keep them as "Other".
    if re.search(r"\bfluid\s+maintenance\b", low):
        return "Other"

    if re.search(r"\btire\s+rotation\b|\brotate\s+tires?\b", low):
        return "Tire Rotation"
    if re.search(
        r"\bfuel\s+(?:system|injector|injection|cleaning|flush)\b|"
        r"\bdiesel\s+fuel\s+filter\b|\bfuel\s+filter\b",
        low,
    ):
        return "Fuel System Flush"
    if re.search(r"\b(radiator|coolant|antifreeze|cooling\s+system)\b", low):
        return "Radiator Flush"
    if re.search(r"\btransmission\b", low):
        return "Transmission Fluid"
    if re.search(r"\bbatter(?:y|ies)\b", low):
        return "Battery"
    if re.search(r"\b(brake|brakes|brake\s+pad|rotor|caliper)\b", low):
        return "Brake"
    if re.search(r"\boil\s+change\b|\bfull\s+synthetic\b|\bsynthetic\s+oil\b|\bdiesel\s+oil\b", low):
        return "Oil Change"
    if re.search(r"\b(tire\s+sale|set\s+of\s+(?:four\s+)?tires?|new\s+tires?|tire\s+rebate)\b", low):
        return "Tire Sales"

    classified = classify_service(text) or "Other"
    if classified in _ALLOWED_SERVICES:
        return classified
    if service_hint and service_hint in _ALLOWED_SERVICES:
        return service_hint
    return "Other"


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
    fname = f"mobil1_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=20,
                         allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(f"[mobil1-v2] image fetch {r.status_code} for {url}")
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[mobil1-v2] image download error for {url}: {e}")
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
            logger.warning(f"[mobil1-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


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
    return hinted + rest[: max(0, 12 - len(hinted))]


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
    city: str,
    applicable_cities: List[str],
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
        "location": city,
        "offer_details": (offer_details or "")[:1000],
        "ad_title": title,
        "ad_text": (raw_text or "")[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA / meta columns
        "city": city,
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
        "applicable_cities": list(applicable_cities),
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
        "source_image": source_image or "",
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


# ---------------------------------------------------------------------------
# 1) Calgary-only text page
# ---------------------------------------------------------------------------
# Match one "$X off <what>" or "N% off <what>" component anywhere in the body.
# `what` extends until the next coupon boundary (&, "and", paren, comma, etc.)
# OR until the next offer signal ("$N" or "N%"), so multi-component lines like
# "$10 off Any Oil Change & 50% off Car Wash" split cleanly.
_CALGARY_COMPONENT_RE = re.compile(
    r"(?P<disc>\$\s*\d+(?:\.\d{2})?|\d+\s*%)\s*off\s+"
    r"(?P<what>[A-Za-z][A-Za-z0-9 &/\-']*?)"
    r"(?=\s*(?:&|\band\b|\(|\)|,|;|:|\.\s|"
    r"\$\s*\d|\d+\s*%|\bname\b|\bemail\b|\bregister\b|\bprint\b|"
    r"\bexpires?\b|\bvalid\b|\bsign\s+up\b|$))",
    re.IGNORECASE,
)


def _scrape_calgary_text(
    *,
    url: str,
    service_hint: str,
    excluded_log: List[Dict],
) -> Dict:
    logger.info(f"[mobil1-v2] Fetch calgary_text | {url}")
    html, _ = _fetch_page(url)
    if not html:
        return _empty_page_result(url, "calgary_text", service_hint, "fetch_failed")

    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        s.decompose()
    body_text = _clean(soup.get_text(" ", strip=True))

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()

    if not _CONCRETE_OFFER_SIGNAL.search(body_text):
        return _empty_page_result(url, "calgary_text", service_hint, "ok")

    for cm in _CALGARY_COMPONENT_RE.finditer(body_text):
        disc_raw = cm.group("disc")
        what = _clean(cm.group("what")).strip(" .,()")
        if not what:
            continue
        piece_text = f"{disc_raw} off {what}"

        if _OUT_OF_TAXONOMY_PATTERNS.search(piece_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "service_outside_taxonomy",
                "source_image": "",
                "raw_text": piece_text[:240],
            })
            continue

        service = _refine_service(service_hint, piece_text)
        if service not in _ALLOWED_SERVICES:
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "service_outside_taxonomy",
                "source_image": "",
                "raw_text": piece_text[:240],
            })
            continue

        if "%" in disc_raw:
            discount = re.sub(r"\s+", "", disc_raw)
        else:
            m = re.search(r"\$(\d+(?:\.\d{2})?)", disc_raw)
            if not m:
                continue
            amt = re.sub(r"\.00$", "", m.group(1))
            discount = f"${amt}"

        title = piece_text.title()
        expiry = _extract_expiry(body_text)
        code = _v2_extract_coupon_code(body_text)

        sig_local = _signature_base(
            title=title, discount=discount, expiry=expiry, service=service,
        ) + f"|u={url}|m=text"
        if sig_local in seen_local:
            continue
        seen_local.add(sig_local)

        summary = _summarize_promo_description(
            promotion_title=title,
            offer_details=piece_text,
            discount=discount,
            code=code,
            std_service=service,
            ad_text=piece_text,
            brand=_BUSINESS_NAME,
        )

        cross_sig = _signature_base(
            title=title, discount=discount, expiry=expiry, service=service,
        )
        row = _build_row(
            page_url=url,
            city="Calgary",
            applicable_cities=["Calgary"],
            service=service,
            title=title,
            offer_details=piece_text,
            raw_text=piece_text,
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

    return {
        "url": url, "status": "ok", "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": len(rows),
        "text_extracted_count": len(rows),
        "image_ocr_extracted_count": 0,
        "image_ocr_failed_needs_review_count": 0,
        "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
        "page_kind": "calgary_text", "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# 2) Shared image-OCR coupons page (Calgary + Edmonton)
# ---------------------------------------------------------------------------
def _scrape_shared_coupons(
    *,
    url: str,
    service_hint: str,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
    enable_ocr: bool,
) -> Dict:
    logger.info(f"[mobil1-v2] Fetch shared_coupons | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return _empty_page_result(url, "shared_coupons", service_hint, "fetch_failed")

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    image_count = 0
    image_failed_nr = 0
    ocr_attempted = ocr_success = ocr_failed = 0

    if not enable_ocr:
        return _empty_page_result(url, "shared_coupons", service_hint, "ok")

    for img in _collect_page_images(html, url, fc_images):
        img_url = img["url"]
        ocr_attempted += 1
        ocr_text = _ocr_url(img_url, referer=url, ocr_cache=ocr_cache)
        if not ocr_text or len(ocr_text.strip()) < 8:
            ocr_failed += 1
            if img["hinted"]:
                image_failed_nr += 1
                fallback_service = (
                    service_hint if service_hint in _ALLOWED_SERVICES else "Oil Change"
                )
                cross_sig = _signature_base(
                    title=img_url, discount=None, expiry=None,
                    service=fallback_service,
                )
                # Per spec: never silently drop image-based offers — emit a
                # needs_review row for each target city.
                for city in _TARGET_CITIES_SHARED:
                    row = _build_row(
                        page_url=url,
                        city=city,
                        applicable_cities=list(_TARGET_CITIES_SHARED),
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

        segments = _segment_ocr_coupons(ocr_text)
        kept_for_image = 0
        for seg in segments:
            if not _CONCRETE_OFFER_SIGNAL.search(seg):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "ocr_no_concrete_offer",
                    "source_image": img_url,
                    "raw_text": seg[:300],
                })
                continue
            if _OUT_OF_TAXONOMY_PATTERNS.search(seg):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "service_outside_taxonomy",
                    "source_image": img_url,
                    "raw_text": seg[:300],
                })
                continue

            ot = _title_from_ocr(seg, _extract_title(seg))
            # Classify on the title first (most specific to the offer),
            # fall back to the segment body if the title is too generic.
            service = _refine_service(service_hint, ot)
            if service == "Other":
                service = _refine_service(service_hint, seg)
            if service not in _ALLOWED_SERVICES:
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "service_outside_taxonomy",
                    "source_image": img_url,
                    "raw_text": seg[:300],
                })
                continue
            discount = _v2_extract_discount(seg)
            code = _v2_extract_coupon_code(seg)
            expiry = _extract_expiry(seg)

            sig_local = _signature_base(
                title=ot, discount=discount, expiry=expiry, service=service,
            ) + f"|u={url}|m=ocr|img={img_url}"
            if sig_local in seen_local:
                continue
            seen_local.add(sig_local)

            summary = _summarize_promo_description(
                promotion_title=ot,
                offer_details=seg[:1000],
                discount=discount,
                code=code,
                std_service=service,
                ad_text=seg,
                brand=_BUSINESS_NAME,
            )

            cross_sig = _signature_base(
                title=ot, discount=discount, expiry=expiry, service=service,
            )
            for city in _TARGET_CITIES_SHARED:
                row = _build_row(
                    page_url=url,
                    city=city,
                    applicable_cities=list(_TARGET_CITIES_SHARED),
                    service=service,
                    title=ot,
                    offer_details=seg[:1000],
                    raw_text=seg,
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
            kept_for_image += 1
        if kept_for_image:
            image_count += 1

    cards = image_count + image_failed_nr
    logger.info(
        f"[mobil1-v2] {url}: img={image_count} nr_fail={image_failed_nr} "
        f"excluded={excluded_here}"
    )
    return {
        "url": url, "status": "ok", "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": cards,
        "text_extracted_count": 0,
        "image_ocr_extracted_count": image_count,
        "image_ocr_failed_needs_review_count": image_failed_nr,
        "ocr_attempted": ocr_attempted,
        "ocr_success": ocr_success,
        "ocr_failed": ocr_failed,
        "page_kind": "shared_coupons", "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
def _empty_page_result(url: str, page_kind: str, service_hint: str, status: str) -> Dict:
    return {
        "url": url, "status": status, "rows": [],
        "excluded": 0, "cards_on_page": 0,
        "text_extracted_count": 0,
        "image_ocr_extracted_count": 0,
        "image_ocr_failed_needs_review_count": 0,
        "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
        "page_kind": page_kind, "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
_PAGE_KIND_DISPATCH = {
    "calgary_text": _scrape_calgary_text,
    "shared_coupons": _scrape_shared_coupons,
}


def _page_kind_for(url: str, link: Dict) -> str:
    k = (link.get("page_kind") or "").strip().lower()
    if k in _PAGE_KIND_DISPATCH:
        return k
    u = url.lower()
    if "mobil1calgary.com" in u:
        return "calgary_text"
    if "mobil1express.ca" in u:
        return "shared_coupons"
    return "shared_coupons"


def scrape_mobil1_lube_express_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = True,
) -> Dict:
    """Scrape Mobil 1 Lube Express (Calgary + Edmonton)."""
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
        pk = _page_kind_for(url, link if isinstance(link, dict) else {})
        expected_urls.append(url)

        if pk == "calgary_text":
            res = _scrape_calgary_text(
                url=url, service_hint=hint, excluded_log=excluded_log,
            )
        elif pk == "shared_coupons":
            res = _scrape_shared_coupons(
                url=url, service_hint=hint, excluded_log=excluded_log,
                ocr_cache=ocr_cache, enable_ocr=enable_ocr,
            )
        else:
            res = _empty_page_result(url, pk, hint, "unknown_page_kind")

        all_rows.extend(res["rows"])
        url_log.append({
            "url": url, "scope": _SOURCE_SCOPE,
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

    # Strict taxonomy enforcement (defensive — most rows already passed earlier
    # checks).
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
            sig_to_group[sig] = f"mobil1-{len(sig_to_group)+1:03d}"
    for r in all_rows:
        sig = r.pop("_signature_base")
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]

    if mode == "final_deduped":
        # Keep one row per (duplicate_group_id, city).
        kept_dedup: List[Dict] = []
        seen: set = set()
        for r in all_rows:
            key = (r.get("duplicate_group_id"), r.get("city"))
            if key in seen:
                continue
            seen.add(key)
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

    output_file = PROMOTIONS_DIR / "mobil1_lube_express_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(
        f"[mobil1-v2|{mode}] Saved {len(all_rows)} rows to {output_file}"
    )
    return result
