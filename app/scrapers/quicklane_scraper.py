"""Quick Lane Tire & Auto Center v2 scraper (Phase 5).

Text-only extraction. Each /en-us/savings/coupons-offers-rebates/*-coupons/
page is a national service page; valid offers are fanned out to all
applicable cities (Edmonton, Grande Prairie by default).

Public entry point:
    scrape_quicklane_v2(competitor_v2, *, mode="qa_expanded")
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
    _summarize_promo_description,
    _normalize_discount,
    _confidence_from_promo,
    _signature_meaningful_tokens,
)

logger = setup_logger(__name__, "quicklane_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


_OFFER_INDICATORS = re.compile(
    r"(?:\bcoupons?\b|\brebates?\b|\bsave\b|\bdiscount\b|\bfree\b|"
    r"\bfinancing\b|\bpromotion\b|\bspecial\b|\bdeal\b|\boffer\b|\bpromo\b|"
    r"\blimited[- ]time\b|\bexpires?\b|\bvalid\s+through\b|\bbonus\b|"
    r"\$\s*\d|\d+\s*%\s*off\b|\bget\s+\$?\d|\bup\s+to\s+\$?\d|"
    r"\brewards?\s+points?\b|\bMSRP\b)",
    re.IGNORECASE,
)

_US_ONLY_PATTERNS = re.compile(
    r"(?:\bU\.?\s*S\.?(?:A\.?)?\s+only\b|\bUnited\s+States\s+only\b|"
    r"\bvalid\s+only\s+in\s+(?:the\s+)?U\.?\s*S\.?(?:A\.?)?\b|"
    r"\bnot\s+valid\s+in\s+Canada\b|\bexcluding\s+Canada\b|"
    r"\bdoes\s+not\s+apply\s+in\s+Canada\b)",
    re.IGNORECASE,
)

_SERVICE_HINT_FROM_URL = {
    "battery-coupons": "Battery",
    "oil-change-coupons": "Oil Change",
    "brake-coupons": "Brake",
    "tire-coupons": "Tire Sales",
}


def _fetch_html(url: str) -> str:
    res = fetch_with_firecrawl(url, timeout=60)
    if res.get("html") and not res.get("error"):
        return res["html"]
    logger.warning(f"Firecrawl failed for {url}: {res.get('error')}")
    return ""


def _extract_offer_cards(html: str) -> List[Dict]:
    """Return one dict per `<li class='offer-detail'>` card on the page."""
    soup = BeautifulSoup(html, "html.parser")
    cards: List[Dict] = []
    for li in soup.find_all("li", class_=lambda c: c and "offer-detail" in c):
        full_text = li.get_text("\n", strip=True)
        if not full_text or len(full_text) < 8:
            continue
        # Split lines, drop the leading 'Offer' eyebrow + the 'View Offer' CTA.
        lines = [ln.strip() for ln in full_text.split("\n") if ln.strip()]
        lines = [ln for ln in lines if ln.lower() not in {"offer", "view offer"}]
        if not lines:
            continue

        # Pull expiry sub-block.
        expiry: Optional[str] = None
        body_lines: List[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.lower() == "expires" and i + 1 < len(lines):
                m = re.search(
                    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                    r"[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
                    lines[i + 1],
                )
                if m:
                    expiry = m.group(1)
                    i += 2
                    continue
            body_lines.append(ln)
            i += 1

        title = body_lines[0] if body_lines else ""
        body = " ".join(body_lines)
        cards.append({
            "title": title,
            "body": body,
            "raw_text": full_text,
            "expiry": expiry,
            "html": str(li)[:2500],
        })
    return cards


def _signature(*, title: str, discount: Optional[str], expiry: Optional[str],
               service: str, page_url: str) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"u={page_url}|s={service}|d={d}|e={e}|t={t}"


def _build_row(
    *,
    competitor: str,
    page_url: str,
    city: str,
    service: str,
    title: str,
    offer_details: str,
    raw_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    region_applicability: str,
    needs_review_reason: Optional[str],
) -> Dict:
    row: Dict = {
        # Existing sheet columns
        "website": "quicklane.com",
        "page_url": page_url,
        "business_name": competitor,
        "google_reviews": "",
        "service_name": service,
        "promo_description": offer_details,
        "category": service,
        "contact": "National",
        "location": "National",
        "offer_details": offer_details,
        "ad_title": title,
        "ad_text": raw_text[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA metadata
        "city": city,
        "store_name": "National",
        "source_scope": "national_service",
        "extraction_method": "text",
        "confidence": None,
        "needs_review": bool(needs_review_reason),
        "needs_review_reason": needs_review_reason or "",
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": title,
        "normalized_title": (title or "").lower().strip(),
        "applicable_cities": ["Edmonton", "Grande Prairie"],
        "duplicate_group_id": None,  # filled by orchestrator
        "duplicate_group_total": 0,  # filled by orchestrator
        "region_applicability": region_applicability,
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


def _refine_summary(summary: str, *, title: str, body: str, service: str) -> str:
    """Tighten the generic 'off' phrasing for Quick Lane offers.

    The shared summarizer always says '$X off <service> at Quick Lane.' That's
    wrong when X is an MSRP price, a 'starting at' value, a 'rebate', or a
    'Ford Rewards Points' redemption. Detect those patterns and rewrite.
    """
    text = (title + " " + body)

    rebate_m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)\s+(?:mail[- ]in\s+)?rebate", text, re.IGNORECASE)
    if rebate_m:
        return f"${rebate_m.group(1)} rebate on {service.lower()} at Quick Lane."

    starting_m = re.search(r"\bstarting\s+at\s+\$\s*(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
    if starting_m:
        return f"{service} starting at ${starting_m.group(1)} MSRP at Quick Lane."

    msrp_m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)\s+MSRP", text, re.IGNORECASE)
    if msrp_m:
        return f"{service} from ${msrp_m.group(1)} MSRP at Quick Lane."

    points_m = re.search(r"\b(\d{1,3}(?:,\d{3})*|\d+)\s+Ford\s+Rewards?\s+Points?\b",
                         text, re.IGNORECASE)
    if points_m and "off" in summary.lower():
        return f"Earn or redeem Ford Rewards Points on {service.lower()} at Quick Lane."

    # "Get four $50 instant service discounts" → "$50 off <service> at Quick Lane (×4)."
    multi_m = re.search(r"\bget\s+(?:four|three|two|four\s*\(4\))\s+\$\s*(\d+(?:\.\d{1,2})?)",
                        text, re.IGNORECASE)
    if multi_m:
        return f"${multi_m.group(1)} off {service.lower()} at Quick Lane (4× discounts for Ford Employees)."

    # "Have your battery tested at no charge" → free check
    if re.search(r"\bat\s+no\s+charge\b|\bfor\s+free\b|\bcomplimentary\b",
                 text, re.IGNORECASE):
        return f"Free {service.lower()} check at Quick Lane."

    return summary


def _scrape_one_page(
    *,
    url: str,
    service_hint: str,
    competitor: str,
    excluded_log: List[Dict],
) -> Dict:
    logger.info(f"[quicklane-v2] Fetching {service_hint} | {url}")
    html = _fetch_html(url)
    if not html:
        return {"url": url, "status": "fetch_failed", "rows": [], "excluded": 0}

    cards = _extract_offer_cards(html)
    rows: List[Dict] = []
    excluded_here = 0
    seen_local_sigs: set = set()

    for card in cards:
        title = card["title"]
        body = card["body"]
        raw_text = card["raw_text"]

        if not _OFFER_INDICATORS.search(raw_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": "national_service", "extraction_method": "text",
                "reason": "no_offer_indicator", "raw_text": raw_text[:240],
            })
            continue

        discount = _v2_extract_discount(raw_text)
        code = _v2_extract_coupon_code(raw_text)
        expiry = card["expiry"]

        us_only = bool(_US_ONLY_PATTERNS.search(raw_text))
        needs_review_reason = "possible_us_only_offer" if us_only else None
        region_applicability = "us_only_flagged" if us_only else "assumed_canada_ok"

        # Service decision: trust the URL hint unless the text clearly says
        # a different category. Run the classifier on the full text and only
        # override the hint when the result is something obvious.
        classified = classify_service(title + " " + body)
        service = service_hint
        if classified and classified != "Other" and classified != service_hint:
            text_lower = (title + " " + body).lower()
            if (classified == "Battery" and "batter" in text_lower) or \
               (classified == "Brake" and "brake" in text_lower) or \
               (classified == "Oil Change" and "oil" in text_lower) or \
               (classified == "Tire Sales" and ("tire" in text_lower or "tyre" in text_lower)) or \
               (classified == "Tire Rotation" and "rotation" in text_lower):
                service = classified
                logger.info(
                    f"[quicklane-v2] Service override on {url}: hint={service_hint} → "
                    f"classified={classified}  title={title[:80]!r}"
                )

        local_sig = _signature(
            title=title, discount=discount, expiry=expiry,
            service=service, page_url=url,
        )
        if local_sig in seen_local_sigs:
            continue
        seen_local_sigs.add(local_sig)

        summary = _summarize_promo_description(
            promotion_title=title,
            offer_details=body,
            discount=discount,
            code=code,
            std_service=service,
            ad_text=raw_text,
            brand="Quick Lane",
        )
        summary = _refine_summary(summary, title=title, body=body, service=service)
        # Edmonton + Grande Prairie fan-out
        for city in ("Edmonton", "Grande Prairie"):
            row = _build_row(
                competitor=competitor,
                page_url=url,
                city=city,
                service=service,
                title=title,
                offer_details=body[:1000],
                raw_text=raw_text,
                discount=discount,
                code=code,
                expiry=expiry,
                region_applicability=region_applicability,
                needs_review_reason=needs_review_reason,
            )
            row["promo_description"] = summary
            row["_signature_base"] = local_sig
            rows.append(row)

    return {
        "url": url,
        "status": "ok",
        "rows": rows,
        "excluded": excluded_here,
        "cards_on_page": len(cards),
    }


def scrape_quicklane_v2(competitor_v2: Dict, *, mode: str = "qa_expanded") -> Dict:
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError("mode must be qa_expanded or final_deduped")

    competitor_name = competitor_v2.get("competitor", "Quick Lane Tire & Auto Center")
    all_rows: List[Dict] = []
    url_log: List[Dict] = []
    excluded_log: List[Dict] = []
    expected_urls: List[str] = []

    for link in competitor_v2.get("promo_links", []):
        url = link["url"]
        hint = link.get("service_hint")
        if not hint:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            hint = _SERVICE_HINT_FROM_URL.get(slug, "Other")
        expected_urls.append(url)

        res = _scrape_one_page(
            url=url, service_hint=hint, competitor=competitor_name,
            excluded_log=excluded_log,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url, "scope": "national_service",
            "service_hint": hint,
            "status": res["status"],
            "cards_on_page": res.get("cards_on_page", 0),
            "added_rows": len(res["rows"]),
            "excluded_count": res.get("excluded", 0),
        })

    # Build duplicate_group_id + duplicate_group_total. The signature already
    # includes page_url, so cross-page duplicates do NOT collapse here.
    sig_to_group: Dict[str, str] = {}
    sig_counts: Dict[str, int] = {}
    for r in all_rows:
        sig = r["_signature_base"]
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        if sig not in sig_to_group:
            sig_to_group[sig] = f"qlane-{len(sig_to_group)+1:03d}"

    for r in all_rows:
        sig = r["_signature_base"]
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]
        r.pop("_signature_base", None)

    # Final-deduped mode: collapse city fan-out to one row per offer.
    if mode == "final_deduped":
        kept: List[Dict] = []
        seen_groups: set = set()
        for r in all_rows:
            gid = r["duplicate_group_id"]
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
            r["city"] = ", ".join(r.get("applicable_cities") or [])
            kept.append(r)
        all_rows = kept

    # Validation -------------------------------------------------------------
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

    exclusion_reason_counts: Dict[str, int] = {}
    for x in excluded_log:
        exclusion_reason_counts[x["reason"]] = exclusion_reason_counts.get(x["reason"], 0) + 1

    unique_promo_descriptions = len({(r.get("promo_description") or "").strip().lower() for r in all_rows})

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
            "unique_promo_descriptions": unique_promo_descriptions,
            "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
            "possible_us_only_offer_count": sum(
                1 for r in all_rows if r.get("needs_review_reason") == "possible_us_only_offer"
            ),
            "duplicate_group_total": len(sig_to_group),
            "service_count_by_category": svc_counts,
            "extraction_method_counts": method_counts,
            "excluded_row_count": len(excluded_log),
            "excluded_reason_counts": exclusion_reason_counts,
            "url_log": url_log,
            "excluded_rows": excluded_log,
        },
    }
    output_file = PROMOTIONS_DIR / "quicklane_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[quicklane-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
