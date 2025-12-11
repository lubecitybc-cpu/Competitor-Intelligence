"""Merger for combining promotions, reviews, and AI Overview data for Google Sheets."""
import json
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


def build_promo_description(promo: Dict) -> str:
    """
    Build promo_description (Column 6) - one-line summary.
    
    Priority:
    1. promo.promotion_title (if not generic/CHECK)
    2. First sentence from promo.offer_details or promo.ad_text
    3. Build from discount_value + service_name
    """
    title = promo.get("promotion_title", "").strip()
    
    # Skip generic titles
    if title and title.upper() not in ["CHECK", "PROMOTION", "OFFER", "DEAL", ""]:
        # Use title if it's descriptive (longer than 5 chars)
        if len(title) > 5:
            return title
    
    # Try first sentence from offer_details or ad_text
    offer_details = promo.get("offer_details", "")
    ad_text = promo.get("ad_text", "")
    
    text_source = offer_details or ad_text
    if text_source:
        # Extract first sentence
        sentences = text_source.split('.')
        if sentences and len(sentences[0].strip()) > 20:
            return sentences[0].strip() + "."
        
        # If no sentence break, use first 150 chars
        if len(text_source.strip()) > 20:
            return text_source[:150].strip() + ("..." if len(text_source) > 150 else "")
    
    # Build from discount + service
    discount = promo.get("discount_value")
    service = promo.get("service_name", "service")
    
    if discount and discount.upper() != "NA":
        return f"{discount} off {service}"
    
    return "Promotion available"  # Fallback


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
            
            # Get offer_details: Use promotion details if available, otherwise AI Overview as fallback
            # Priority: promo.offer_details → promo.ad_text → AI Overview business insights
            promo_offer_details = promo.get("offer_details", "")
            promo_ad_text = promo.get("ad_text", "")
            
            # Use promotion details (full description)
            if promo_offer_details and len(promo_offer_details.strip()) > 20:
                offer_details_value = promo_offer_details[:1000].strip()  # Limit to 1000 chars
            elif promo_ad_text and len(promo_ad_text.strip()) > 20:
                offer_details_value = promo_ad_text[:1000].strip()  # Limit to 1000 chars
            else:
                # Fallback to AI Overview business insights (only when no promotion details)
                offer_details_value = ai_overview_text if ai_overview_text else "Not available"
            
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
                
                # Column 6: promo_description ⭐ MOST IMPORTANT
                "promo_description": build_promo_description(promo),
                
                # Column 7: category
                "category": get_category(promo),
                
                # Column 8: contact
                "contact": competitor_config.get("phone") or "NA",
                
                # Column 9: location
                "location": address or "NA",
                
                # Column 10: offer_details (promotion details, or AI Overview as fallback)
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


def merge_all_data() -> List[Dict]:
    """
    Load all promotions, reviews, and configs, then merge for Google Sheets.
    
    Returns:
        List of all merged rows for all competitors
    """
    # Load competitor config
    config_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    competitors = json.loads(config_file.read_text())
    
    # Load all reviews
    reviews_file = REVIEWS_DIR / "all_reviews.json"
    if reviews_file.exists():
        reviews_data = json.loads(reviews_file.read_text()).get("reviews", [])
    else:
        logger.warning(f"Reviews file not found: {reviews_file}")
        reviews_data = []
    
    # Load all promotions
    all_merged_rows = []
    
    for competitor in competitors:
        business_name = competitor.get("name", "")
        name_slug = business_name.lower().replace(" ", "_")
        promo_file = PROMOTIONS_DIR / f"{name_slug}.json"
        
        if not promo_file.exists():
            logger.warning(f"Promotions file not found: {promo_file}")
            continue
        
        try:
            promo_data = json.loads(promo_file.read_text())
            
            # Handle both single dict and list format
            if isinstance(promo_data, list):
                promo_results = promo_data
            else:
                promo_results = [promo_data]
            
            merged_rows = merge_promotions_with_reviews_and_ai_overview(
                promo_results,
                reviews_data,
                competitor
            )
            
            all_merged_rows.extend(merged_rows)
            logger.info(f"Merged {len(merged_rows)} rows for {business_name}")
            
        except Exception as e:
            logger.error(f"Error merging data for {business_name}: {e}", exc_info=True)
    
    return all_merged_rows


def save_merged_data(rows: List[Dict], output_file: Optional[Path] = None) -> Path:
    """Save merged data to JSON file."""
    if output_file is None:
        output_file = OUTPUT_DIR / "promotions_merged_for_sheets.json"
    
    output_data = {
        "merged_at": datetime.now().isoformat(),
        "total_rows": len(rows),
        "rows": rows
    }
    
    output_file.write_text(json.dumps(output_data, indent=2, default=str))
    logger.info(f"Saved {len(rows)} merged rows to {output_file}")
    
    return output_file


if __name__ == "__main__":
    print("🔄 Merging promotions, reviews, and AI Overview data...")
    rows = merge_all_data()
    output_file = save_merged_data(rows)
    print(f"✅ Merged {len(rows)} rows saved to: {output_file}")
    
    # Show sample
    if rows:
        print("\n📊 Sample merged row:")
        sample = rows[0]
        print(f"   Business: {sample.get('business_name')}")
        print(f"   Promo Description: {sample.get('promo_description')[:80]}...")
        print(f"   Ad Text: {sample.get('ad_text')[:80] if sample.get('ad_text') else 'N/A'}...")
        print(f"   Google Reviews: {sample.get('google_reviews')}")

