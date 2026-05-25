"""Quick Lane v2 scraper (Canadian sources — Edmonton + Grande Prairie).

Sources (confirmed Canadian, no US fallback):
  1. Edmonton:        https://edmonton-b6280.quicklane.ca/coupons
                      Tab/button page with 4 panes (Auto Services, Brake Centre,
                      Battery Centre, Tire Centre). All four tab-pane divs are
                      present in the HTML — we parse all of them and dedupe by
                      coupon node-ID so we don't only see the default tab.
  2. Grande Prairie:  https://quicklanewest.ca/ (+ internal service pages)
                      No dedicated coupon page; we crawl pages on the domain
                      that are likely to carry offers and apply a strict
                      concrete-offer signal so we don't keep marketing copy.

Every kept row is city_store-scoped to a single city. There is NO
regional / nationwide / fallback fan-out and NO US Quick Lane content.

Public entry point:
    scrape_quicklane_v2(competitor_v2, *,
                        mode="qa_expanded",
                        enable_ocr=True) -> Dict
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

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

logger = setup_logger(__name__, "quicklane_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

_BUSINESS_NAME = "Quick Lane"
_WEBSITE = "quicklane.ca"
_STORE = "Quick Lane"
_SOURCE_SCOPE = "city_store"

_ALLOWED_SERVICES = frozenset({
    "Battery", "Oil Change", "Brake", "Tire Sales", "Tire Rotation",
    "Transmission Fluid", "Radiator Flush", "Fuel System Flush", "Other",
})

_EDMONTON_HOST = "edmonton-b6280.quicklane.ca"
_GP_HOST = "quicklanewest.ca"

# Loose offer signal — first-pass gate that lets a candidate proceed.
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?(?:\s*(?:off|=|/))?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|\bup\s+to\s+\$\s*\d|"
    r"\bcoupons?\b|\bpromos?\b|\brebates?\b|\bdiscounts?\b|"
    r"\bsave\s+\$?\d|\bbonus\b|\bfree\b|"
    r"\bbuy\s+\d+,?\s*get\s+(?:the\s+)?\d+(?:st|nd|rd|th)?\s+free\b|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b|\boffer\s+ends?\b|\bends?\s+[A-Z][a-z]+\s+\d+\b|"
    r"\bfinancing\b|\bpackage\s+price\b|\bcomplimentary\b)",
    re.IGNORECASE,
)

# Concrete offer signal — the body MUST match this to be kept. Bare words
# like "free" or "save" on their own don't qualify.
_CONCRETE_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?\s*(?:off|=|/)|"
    r"\$\s*\d+(?:\.\d{1,2})?\b|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|\bup\s+to\s+\$\s*\d|"
    r"\bbuy\s+\d+,?\s*get\s+(?:the\s+)?\d+(?:st|nd|rd|th)?\s+free\b|"
    r"\bfree\s+(?:tire(?:s|\s+(?:storage|rotation|changeover|mount))?|"
    r"oil\s+change|brake(?:s|\s+service)?|battery(?:\s+(?:check|test))?|"
    r"alignment|wheel\s+alignment|wiper(?:s|\s+blades)?|"
    r"inspection|coolant\s+(?:flush|service))\b|"
    r"\bsave\s+\$\s*\d|\bget\s+\$\s*\d|"
    r"\bmail-?in\s+rebate\b|\brebates?\s+up\s+to\s+\$\s*\d|"
    r"\bcomplimentary\s+(?:pickup|delivery|inspection|check|test)|"
    r"\bno\s+payments?\s+for\s+\d+\s+months\b|"
    r"\bjoin\s+now\s+(?:and|to)\s+save\s+\$?\d)",
    re.IGNORECASE,
)

# Useless marketing/CTA phrases — drop on sight.
_NOISE_PHRASES = re.compile(
    r"(?:get\s+great\s+offers\s+right\s+to\s+your\s+inbox|"
    r"subscribe\b|\bnewsletter\b|\bsign\s*up\b|"
    r"price\s+match\s+promise|"
    r"^get\s+offer$|^view\s+offer$|^learn\s+more$|^read\s+more$|"
    r"hours\s+of\s+operation|get\s+in\s+touch|"
    r"^contact\s+us\b|^book\s+an?\s+appointment\b|"
    r"^find\s+your\s+perfect\s+tires)",
    re.IGNORECASE,
)

_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|special|discount|deal|save|"
    r"banner|\$\s*\d+|\d+\s*%)",
    re.IGNORECASE,
)

_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"google-?play|app-?store|badge|qr|emblem|review-star)",
    re.IGNORECASE,
)

_EXPIRY_RE = re.compile(
    r"(?:offer\s+ends?|expires?|valid\s+(?:until|through|thru)|ends?)\s*[:\-]?\s*"
    r"((?:[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})|"
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


def _normalize_money(text: str) -> str:
    """Collapse Edmonton's split ``<sup>$</sup><strong>140</strong><sup>.00</sup>``
    rendering — ``"$ 140 .00"`` — back into ``"$140.00"`` so the shared
    discount/rebate/percent regexes can recognize it.
    """
    if not text:
        return text
    out = re.sub(r"\$\s+(\d+)\s*\.\s*(\d{2})\b", r"$\1.\2", text)
    out = re.sub(r"\$\s+(\d+)(?!\d)", r"$\1", out)
    out = re.sub(r"(\d+)\s+%", r"\1%", out)
    return out


def _fetch_page(url: str) -> Tuple[str, List[str]]:
    res = fetch_with_firecrawl(url, timeout=90)
    if res.get("html") and not res.get("error"):
        return res["html"], res.get("images") or []
    logger.warning(f"[quicklane-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip(" .,") if m else None


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_expiry_date(expiry: str) -> Optional[date]:
    """Parse the strings extracted by _EXPIRY_RE into a date.

    Returns None if the string is unparseable (we err on the side of *keeping*
    the offer rather than silently dropping it)."""
    if not expiry:
        return None
    s = expiry.strip()
    # "May 30, 2026" / "May 30 2026" / "May 30th, 2026"
    m = re.match(
        r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        s,
    )
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                return None
    # "5/30/2026" or "5-30-26"
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd)
        except ValueError:
            return None
    return None


def _refine_service(service_hint: str, text: str) -> str:
    low = (text or "").lower()
    classified = classify_service(text) or "Other"

    # Strong text overrides (the body says "battery" / "brake" / etc.)
    overrides = [
        ("Battery", r"\bbatter"),
        ("Brake", r"\bbrakes?\b"),
        ("Tire Rotation", r"\btire\s+rotation\b|\brotation\b"),
        ("Tire Sales", r"\btire(?:s)?\b"),
        ("Oil Change", r"\boil\s+change\b|\bsynthetic\b|\blube\s+and\s+filter\b"),
        ("Transmission Fluid", r"\btransmission\s+fluid\b|\btransmission\s+(?:flush|service)\b"),
        ("Radiator Flush", r"\bcoolant\b|\bradiator\b"),
        ("Fuel System Flush", r"\bfuel\s+(?:system|injection|injector)\b"),
    ]
    # Skip "tire" override on rotation-only offers
    if re.search(r"\btire\s+rotation\b", low):
        return "Tire Rotation" if "Tire Rotation" in _ALLOWED_SERVICES else "Other"
    for svc, pat in overrides:
        if re.search(pat, low, re.IGNORECASE):
            return svc if svc in _ALLOWED_SERVICES else "Other"

    if classified in _ALLOWED_SERVICES and classified != "Other":
        return classified
    if service_hint in _ALLOWED_SERVICES:
        return service_hint
    return "Other"


# ---------------------------------------------------------------------------
# Edmonton extractor
# ---------------------------------------------------------------------------
def _edmonton_extract_coupons(html: str, page_url: str) -> List[Dict]:
    """Walk all four tab-panes on the Edmonton coupons page.

    The page ships every tab's content in the HTML; we read all of them and
    dedupe across panes by the coupon's `/coupon/{id}` URL. This satisfies
    the spec's "must activate each tab" rule without needing JS.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Map tab id -> tab label
    tab_labels: Dict[str, str] = {}
    for a in soup.select("ul.nav-tabs li a"):
        aria = (a.get("aria-controls") or "").strip()
        if aria:
            tab_labels[aria] = _clean(a.get_text(" ", strip=True))

    panes = soup.find_all("div", class_=lambda c: c and "tab-pane" in c.split())
    if not panes:
        # Some Quick Lane builds use a different wrapper — last-ditch grab.
        panes = soup.select("div.coupons-panes > div")
    if not panes:
        # Whole page as one container if no panes detected.
        panes = [soup]

    cards: Dict[str, Dict] = {}
    for pane in panes:
        pane_id = (pane.get("id") or "").strip()
        pane_label = tab_labels.get(pane_id) or pane_id or ""
        for art in pane.find_all("article", class_=lambda c: c and "coupon" in c.split()):
            # Coupon detail URL identifies the offer uniquely across tabs.
            link_el = art.find("a", href=re.compile(r"/coupon/\d+"))
            coupon_url = ""
            if link_el and link_el.get("href"):
                coupon_url = urljoin(page_url, link_el["href"])
            node_id = (art.get("id") or "").strip() or coupon_url

            title_el = art.find(["h3", "h2"])
            subtitle_el = art.find(["h4", "h5"])
            expire_el = art.find(
                lambda t: t.name == "div" and "expire" in " ".join(t.get("class") or [])
            )

            title = _clean(title_el.get_text(" ", strip=True)) if title_el else ""
            subtitle = _clean(subtitle_el.get_text(" ", strip=True)) if subtitle_el else ""
            expire_txt = _clean(expire_el.get_text(" ", strip=True)) if expire_el else ""

            if not title and not subtitle:
                continue

            key = node_id or (title + "|" + subtitle)
            existing = cards.get(key)
            tab_list = (existing or {}).get("tab_labels") or []
            if pane_label and pane_label not in tab_list:
                tab_list.append(pane_label)
            if existing is None:
                # Read images inside the card for OCR fallback (rare on Edmonton).
                imgs: List[str] = []
                for im in art.find_all("img"):
                    src = (
                        im.get("src") or im.get("data-src")
                        or im.get("data-lazy-src") or ""
                    )
                    if src and not src.startswith("data:"):
                        imgs.append(normalize_url(page_url, src))
                cards[key] = {
                    "node_id": node_id,
                    "coupon_url": coupon_url,
                    "title": title,
                    "subtitle": subtitle,
                    "expire_txt": expire_txt,
                    "raw_text": _clean(art.get_text(" ", strip=True)),
                    "tab_labels": tab_list,
                    "images": imgs,
                }
            else:
                existing["tab_labels"] = tab_list
    return list(cards.values())


