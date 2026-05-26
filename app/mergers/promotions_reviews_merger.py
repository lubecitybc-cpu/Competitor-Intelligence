"""Merger for combining promotions, reviews, and AI Overview data for Google Sheets.

v2 path (default): scans PROMOTIONS_DIR for *_v2.json files produced by Phase 5
scrapers.  Each v2 row already has all 14 sheet columns populated; the merger
only fills the null `google_reviews` field from reviews data.

Legacy path: loads old-format JSON files via competitor_list.json for any
competitor not yet covered by a v2 scraper.
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from app.config.constants import DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "promotions_reviews_merger.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
REVIEWS_DIR = DATA_DIR / "reviews"
OUTPUT_DIR = DATA_DIR / "sheets_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 14 standard sheet columns (in order).  v2 rows carry all of these already.
SHEET_COLUMNS = [
    "website", "page_url", "business_name", "google_reviews",
    "service_name", "promo_description", "category",
    "offer_details", "ad_title", "ad_text",
    "new_or_updated", "date_scraped",
    "city", "extraction_method",
]

# Extra v2 QA columns appended after the 14 standard ones.
V2_EXTRA_COLUMNS = [
    "city", "store_name", "source_scope", "extraction_method",
    "confidence", "needs_review", "needs_review_reason",
    "discount_value", "coupon_code", "expiry_date",
    "promotion_title", "normalized_title", "applicable_cities",
    "duplicate_group_id", "duplicate_group_total",
]


# ---------------------------------------------------------------------------
# Review-lookup helpers
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Lower-case, keep only alphanumeric chars for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _find_review(business_name: str, reviews_data: List[Dict]) -> Dict:
    """Return the review dict whose business_name best matches *business_name*.

    Tries exact normalised match first, then substring containment in either
    direction.  Returns {} if nothing matches.
    """
    target = _norm(business_name)
    if not target:
        return {}
    for r in reviews_data:
        if _norm(r.get("business_name", "")) == target:
            return r
    for r in reviews_data:
        rn = _norm(r.get("business_name", ""))
        if rn and (rn in target or target in rn):
            return r
    return {}


_GENERIC_TITLES: set = {"check", "promotion", "offer", "deal", "na", "n/a", "none", ""}


def _make_offer_summary(row: Dict) -> str:
    """Return a short 1-2 sentence offer_details summary for a v2 row.

    Priority:
    1. ad_title when >= 15 chars and not generic (it's the scraper's headline)
    2. promo_description when <= 200 chars (already LLM-cleaned by the scraper)
    3. Existing offer_details when already short (<= 120 chars) and distinct from ad_text
    4. LLM call on ad_text as last resort
    5. First sentence fallback
    """
    ad_title = (row.get("ad_title") or "").strip()
    offer_details = (row.get("offer_details") or "").strip()
    ad_text = (row.get("ad_text") or "").strip()
    promo_desc = (row.get("promo_description") or "").strip()

    # 1. Use ad_title if it looks like a real headline (not just a service type label)
    if ad_title and 15 <= len(ad_title) <= 120 and ad_title.lower() not in _GENERIC_TITLES:
        return ad_title

    # 2. promo_description is already LLM-cleaned by the scraper — prefer it over long offer_details
    if promo_desc:
        if len(promo_desc) <= 200:
            return promo_desc
        # Promo desc too long — extract just the first sentence
        sentences = re.split(r'[.!?]+\s+', promo_desc.strip())
        first = sentences[0].strip() if sentences else ""
        if 15 <= len(first) <= 200:
            return first + ("." if first[-1] not in ".!?" else "")

    # 3. Keep existing offer_details if it's already a short, distinct snippet
    if offer_details and len(offer_details) <= 120 and offer_details != ad_text[:len(offer_details)]:
        return offer_details

    # 4. LLM summarisation (only when we have substantial text to summarise)
    text_to_summarise = ad_text or promo_desc or offer_details
    if text_to_summarise and len(text_to_summarise) > 30:
        try:
            from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
            result = clean_promo_text_with_llm(
                text_to_summarise,
                context="Write a short 1-2 sentence customer-facing summary of this automotive promo offer.",
            )
            if result and isinstance(result, dict):
                summary = (result.get("promo_description") or "").strip()
                if len(summary) > 10:
                    return summary[:200]
        except Exception as exc:
            logger.debug(f"LLM offer summary failed: {exc}")

    # 5. First sentence of promo_desc / offer_details as last resort
    source = promo_desc or offer_details or ad_text
    if source:
        sentences = re.split(r'[.!?]+\s+', source.strip())
        first = sentences[0].strip() if sentences else ""
        if 10 <= len(first) <= 200:
            return first + ("." if first[-1] not in ".!?" else "")
        return source[:150].strip()

    return offer_details  # keep whatever was there


def _enrich_v2_row(row: Dict, reviews_data: List[Dict]) -> Dict:
    """Fill null google_reviews and clean offer_details for a v2 promo row."""
    if not row.get("google_reviews"):
        review = _find_review(row.get("business_name", ""), reviews_data)
        row["google_reviews"] = format_google_reviews(
            review.get("google_review_stars"),
            review.get("google_review_count"),
        )
    row["offer_details"] = _make_offer_summary(row)
    return row


# ---------------------------------------------------------------------------
# v2 loading
# ---------------------------------------------------------------------------

def load_v2_promotions(reviews_data: List[Dict]) -> List[Dict]:
    """Scan PROMOTIONS_DIR for *_v2.json files and return enriched rows.

    Each file must have the structure ``{"promotions": [...], ...}`` produced
    by Phase 5 scrapers.  Rows are returned in file-name order.
    """
    all_rows: List[Dict] = []
    v2_files = sorted(PROMOTIONS_DIR.glob("*_v2.json"))
    if not v2_files:
        logger.warning(f"No *_v2.json files found in {PROMOTIONS_DIR}")
        return all_rows

    for path in v2_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            promos = data.get("promotions", [])
            enriched = [_enrich_v2_row(dict(r), reviews_data) for r in promos]
            all_rows.extend(enriched)
            logger.info(f"[v2] Loaded {len(enriched)} rows from {path.name}")
        except Exception as exc:
            logger.error(f"[v2] Failed to load {path.name}: {exc}", exc_info=True)

    return all_rows


def normalize_float(value: any) -> Optional[str]:
    """Normalize float value to string, return None if invalid."""
    try:
        if value is None or value == "" or str(value).upper() == "NA":
            return None
        return str(float(value))
    except (ValueError, TypeError):
        return None


def normalize_int(value: any) -> Optional[str]:
    """Normalize integer value to string, return None if invalid."""
    try:
        if value is None or value == "" or str(value).upper() == "NA":
            return None
        # Extract only digits
        cleaned = "".join(filter(str.isdigit, str(value)))
        return str(int(cleaned)) if cleaned else None
    except (ValueError, TypeError):
        return None


def format_google_reviews(stars: any, count: any) -> str:
    """Format Google reviews as '{stars} ⭐ | {count} reviews' or 'NA'."""
    stars_str = normalize_float(stars)
    count_str = normalize_int(count)
    
    if stars_str and count_str:
        return f"{stars_str} ⭐ | {count_str} reviews"
    return "NA"


def clean_text_with_llm(text: str) -> str:
    """Clean text using LLM cleaner if available, otherwise return original text."""
    if not text or len(text.strip()) < 20:
        return text
    
    try:
        from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
        
        # Clean with LLM
        cleaned_data = clean_promo_text_with_llm(text, context="Cleaning promo_description for display")
        
        if cleaned_data and isinstance(cleaned_data, dict):
            # Extract cleaned description
            cleaned_desc = cleaned_data.get("promo_description", "")
            if cleaned_desc and len(cleaned_desc.strip()) > 20:
                return cleaned_desc.strip()
        
        # If LLM cleaning didn't produce better result, return original
        return text
    except Exception as e:
        # If LLM cleaning fails, return original text
        logger.warning(f"LLM cleaning failed for promo_description: {e}")
        return text


def build_promo_description(promo: Dict) -> str:
    """
    Build promo_description (Column 6) - ALL DETAILS of the promotion, formatted clearly.
    
    Format:
    - Main description text
    - Discount value (if available)
    - Coupon code (if available)
    - Expiry date (if available)
    - Service details
    """
    # Get main description text - prefer ad_text as it has the most complete information
    ad_text = promo.get("ad_text", "").strip()
    offer_details = promo.get("offer_details", "").strip()
    existing_promo_desc = promo.get("promo_description", "").strip()
    
    # Use the most complete text available
    main_text = ""
    if ad_text and len(ad_text) > 50:
        main_text = ad_text
    elif offer_details and len(offer_details) > 50:
        main_text = offer_details
    elif existing_promo_desc and len(existing_promo_desc) > 50:
        main_text = existing_promo_desc
    
    # Clean main text with LLM if we have substantial content
    if main_text and len(main_text) > 50:
        main_text = clean_text_with_llm(main_text)
    
    # Extract structured fields (handle None values)
    discount_value = (promo.get("discount_value") or "").strip()
    coupon_code = (promo.get("coupon_code") or "").strip()
    expiry_date = (promo.get("expiry_date") or "").strip()
    service_name = (promo.get("service_name") or "").strip()
    promotion_title = (promo.get("promotion_title") or "").strip()
    
    # Clean up values - exclude "NA", "not specified", empty strings
    def is_valid_value(value: str) -> bool:
        if not value:
            return False
        value_upper = value.upper()
        return value_upper not in ["NA", "N/A", "NOT SPECIFIED", "NONE", ""]
    
    # Build formatted description
    formatted_parts = []
    
    # Start with main text or title
    if main_text:
        formatted_parts.append(main_text)
    elif promotion_title and promotion_title.upper() not in ["CHECK", "PROMOTION", "OFFER", "DEAL", ""]:
        formatted_parts.append(promotion_title)
    elif service_name and service_name.lower() not in ["other", "na", ""]:
        formatted_parts.append(f"{service_name.title()} Promotion")
    
    # Add structured information in a clear format
    details_parts = []
    
    # Discount value
    if is_valid_value(discount_value):
        details_parts.append(f"Discount: {discount_value}")
    
    # Coupon code
    if is_valid_value(coupon_code):
        details_parts.append(f"Coupon Code: {coupon_code}")
    
    # Expiry date
    if is_valid_value(expiry_date):
        # Format date nicely if it's in various formats
        expiry_formatted = expiry_date
        # Try to improve date format if needed
        if "/" in expiry_date or "-" in expiry_date:
            expiry_formatted = expiry_date
        details_parts.append(f"Expires: {expiry_formatted}")
    
    # Service name (if not already clearly mentioned in main text)
    if is_valid_value(service_name) and service_name.lower() not in ["other", "na"]:
        # Only add if it's not already clearly mentioned in main_text
        # Check if service name appears as a distinct word/phrase in main text
        if main_text:
            service_lower = service_name.lower()
            main_lower = main_text.lower()
            # Check if service appears as a distinct word (not just substring)
            # E.g., "tire" in "tires" is OK, but "oil change" should match "oil change service"
            service_words = service_lower.split()
            is_mentioned = any(
                word in main_lower and (
                    # Check if it's a complete word match
                    f" {word} " in f" {main_lower} " or
                    main_lower.startswith(f"{word} ") or
                    main_lower.endswith(f" {word}")
                )
                for word in service_words if len(word) > 3  # Skip short words like "oil"
            ) or len(service_lower) <= 10 and service_lower in main_lower  # Short service names
            
            if not is_mentioned:
                details_parts.append(f"Service: {service_name.title()}")
        else:
            # No main text, add service
            details_parts.append(f"Service: {service_name.title()}")
    
    # Combine everything
    if details_parts:
        # Format with separator for readability
        details_str = " | ".join(details_parts)
        
        # If we have main text, append structured details for clarity
        if formatted_parts:
            # Always append structured details for better visibility, even if mentioned in text
            # This ensures key info (discount, code, expiry) is always clearly visible
            return " | ".join(formatted_parts) + " | " + details_str
        else:
            return details_str
    
    # Fallback: return main text or build minimal description
    if formatted_parts:
        return formatted_parts[0]
    
    # Last resort: build from available fields
    fallback_parts = []
    if promotion_title and promotion_title.upper() not in ["CHECK", "PROMOTION", "OFFER", "DEAL", ""]:
        fallback_parts.append(promotion_title)
    if service_name and service_name.lower() not in ["other", "na", ""]:
        fallback_parts.append(f"Service: {service_name.title()}")
    
    if fallback_parts:
        return " | ".join(fallback_parts)
    
    return "Promotion available - see details"  # Final fallback


def get_ad_text(promo: Dict) -> str:
    """
    Get ad_text (Column 12) - full promotion details.
    
    Priority:
    1. promo.ad_text (full OCR/text content)
    2. promo.offer_details (full promotion description)
    3. promo.promo_description (fallback)
    """
    ad_text = promo.get("ad_text", "")
    if ad_text and len(ad_text.strip()) > 50:
        return ad_text.strip()
    
    offer_details = promo.get("offer_details", "")
    if offer_details and len(offer_details.strip()) > 50:
        return offer_details.strip()
    
    promo_description = promo.get("promo_description", "")
    if promo_description and len(promo_description.strip()) > 50:
        return promo_description.strip()
    
    return ""  # Empty string if no details available


def get_service_name(promo: Dict) -> str:
    """Get clean service name."""
    service = promo.get("service_name", "")
    if not service or service.lower() in ["other", "general", "na"]:
        category = promo.get("category", "")
        if category and category.lower() not in ["other", "general", "na"]:
            return category.title()
        return "All Services"
    return service.title()


def get_category(promo: Dict) -> str:
    """Get category, with fallback."""
    category = promo.get("category", "")
    if not category or category.lower() in ["na", "general"]:
        service = promo.get("service_name", "")
        if service and service.lower() not in ["other", "na"]:
            return service.title()
        return "Other"
    return category.title()


def format_date_scraped(date_str: Optional[str]) -> str:
    """Format date_scraped as YYYY-MM-DD."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    
    try:
        # Parse ISO format
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d")
    except:
        return datetime.now().strftime("%Y-%m-%d")


