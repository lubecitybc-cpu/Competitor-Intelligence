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