# ---------------------------------------------------------------------------
# Grande Prairie extractor
# ---------------------------------------------------------------------------
def _gp_split_paragraphs(html: str) -> List[Dict]:
    """Return paragraph-like text candidates from a Grande Prairie page.

    We split on heading boundaries and ``<p>``/``<li>`` blocks. Each candidate
    keeps a snippet of the surrounding text so the offer-signal gates can
    inspect a meaningful unit (not single noisy sentences)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    blocks: List[Dict] = []
    seen: set = set()

    # Iterate elementor widgets / paragraphs / list items.
    for el in soup.find_all(
        ["section", "article", "div", "p", "li", "h1", "h2", "h3", "h4"]
    ):
        if not el.get_text(strip=True):
            continue
        # Skip wide containers — they balloon irrelevant copy together.
        text = _clean(el.get_text(" ", strip=True))
        if len(text) < 12 or len(text) > 600:
            continue
        sig = hash(text[:240])
        if sig in seen:
            continue
        seen.add(sig)
        blocks.append({"raw_text": text})
    return blocks


def _gp_extract_offers(html: str, page_url: str) -> Tuple[List[Dict], List[Dict]]:
    """Return (kept_cards, image_candidates) for a Grande Prairie page.

    Each kept card carries title/subtitle/raw_text/expiry just like the
    Edmonton extractor so the downstream builder is symmetric."""
    candidates = _gp_split_paragraphs(html)
    cards: List[Dict] = []
    seen_sig: set = set()

    for cand in candidates:
        raw = cand["raw_text"]
        if _NOISE_PHRASES.search(raw):
            continue
        if not _OFFER_SIGNAL.search(raw):
            continue
        if not _CONCRETE_OFFER_SIGNAL.search(raw):
            continue
        # Drop "they offer 4-wheel drive" / "we offer comprehensive..." —
        # generic verb usage of "offer".
        low = raw.lower()
        if re.search(r"\bthey\s+offer\b|\bwe\s+offer\b|\boffer\s+(?:a\s+)?comprehensive", low):
            # Allow only if there's a real numeric/discount/expiry signal alongside.
            if not re.search(r"\$\s*\d|\d+\s*%|\brebate|\bexpires|\bvalid\s+until\b", low):
                continue
        # Skip price-match-only marketing.
        if "price match" in low and not re.search(r"\$\s*\d|\d+\s*%", low):
            continue

        title = re.split(r"(?<=[.!?])\s+|\n", raw, maxsplit=1)[0][:160]
        body = raw[:600]
        expiry = _extract_expiry(raw)

        sig = hash(_signature_meaningful_tokens(title.lower())[:240])
        if sig in seen_sig:
            continue
        seen_sig.add(sig)

        cards.append({
            "node_id": "",
            "coupon_url": "",
            "title": title,
            "subtitle": "",
            "expire_txt": expiry or "",
            "raw_text": body,
            "tab_labels": [],
            "images": [],
        })

    # Image candidates for OCR (coupon-hinted images only)
    soup = BeautifulSoup(html, "html.parser")
    image_candidates: List[Dict] = []
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
        blob = " ".join([url, alt, cls])
        if not _PROMO_IMAGE_HINTS.search(blob):
            continue
        image_candidates.append({"url": url, "alt": alt})
    return cards, image_candidates


# ---------------------------------------------------------------------------
# Image download + OCR (shared)
# ---------------------------------------------------------------------------
def _download_image(url: str, *, referer: str,
                    dest_dir: Path = IMAGES_DIR) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = referer
    p = urlparse(referer)
    if p.scheme and p.netloc:
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fname = f"quicklane_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=20,
                         allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(
                f"[quicklane-v2] image fetch {r.status_code} for {url}"
            )
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[quicklane-v2] image download error for {url}: {e}")
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
            logger.warning(f"[quicklane-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


# ---------------------------------------------------------------------------
# Row builder + signature
# ---------------------------------------------------------------------------
def _signature_base(
    *, title: str, discount: Optional[str], expiry: Optional[str],
    service: str, city: str,
) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    # City is part of the signature so the same offer in two cities stays
    # as two distinct rows (city_store scope = per-store offer).
    return f"c={city}|s={service}|d={d}|e={e}|t={t}"


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
        "applicable_cities": [city],
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
        "source_image": source_image or "",
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


# ---------------------------------------------------------------------------
# Summary refinement — Quick Lane idioms
# ---------------------------------------------------------------------------
def _quicklane_summary(
    *, title: str, body: str, discount: Optional[str], service: str,
    expiry: Optional[str],
) -> str:
    text = f"{title} {body}".strip()
    low = text.lower()
    # When the offer doesn't fit the taxonomy we don't want phrases like
    # "$50 off other at Quick Lane" — write the noun out of the sentence.
    svc_phrase = f" {service.lower()}" if service != "Other" else ""
    rebate_phrase = (
        f" on {service.lower()}" if service != "Other" else ""
    )

    def _tail() -> str:
        return f" (expires {expiry})." if expiry else "."

    # Filter/package-price form: "$39.95 filters only - install extra"
    pkg_m = re.search(
        r"\$(\d+(?:\.\d{1,2})?)\s+(?:filters?\s+only|parts?\s+only|package)",
        text, re.IGNORECASE,
    )
    if pkg_m:
        item = "cabin and air filters" if "filter" in low else "package"
        return (
            f"{item.capitalize()} for ${pkg_m.group(1)} at Quick Lane"
            f" (install extra){_tail()}".replace(" .", ".")
        )

    rebate_m = re.search(
        r"(?:up\s+to\s+)?\$(\d+(?:\.\d{1,2})?)\s+(?:mail[- ]in\s+)?rebate",
        text, re.IGNORECASE,
    )
    if rebate_m:
        s = f"Up to ${rebate_m.group(1)} rebate{rebate_phrase} at Quick Lane"
        return s + _tail()

    buy_get_m = re.search(
        r"buy\s+(\d+),?\s*get\s+(?:the\s+)?(\d+(?:st|nd|rd|th)?)\s+free",
        text, re.IGNORECASE,
    )
    if buy_get_m:
        s = (
            f"Buy {buy_get_m.group(1)}, get the {buy_get_m.group(2)} free"
            f"{rebate_phrase} at Quick Lane"
        )
        return s + _tail()

    pct_m = re.search(r"(\d{1,2})\s*%\s*off", text, re.IGNORECASE)
    if pct_m:
        s = f"{pct_m.group(1)}% off{svc_phrase} at Quick Lane"
        return s + _tail()

    if "free tire storage" in low:
        s = "Free tire storage with 4-tire purchase at Quick Lane"
        return s + _tail()

    # "You Pick N" multi-service discount — name the structure.
    you_pick_m = re.search(
        r"you\s+pick(?:\s+(\d+))?[^$]*?\$(\d+(?:\.\d{1,2})?)",
        text, re.IGNORECASE,
    )
    if you_pick_m:
        n = you_pick_m.group(1)
        amt = you_pick_m.group(2)
        if n:
            s = (
                f"${amt} off when you choose {n} qualifying services "
                f"at Quick Lane (includes pickup and delivery)"
            )
        else:
            s = (
                f"Up to ${amt} off when bundling qualifying services "
                f"at Quick Lane (includes pickup and delivery)"
            )
        return s + _tail()

    if discount and discount.lower() != "free":
        s = f"{discount} off{svc_phrase} at Quick Lane"
        return s + _tail()

    if "complimentary" in low and ("pickup" in low or "delivery" in low):
        s = "Complimentary pickup and delivery service at Quick Lane"
        return s + _tail()

    if discount and discount.lower() == "free":
        s = f"Free{svc_phrase or ' service'} offer at Quick Lane"
        return s + _tail()

    # Fall back to the shared summarizer.
    fallback = _summarize_promo_description(
        promotion_title=title,
        offer_details=body,
        discount=discount,
        code=None,
        std_service=service,
        ad_text=body,
        brand="Quick Lane",
    )
    return fallback


# ---------------------------------------------------------------------------
# Per-URL scrape
# ---------------------------------------------------------------------------
def _scrape_one_url(
    *,
    url: str,
    city: str,
    service_hint: str,
    page_kind: str,
    today: date,
    enable_ocr: bool,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
) -> Dict:
    logger.info(f"[quicklane-v2] Fetch {city}/{page_kind} | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return {
            "url": url, "status": "fetch_failed", "rows": [],
            "excluded": 0, "cards_on_page": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
            "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
            "tab_pane_count": 0, "city": city, "page_kind": page_kind,
            "service_hint": service_hint,
        }

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    text_count = 0
    image_count = 0
    image_failed_nr = 0
    ocr_attempted = ocr_success = ocr_failed = 0

    if page_kind == "edmonton_tabs":
        cards = _edmonton_extract_coupons(html, url)
        gp_image_cands: List[Dict] = []
        soup_for_pane = BeautifulSoup(html, "html.parser")
        tab_pane_count = len(soup_for_pane.find_all(
            "div", class_=lambda c: c and "tab-pane" in c.split()
        ))
    else:
        cards, gp_image_cands = _gp_extract_offers(html, url)
        tab_pane_count = 0

    # ---- Text candidates --------------------------------------------------
    for c in cards:
        title = _normalize_money(c["title"])
        subtitle = _normalize_money(c["subtitle"])
        expire_txt = c["expire_txt"]
        raw_text_norm = _normalize_money(c.get("raw_text") or "")
        body_text = " ".join(
            x for x in (title, subtitle, expire_txt, raw_text_norm) if x
        )

        # Offer-signal gates
        if not _OFFER_SIGNAL.search(body_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "no_offer_signal",
                "source_image": "",
                "raw_text": body_text[:240],
            })
            continue
        if not _CONCRETE_OFFER_SIGNAL.search(body_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "no_concrete_offer_signal",
                "source_image": "",
                "raw_text": body_text[:240],
            })
            continue

        # Expiry: prefer expire_txt, fall back to mining the body.
        expiry = (
            _extract_expiry(expire_txt) or expire_txt.strip().rstrip(".") or None
        )
        if expiry and expiry.lower().startswith("expires "):
            expiry = expiry[len("expires "):].strip()
        if not expiry:
            expiry = _extract_expiry(body_text)

        # Drop offers whose expiry is clearly in the past.
        exp_date = _parse_expiry_date(expiry or "")
        if exp_date is not None and exp_date < today:
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "expired",
                "source_image": "",
                "raw_text": body_text[:240],
            })
            continue

        # Service classification
        if page_kind == "edmonton_tabs":
            # Use tab labels as a hint, then refine with full text.
            tab_hint = " ".join(c.get("tab_labels") or [])
            service = _refine_service(service_hint, body_text + " " + tab_hint)
        else:
            service = _refine_service(service_hint, body_text)

        # Drop if outside taxonomy (paranoia — _refine_service maps to allowed)
        if service not in _ALLOWED_SERVICES:
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "service_outside_taxonomy",
                "source_image": "",
                "raw_text": body_text[:240],
            })
            continue

        # Title preference: a real card title beats the subtitle.
        promo_title = title if len(title) >= 6 else (subtitle or title)
        if subtitle and subtitle.lower() != promo_title.lower():
            promo_title = f"{promo_title} — {subtitle}"
        promo_title = promo_title[:200]

        discount = _v2_extract_discount(body_text)
        if not discount and re.search(r"\bfree\b", body_text, re.IGNORECASE):
            discount = "free"
        code = _v2_extract_coupon_code(body_text)

        sig_local = _signature_base(
            title=promo_title, discount=discount, expiry=expiry,
            service=service, city=city,
        ) + f"|u={url}|m=text"
        if sig_local in seen_local:
            continue
        seen_local.add(sig_local)

        summary = _quicklane_summary(
            title=promo_title,
            body=body_text,
            discount=discount,
            service=service,
            expiry=expiry,
        )

        cross_sig = _signature_base(
            title=promo_title, discount=discount, expiry=expiry,
            service=service, city=city,
        )
        row = _build_row(
            page_url=url,
            city=city,
            service=service,
            title=promo_title,
            offer_details=body_text[:1000],
            raw_text=raw_text_norm or body_text,
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

    # ---- OCR fallback (Grande Prairie only, when enable_ocr=True) --------
    if enable_ocr and page_kind != "edmonton_tabs":
        for img in gp_image_cands:
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
                    city=city,
                )
                row = _build_row(
                    page_url=url,
                    city=city,
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
            if not (
                _OFFER_SIGNAL.search(ocr_text)
                and _CONCRETE_OFFER_SIGNAL.search(ocr_text)
            ):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "ocr_no_concrete_offer",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            ot = _clean(
                re.split(r"[\n\r]+", ocr_text.strip(), 1)[0]
            )[:180]
            service = _refine_service(service_hint, ocr_text)
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
            exp_date = _parse_expiry_date(expiry or "")
            if exp_date is not None and exp_date < today:
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": _SOURCE_SCOPE,
                    "extraction_method": "image_ocr",
                    "reason": "expired",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            summary = _quicklane_summary(
                title=ot, body=ocr_text, discount=discount,
                service=service, expiry=expiry,
            )
            cross_sig = _signature_base(
                title=ot, discount=discount, expiry=expiry,
                service=service, city=city,
            )
            sig_local = cross_sig + f"|u={url}|m=ocr|img={img_url}"
            if sig_local in seen_local:
                continue
            seen_local.add(sig_local)
            row = _build_row(
                page_url=url,
                city=city,
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

    cards_on_page = text_count + image_count + image_failed_nr
    logger.info(
        f"[quicklane-v2] {url}: text={text_count} img={image_count} "
        f"nr_fail={image_failed_nr} excl={excluded_here}"
    )
    return {
        "url": url,
        "status": "ok",
        "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": cards_on_page,
        "text_extracted_count": text_count,
        "image_ocr_extracted_count": image_count,
        "image_ocr_failed_needs_review_count": image_failed_nr,
        "ocr_attempted": ocr_attempted,
        "ocr_success": ocr_success,
        "ocr_failed": ocr_failed,
        "tab_pane_count": tab_pane_count,
        "city": city,
        "page_kind": page_kind,
        "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_quicklane_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = True,
) -> Dict:
    """Scrape Quick Lane (Edmonton + Grande Prairie, city_store)."""
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError(
            f"mode must be qa_expanded or final_deduped, got {mode!r}"
        )

    competitor_name = competitor_v2.get("competitor", _BUSINESS_NAME)
    today = date.today()
    all_rows: List[Dict] = []
    url_log: List[Dict] = []
    excluded_log: List[Dict] = []
    expected_urls: List[str] = []
    ocr_cache: Dict[str, str] = {}

    for link in competitor_v2.get("promo_links", []):
        if not isinstance(link, dict):
            logger.warning(f"[quicklane-v2] Unsupported link entry: {link!r}")
            continue
        url = link["url"]
        host = urlparse(url).netloc.lower()
        # Guard rail: refuse any URL outside the two confirmed Canadian hosts.
        if host not in (_EDMONTON_HOST, _GP_HOST):
            logger.warning(
                f"[quicklane-v2] Skipping out-of-scope URL: {url} (host={host})"
            )
            excluded_log.append({
                "url": url, "scope": _SOURCE_SCOPE,
                "extraction_method": "text",
                "reason": "out_of_scope_host",
                "source_image": "",
                "raw_text": "",
            })
            continue
        city = link.get("city") or (
            "Edmonton" if host == _EDMONTON_HOST else "Grande Prairie"
        )
        if city not in ("Edmonton", "Grande Prairie"):
            logger.warning(
                f"[quicklane-v2] Refusing unsupported city {city!r} for {url}; "
                "treating as Edmonton/GP based on host."
            )
            city = "Edmonton" if host == _EDMONTON_HOST else "Grande Prairie"
        hint = link.get("service_hint") or "Other"
        page_kind = link.get("page_kind") or (
            "edmonton_tabs" if host == _EDMONTON_HOST else "gp_service"
        )
        expected_urls.append(url)

        res = _scrape_one_url(
            url=url,
            city=city,
            service_hint=hint,
            page_kind=page_kind,
            today=today,
            enable_ocr=enable_ocr,
            excluded_log=excluded_log,
            ocr_cache=ocr_cache,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url,
            "scope": _SOURCE_SCOPE,
            "city": city,
            "service_hint": hint,
            "page_kind": page_kind,
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
            "tab_pane_count": res.get("tab_pane_count", 0),
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
            sig_to_group[sig] = f"qlane-{len(sig_to_group)+1:03d}"
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

    output_file = PROMOTIONS_DIR / "quicklane_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(
        f"[quicklane-v2|{mode}] Saved {len(all_rows)} rows to {output_file}"
    )
    return result