def merge_promotions_with_reviews_and_ai_overview(
    promotions_data: List[Dict],
    reviews_data: List[Dict],
    competitor_config: Dict
) -> List[Dict]:
    """
    Merge promotions with reviews and AI Overview data for Google Sheets.
    
    Args:
        promotions_data: List of promotion result dicts from scrapers
        reviews_data: List of review dicts from google_reviews_scraper
        competitor_config: Competitor config dict
    
    Returns:
        List of merged row dicts ready for Google Sheets
    """
    business_name = competitor_config.get("name", "")
    domain = competitor_config.get("domain", "")
    address = competitor_config.get("address", "")
    
    # Find reviews for this business
    reviews = next((r for r in reviews_data if r.get("business_name") == business_name), {})
    google_reviews_formatted = format_google_reviews(
        reviews.get("google_review_stars"),
        reviews.get("google_review_count")
    )
    
    # Handle different data structures
    if isinstance(promotions_data, dict):
        promo_results = [promotions_data]
    elif isinstance(promotions_data, list):
        promo_results = promotions_data
    else:
        promo_results = []
    
    # Get AI Overview data (business-level insights) as fallback - only use if no promotions found
    ai_overview_text = promo_results[0].get("google_ai_overview_text", "") if promo_results else ""
    if ai_overview_text:
        # Limit to 500 chars
        ai_overview_text = ai_overview_text[:500].strip()
    
    # Check if we have any actual promotions (not just AI Overview fallback)
    has_website_promotions = False
    total_promotions = 0
    for promo_result in promo_results:
        promotions = promo_result.get("promotions", [])
        # Filter out "CHECK" promotions from count
        valid_promos = [p for p in promotions if p.get("promotion_title", "").upper() != "CHECK"]
        if valid_promos:
            has_website_promotions = True
            total_promotions += len(valid_promos)
    
    # Process each promotion
    merged_rows = []
    
    for promo_result in promo_results:
        promotions = promo_result.get("promotions", [])
        
        for promo in promotions:
            # Skip "CHECK" placeholder promotions if we have real promotions
            if promo.get("promotion_title", "").upper() == "CHECK" and has_website_promotions:
                continue
            
            # Get offer_details: Small insight/summary of the promotion (max 200 chars)
            # Priority: First sentence/short summary from promo details → AI Overview business insights
            promo_offer_details = promo.get("offer_details", "")
            promo_ad_text = promo.get("ad_text", "")
            
            # Extract a short insight (first sentence or first 150 chars max)
            text_source = promo_ad_text or promo_offer_details
            if text_source and len(text_source.strip()) > 20:
                # Clean up text first
                text_source = text_source.strip()
                
                # Try to get first sentence (split by period, exclamation, question mark)
                import re
                sentences = re.split(r'[.!?]+\s+', text_source)
                first_sentence = sentences[0].strip() if sentences and sentences[0].strip() else ""
                
                # Use first sentence if it's reasonable length (20-200 chars)
                if len(first_sentence) >= 20 and len(first_sentence) <= 200:
                    offer_details_value = first_sentence
                    # Add period if not ending with punctuation
                    if first_sentence and first_sentence[-1] not in '.!?':
                        offer_details_value += "."
                else:
                    # Extract first 150 chars and try to end at word boundary
                    summary = text_source[:150].strip()
                    if len(text_source) > 150:
                        # Find last space before 150 chars
                        last_space = summary.rfind(' ')
                        if last_space > 50:  # Only truncate if we have enough content
                            summary = summary[:last_space]
                        offer_details_value = summary + "..."
                    else:
                        offer_details_value = summary
            else:
                # Fallback to AI Overview business insights (only when no promotion details)
                if ai_overview_text:
                    # Extract short summary from AI Overview (max 150 chars)
                    ai_sentences = re.split(r'[.!?]+\s+', ai_overview_text.strip())
                    ai_first = ai_sentences[0].strip() if ai_sentences and ai_sentences[0].strip() else ai_overview_text[:150].strip()
                    if len(ai_first) <= 200:
                        offer_details_value = ai_first + ("." if ai_first and ai_first[-1] not in '.!?' else "")
                    else:
                        offer_details_value = ai_first[:150].rsplit(' ', 1)[0] + "..."
                else:
                    # Build a minimal insight from available fields
                    insight_parts = []
                    discount = promo.get("discount_value", "")
                    if discount and discount.upper() not in ["NA", "NOT SPECIFIED", ""]:
                        insight_parts.append(f"{discount} off")
                    service = promo.get("service_name", "")
                    if service and service.lower() not in ["other", "na", ""]:
                        insight_parts.append(service.lower())
                    offer_details_value = " ".join(insight_parts) if insight_parts else "See promo_description for details"
            
            # Build row according to Google Sheets column guide
            row = {
                # Column 1: website
                "website": domain or "NA",
                
                # Column 2: page_url
                "page_url": promo.get("page_url") or competitor_config.get("url", "NA"),
                
                # Column 3: business_name
                "business_name": business_name,
                
                # Column 4: google_reviews
                "google_reviews": google_reviews_formatted,
                
                # Column 5: service_name
                "service_name": get_service_name(promo),
                
                # Column 6: promo_description ⭐ ALL DETAILS of the promotion
                "promo_description": build_promo_description(promo),
                
                # Column 7: category
                "category": get_category(promo),
                
                # Column 8: contact
                "contact": competitor_config.get("phone") or "NA",
                
                # Column 9: location
                "location": address or "NA",
                
                # Column 10: offer_details (small insight/summary of the promotion)
                "offer_details": offer_details_value,
                
                # Column 11: ad_title
                "ad_title": promo.get("ad_title", ""),
                
                # Column 12: ad_text ⭐ IMPORTANT FOR DETAILS
                "ad_text": get_ad_text(promo),
                
                # Column 13: new_or_updated
                "new_or_updated": promo.get("new_or_updated", "new"),
                
                # Column 14: date_scraped
                "date_scraped": format_date_scraped(promo.get("date_scraped") or promo_result.get("scraped_at")),
            }
            
            merged_rows.append(row)
    
    return merged_rows


