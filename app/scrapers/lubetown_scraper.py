"""Lube Town v2 scraper (Phase 5).

Calgary-only, single URL, pure text extraction.
DOM anchor: ``div.coupon_sec_inner`` — one per coupon card.

Public entry point:
    scrape_lubetown_v2(competitor_v2, *, mode="qa_expanded") -> Dict
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from app.config.constants import DATA_DIR
from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.utils.logging_utils import setup_logger
from app.utils.service_classifier import classify_service
from app.scrapers.jiffy_scraper import (
    _v2_extract_discount,
    _v2_extract_coupon_code,
    _normalize_discount,
    _confidence_from_promo,
    _signature_meaningful_tokens,
)

logger = setup_logger(__name__, "lubetown_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

_BUSINESS_NAME = "Lube Town"
_WEBSITE = "lubetown.com"
_DEFAULT_CITY = "Calgary"
_DEFAULT_STORE = "Lube Town"

# Minimum signals that a card is a real coupon/promotion.
_OFFER_SIGNAL = re.compile(
    r"(?:\boff\b|\bsave\b|\bdiscount\b|\bcoupon\b|\brebate\b|\bfree\b|"
    r"\bpromo\b|\bspecial\b|\blimited\s+time\b|\bexpires?\b|\bbonus\b|"
    r"\bpackage\s+price\b|\b\$\s*\d|\d+\s*%\s*off\b)",
    re.IGNORECASE,
)

# Noise lines to strip from offer_details (T&C boilerplate that adds no value).
_NOISE_RE = re.compile(
    r"^(?:Lube\s+Town|coupon\s+must\s+be\s+presented|can\s+not\s+be\s+combined|"
    r"no\s+cash\s+value|limited\s+time\s+offer\.?)$",
    re.IGNORECASE,
)


def _fetch_html(url: str) -> str:
    res = fetch_with_firecrawl(url, timeout=60)
    if res.get("html") and not res.get("error"):
        logger.info(f"[lubetown-v2] Firecrawl OK: {len(res['html'])} chars")
        return res["html"]
    logger.warning(f"[lubetown-v2] Firecrawl failed for {url}: {res.get('error')}")
    return ""


def _extract_coupon_cards(html: str) -> List[Dict]:
    """Parse all ``div.coupon_sec_inner`` blocks into structured dicts."""
    soup = BeautifulSoup(html, "html.parser")
    cards: List[Dict] = []

    for card in soup.find_all("div", class_="coupon_sec_inner"):
        # Title is in div.coupon_title; fall back to first line of text.
        title_el = card.find(class_="coupon_title")
        title = title_el.get_text(" ", strip=True) if title_el else ""

        lines = [ln.strip() for ln in card.get_text(separator="\n").splitlines() if ln.strip()]
        if not lines:
            continue

        if not title:
            title = lines[0]

        # Parse Code and Expires from lines.
        code: Optional[str] = None
        expiry: Optional[str] = None
        body_lines: List[str] = []
        for ln in lines:
            m_code = re.match(r"^Code\s*:\s*(.+)$", ln, re.IGNORECASE)
            m_exp = re.match(r"^Expires?\s+on\s*:\s*(.+)$", ln, re.IGNORECASE)
            if m_code:
                code = m_code.group(1).strip()
            elif m_exp:
                expiry = m_exp.group(1).strip()
            elif ln.lower() == title.lower() or re.match(r"^lube\s+town$", ln, re.IGNORECASE):
                continue
            else:
                body_lines.append(ln)

        # body = terms sentence(s); strip pure boilerplate lines for offer_details.
        offer_lines = [ln for ln in body_lines if not _NOISE_RE.match(ln)]
        offer_details = " ".join(offer_lines).strip() or " ".join(body_lines).strip()
        raw_text = "\n".join(lines)

        # Normalise expiry to YYYY-MM-DD when possible (input: M/D/YYYY).
        expiry_iso: Optional[str] = None
        if expiry:
            m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", expiry)
            if m:
                expiry_iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            else:
                expiry_iso = expiry

        cards.append({
            "title": title,
            "offer_details": offer_details or offer_details,
            "raw_text": raw_text,
            "code": code,
            "expiry": expiry_iso,
        })

    logger.info(f"[lubetown-v2] Parsed {len(cards)} coupon cards from HTML")
    return cards


def _signature(*, title: str, discount: Optional[str], expiry: Optional[str],
               service: str, page_url: str) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"u={page_url}|s={service}|d={d}|e={e}|t={t}"


def _build_promo_description(
    *,
    title: str,
    discount: Optional[str],
    code: Optional[str],
    service: str,
    expiry: Optional[str],
) -> str:
    """Short factual customer-facing summary, ≤20 words."""
    # Title already encodes the offer cleanly (e.g. "$15 OFF FULL SYNTHETIC OIL CHANGE").
    # Produce a sentence form: "$15 off full synthetic oil change at Lube Town (code 15OFF)."
    base = title.strip().rstrip("*")
    # Title case + lower service part.
    m = re.match(r"^(\$\d+(?:\.\d+)?)\s+OFF\s+(.+)$", base, re.IGNORECASE)
    if m:
        amount = m.group(1)
        what = m.group(2).strip().title()
        desc = f"{amount} off {what} at Lube Town"
    elif discount:
        desc = f"{discount} off {service.lower()} at Lube Town"
    else:
        desc = f"{base.title()} at Lube Town"

    if code:
        desc += f" (code {code})"
    desc = desc.rstrip(".")
    return desc + "."


def _build_row(
    *,
    page_url: str,
    title: str,
    offer_details: str,
    raw_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    service: str,
    promo_description: str,
    needs_review_reason: Optional[str],
) -> Dict:
    row: Dict = {
        # Sheet-compatible columns
        "website": _WEBSITE,
        "page_url": page_url,
        "business_name": _BUSINESS_NAME,
        "google_reviews": None,
        "service_name": service,
        "promo_description": promo_description,
        "category": service,
        "contact": "",
        "location": _DEFAULT_STORE,
        "offer_details": offer_details,
        "ad_title": title,
        "ad_text": raw_text[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA / meta columns
        "city": _DEFAULT_CITY,
        "store_name": _DEFAULT_STORE,
        "source_scope": "city_store",
        "extraction_method": "text",
        "confidence": None,
        "needs_review": bool(needs_review_reason),
        "needs_review_reason": needs_review_reason or "",
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": title,
        "normalized_title": re.sub(r"\s+", " ", (title or "").lower().strip()),
        "applicable_cities": [_DEFAULT_CITY],
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


def _scrape_one_url(
    url: str,
    *,
    excluded_log: List[Dict],
) -> Dict:
    logger.info(f"[lubetown-v2] Fetching {url}")
    html = _fetch_html(url)
    if not html:
        return {"url": url, "status": "fetch_failed", "rows": [], "excluded": 0, "cards_on_page": 0}

    cards = _extract_coupon_cards(html)
    rows: List[Dict] = []
    excluded_here = 0
    seen_sigs: set = set()

    for card in cards:
        title = card["title"]
        offer_details = card["offer_details"]
        raw_text = card["raw_text"]
        code = card["code"]
        expiry = card["expiry"]

        if not _OFFER_SIGNAL.search(raw_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": "city_store", "extraction_method": "text",
                "reason": "no_offer_signal", "raw_text": raw_text[:300],
            })
            logger.info(f"[lubetown-v2] Excluded (no offer signal): {title[:60]!r}")
            continue

        discount = _v2_extract_discount(raw_text)
        # If regex missed it, try to pull from title like "$15 OFF"
        if not discount:
            m = re.search(r"\$(\d+(?:\.\d+)?)\s+OFF", title, re.IGNORECASE)
            if m:
                discount = f"${m.group(1)}"

        service = classify_service(title + " " + offer_details)

        sig = _signature(title=title, discount=discount, expiry=expiry,
                         service=service, page_url=url)
        if sig in seen_sigs:
            logger.info(f"[lubetown-v2] Skipping duplicate within page: {title[:60]!r}")
            continue
        seen_sigs.add(sig)

        promo_desc = _build_promo_description(
            title=title, discount=discount, code=code,
            service=service, expiry=expiry,
        )

        row = _build_row(
            page_url=url,
            title=title,
            offer_details=offer_details,
            raw_text=raw_text,
            discount=discount,
            code=code,
            expiry=expiry,
            service=service,
            promo_description=promo_desc,
            needs_review_reason=None,
        )
        rows.append(row)
        logger.info(f"[lubetown-v2] Extracted: {title!r} d={discount!r} c={code!r} exp={expiry!r}")

    logger.info(
        f"[lubetown-v2] {url} → {len(rows)} rows, excluded={excluded_here}"
    )
    return {
        "url": url,
        "status": "ok",
        "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": len(cards),
    }


def scrape_lubetown_v2(competitor_v2: Dict, *, mode: str = "qa_expanded") -> Dict:
    """Phase 5 entry point for Lube Town.

    Args:
        competitor_v2: Entry from ``app/config/competitors.v2.json``.
        mode: ``"qa_expanded"`` (default) or ``"final_deduped"``.
              For Lube Town (single URL, single city) both modes produce
              identical output.
    Returns:
        Standard result dict with ``promotions`` and ``validation``.
    """
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError(f"mode must be qa_expanded or final_deduped, got {mode!r}")

    competitor_name = competitor_v2.get("competitor", _BUSINESS_NAME)
    all_rows: List[Dict] = []
    url_log: List[Dict] = []
    excluded_log: List[Dict] = []
    expected_urls: List[str] = []

    for link in competitor_v2.get("promo_links", []):
        url = link["url"] if isinstance(link, dict) else link
        expected_urls.append(url)

        res = _scrape_one_url(url, excluded_log=excluded_log)
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url,
            "scope": "city_store",
            "status": res["status"],
            "cards_on_page": res["cards_on_page"],
            "added_rows": len(res["rows"]),
            "excluded_count": res["excluded"],
        })

    # Assign duplicate_group_id / duplicate_group_total.
    sig_to_group: Dict[str, str] = {}
    sig_counts: Dict[str, int] = {}
    for r in all_rows:
        sig = _signature(
            title=r["promotion_title"],
            discount=r["discount_value"],
            expiry=r["expiry_date"],
            service=r["service_name"],
            page_url=r["page_url"],
        )
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        if sig not in sig_to_group:
            sig_to_group[sig] = f"lt-{len(sig_to_group)+1:03d}"
        r["_sig"] = sig

    for r in all_rows:
        sig = r.pop("_sig")
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]

    # Validation ----------------------------------------------------------------
    processed = {e["url"] for e in url_log if e["status"] == "ok"}
    failed = [e["url"] for e in url_log if e["status"] == "fetch_failed"]
    missing = sorted(set(expected_urls) - {e["url"] for e in url_log})

    row_count_by_url: Dict[str, int] = {}
    row_count_by_city: Dict[str, int] = {}
    svc_counts: Dict[str, int] = {}
    method_counts: Dict[str, int] = {}
    for r in all_rows:
        u = r["page_url"]
        row_count_by_url[u] = row_count_by_url.get(u, 0) + 1
        c = r.get("city") or ""
        row_count_by_city[c] = row_count_by_city.get(c, 0) + 1
        s = r.get("service_name") or ""
        svc_counts[s] = svc_counts.get(s, 0) + 1
        m = r.get("extraction_method") or ""
        method_counts[m] = method_counts.get(m, 0) + 1

    excl_reason_counts: Dict[str, int] = {}
    for x in excluded_log:
        excl_reason_counts[x["reason"]] = excl_reason_counts.get(x["reason"], 0) + 1

    unique_descs = sorted({(r.get("promo_description") or "").strip() for r in all_rows})

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
            "unique_promo_descriptions": unique_descs,
            "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
            "duplicate_group_total": len(sig_to_group),
            "service_count_by_category": svc_counts,
            "extraction_method_counts": method_counts,
            "excluded_row_count": len(excluded_log),
            "excluded_reason_counts": excl_reason_counts,
            "ocr_attempted": 0,
            "url_log": url_log,
            "excluded_rows": excluded_log,
        },
    }

    output_file = PROMOTIONS_DIR / "lubetown_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[lubetown-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