# Competitors that have v2 scrapers.  The merger uses this to skip stale
# legacy JSON files for competitors that now have proper v2 output.
# Valvoline has no v2 scraper yet and is intentionally absent — it falls
# back to the legacy path if its JSON file is present.
_ACTIVE_COMPETITORS: set = {
    "jiffy lube", "lube town", "midas", "quick lane",
    "great canadian oil change", "econo lube", "mr. lube + tires",
    "mr. lube", "lubefx plus", "lubefx", "mobil 1 lube express",
}


_CITY_KEYWORDS: List[str] = ["Calgary", "Edmonton", "Grande Prairie"]


def _infer_city(competitor_config: Dict) -> Optional[str]:
    """Attempt to extract a known city name from the competitor's address field."""
    address = (competitor_config.get("address") or "").lower()
    for city in _CITY_KEYWORDS:
        if city.lower() in address:
            return city
    return None


def _covered_by_v2(business_name: str, v2_names_norm: set) -> bool:
    """Return True if *business_name* is already represented in v2 rows.

    Uses both exact normalised match and bidirectional substring containment
    so 'Mr. Lube' (legacy) is recognised as a duplicate of 'Mr. Lube + Tires' (v2).
    """
    target = _norm(business_name)
    if not target:
        return False
    if target in v2_names_norm:
        return True
    # Bidirectional substring: catches "mrlube" ⊂ "mrlubeandtires".
    for v2n in v2_names_norm:
        if (target in v2n) or (v2n in target):
            return True
    return False


_OCR_JUNK = re.compile(
    r'GET[\xa0\s]+COUPON|Fill in your information below[^.]*\.|'
    r'^\s*Coupon\s+|^\s*Offer\s*\n',
    re.IGNORECASE | re.MULTILINE,
)

# Single-word OCR label lines to strip when they appear alone on a line
_OCR_LABEL_LINE = re.compile(
    r'^(Shop|Online|Choose|Offer|Coupon|Print)\s*$',
    re.IGNORECASE | re.MULTILINE,
)


def _clean_ad_text(text: str) -> str:
    """Remove OCR artifacts, non-breaking spaces, and repeated UI junk from ad_text."""
    if not text:
        return text
    text = text.replace("\xa0", " ")
    text = _OCR_JUNK.sub("", text)
    text = _OCR_LABEL_LINE.sub("", text)
    # Collapse runs of 3+ newlines to double newline
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Collapse runs of spaces
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def _clean_date(value: Optional[str]) -> str:
    """Trim ISO timestamps to YYYY-MM-DD; return today's date on failure."""
    if not value:
        return datetime.now().strftime("%Y-%m-%d")
    # ISO timestamp: take the date part before 'T'
    if "T" in value:
        return value.split("T")[0]
    # Already looks like YYYY-MM-DD or close enough
    return value[:10] if len(value) >= 10 else datetime.now().strftime("%Y-%m-%d")


def merge_all_data(*, include_legacy: bool = True) -> List[Dict]:
    """Load all promotions and reviews, merge for Google Sheets.

    v2 scrapers are loaded first (all *_v2.json files).  If *include_legacy*
    is True, the legacy competitor_list.json path is also checked — but only
    for competitors whose scrapers have NOT yet been upgraded to v2 (currently
    only Valvoline).  Stale legacy files for deleted/replaced competitors are
    silently skipped.

    Returns:
        List of all merged rows for all competitors.
    """
    # Load all reviews once.
    reviews_file = REVIEWS_DIR / "all_reviews.json"
    if reviews_file.exists():
        reviews_data = json.loads(reviews_file.read_text(encoding="utf-8")).get("reviews", [])
    else:
        logger.warning(f"Reviews file not found: {reviews_file}")
        reviews_data = []

    all_merged_rows: List[Dict] = []

    # --- v2 path -----------------------------------------------------------
    v2_rows = load_v2_promotions(reviews_data)
    all_merged_rows.extend(v2_rows)
    logger.info(f"[v2] Total rows from v2 files: {len(v2_rows)}")

    v2_names_norm = {_norm(r.get("business_name", "")) for r in v2_rows}

    # --- legacy path -------------------------------------------------------
    if not include_legacy:
        return all_merged_rows

    config_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    if not config_file.exists():
        logger.warning(f"Legacy config not found: {config_file}")
        return all_merged_rows

    competitors = json.loads(config_file.read_text(encoding="utf-8"))

    for competitor in competitors:
        business_name = competitor.get("name", "")
        name_lower = (business_name or "").lower().strip()

        # Skip competitors now covered by v2 scrapers.
        if _covered_by_v2(business_name, v2_names_norm):
            logger.info(f"[legacy] Skipping {business_name!r} — covered by v2")
            continue

        # Skip stale legacy files for competitors no longer in scope.
        if name_lower not in _ACTIVE_COMPETITORS and name_lower != "valvoline express care":
            logger.info(f"[legacy] Skipping {business_name!r} — not in active competitor set")
            continue

        name_slug = business_name.lower().replace(" ", "_")
        promo_file = PROMOTIONS_DIR / f"{name_slug}.json"
        if not promo_file.exists():
            logger.warning(f"[legacy] Promotions file not found: {promo_file}")
            continue

        try:
            promo_data = json.loads(promo_file.read_text(encoding="utf-8"))
            promo_results = promo_data if isinstance(promo_data, list) else [promo_data]
            merged_rows = merge_promotions_with_reviews_and_ai_overview(
                promo_results, reviews_data, competitor
            )
            # Patch city from address if the scraper didn't set one.
            inferred_city = _infer_city(competitor)
            if inferred_city:
                for r in merged_rows:
                    if not r.get("city"):
                        r["city"] = inferred_city
                        r.setdefault("applicable_cities", [inferred_city])
            all_merged_rows.extend(merged_rows)
            logger.info(f"[legacy] Merged {len(merged_rows)} rows for {business_name}")
        except Exception as exc:
            logger.error(f"[legacy] Error merging {business_name}: {exc}", exc_info=True)

    # Final pass: clean up all rows before they go to the sheet
    for row in all_merged_rows:
        row["offer_details"] = _make_offer_summary(row)
        row["date_scraped"] = _clean_date(row.get("date_scraped"))
        row["ad_text"] = _clean_ad_text(row.get("ad_text") or "")

    return all_merged_rows


def _write_csv(rows: List[Dict], path: Path, fieldnames: List[str]) -> None:
    """Write *rows* to *path* as a CSV with *fieldnames* ordered first."""
    import csv
    all_keys = list(dict.fromkeys(
        fieldnames
        + [k for r in rows for k in r if k not in fieldnames]
    ))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k in all_keys:
                v = r.get(k)
                if v is None:
                    flat[k] = ""
                elif isinstance(v, (list, dict)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = str(v)
            w.writerow(flat)


def split_by_city(rows: List[Dict]) -> Dict[str, List[Dict]]:
    """Group rows by their exact city field value.

    Rows without a city field land in "Unknown".
    """
    groups: Dict[str, List[Dict]] = {}
    for row in rows:
        city = (row.get("city") or "Unknown").strip()
        groups.setdefault(city, []).append(row)
    return groups


def save_city_sheets(rows: List[Dict], output_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Save one CSV per city to *output_dir* (default: data/sheets_ready/).

    File names follow the pattern ``{city_slug}_promos.csv``.
    Returns a dict mapping city name → Path.
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    col_order = SHEET_COLUMNS + V2_EXTRA_COLUMNS
    city_files: Dict[str, Path] = {}
    by_city = split_by_city(rows)

    for city, city_rows in sorted(by_city.items()):
        slug = re.sub(r"[^a-z0-9]+", "_", city.lower()).strip("_")
        path = output_dir / f"{slug}_promos.csv"
        _write_csv(city_rows, path, col_order)
        city_files[city] = path
        logger.info(f"[city-split] {city}: {len(city_rows)} rows → {path.name}")

    return city_files


def save_merged_data(rows: List[Dict], output_file: Optional[Path] = None) -> Path:
    """Save merged data to JSON + main CSV + per-city CSVs."""
    if output_file is None:
        output_file = OUTPUT_DIR / "promotions_merged_for_sheets.json"

    output_data = {
        "merged_at": datetime.now().isoformat(),
        "total_rows": len(rows),
        "rows": rows,
    }
    output_file.write_text(json.dumps(output_data, indent=2, default=str), encoding="utf-8")
    logger.info(f"Saved {len(rows)} merged rows to {output_file}")

    # Main CSV (all cities combined).
    if rows:
        col_order = SHEET_COLUMNS + V2_EXTRA_COLUMNS
        csv_path = output_file.with_suffix(".csv")
        _write_csv(rows, csv_path, col_order)
        logger.info(f"Saved main CSV to {csv_path}")

        # Per-city CSVs.
        save_city_sheets(rows, output_dir=output_file.parent)

    return output_file


if __name__ == "__main__":
    import sys
    print("=" * 70)
    print("Promotions merger — v2 + legacy")
    print("=" * 70)
    rows = merge_all_data()

    # Summary by competitor
    from collections import Counter
    by_biz: Counter = Counter(r.get("business_name", "?") for r in rows)
    print(f"\nTotal rows: {len(rows)}")
    for name, n in sorted(by_biz.items()):
        print(f"  {name:<40}: {n}")

    output_file = save_merged_data(rows)
    csv_path = output_file.with_suffix(".csv")
    print(f"\nJSON: {output_file}")
    print(f"CSV : {csv_path}")

    if rows:
        sample = rows[0]
        print(f"\nSample ({sample.get('business_name')}):")
        print(f"  promo_description : {(sample.get('promo_description') or '')[:90]!r}")
        print(f"  google_reviews    : {sample.get('google_reviews')}")
        print(f"  city              : {sample.get('city')}")
        print(f"  discount_value    : {sample.get('discount_value')}")
        print(f"  coupon_code       : {sample.get('coupon_code')}")
        print(f"  expiry_date       : {sample.get('expiry_date')}")

