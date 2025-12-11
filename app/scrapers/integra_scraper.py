"""Integra Tire Auto Centre scraper - Image OCR for tire rebates."""
import json
from pathlib import Path
from typing import List, Dict
from datetime import datetime
import hashlib
import re

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.html_parser import find_images_by_css_selector
from app.extractors.images.image_downloader import download_image, get_image_hash, normalize_url
from app.extractors.ocr.ocr_processor import ocr_image, detect_promo_keywords
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import PROMO_KEYWORDS, DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "integra_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def normalize_title(title: str) -> str:
    """Normalize title for deduplication."""
    return " ".join(title.lower().strip().split())


def extract_rebate_details_from_text(text: str, alt_text: str = "") -> Dict:
    """Extract rebate details from OCR text or alt text."""
    # Try OCR text first, fallback to alt text
    source_text = text if text and len(text.strip()) > 10 else alt_text
    if not source_text:
        return {}
    
    text_lower = source_text.lower()
    
    # Extract rebate amount ($)
    rebate_amount = None
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', source_text)
    if dollar_match:
        rebate_amount = f"${dollar_match.group(1)}"
    
    # Extract percentage discount
    percent_match = re.search(r'(\d+)\s*%', source_text)
    if percent_match and not rebate_amount:
        rebate_amount = f"{percent_match.group(1)}%"
    
    # Extract brand name (look for common tire brands)
    tire_brands = [
        "michelin", "bridgestone", "goodyear", "continental", "pirelli",
        "bfgoodrich", "toyo", "nitto", "hankook", "falken", "kumho",
        "yokohama", "dunlop", "firestone", "general", "cooper", "uniroyal"
    ]
    brand_name = None
    for brand in tire_brands:
        if brand in text_lower:
            brand_name = brand.title()
            break
    
    # Extract expiry date
    expiry_date = None
    # Try various date patterns
    date_patterns = [
        r'(?:expires?|expiry|valid until|until)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, source_text, re.IGNORECASE)
        if date_match:
            expiry_date = date_match.group(1).strip()
            break
    
    # Extract eligibility (look for common terms)
    eligibility = None
    if "mail" in text_lower or "rebate" in text_lower:
        eligibility = "Mail-in rebate"
    elif "instant" in text_lower:
        eligibility = "Instant rebate"
    
    return {
        "rebate_amount": rebate_amount,
        "brand_name": brand_name,
        "expiry_date": expiry_date,
        "eligibility": eligibility
    }


def process_integra_promotions(competitor: Dict) -> List[Dict]:
    """Process Integra Tire promotions using image OCR - enhanced for all rebate images."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    seen_image_urls = set()
    seen_promo_signatures = set()  # Use signature (brand + amount + image hash) for deduplication
    seen_image_hashes = set()
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Step 1: Fetch with Firecrawl (try with JS rendering if available)
        firecrawl_result = fetch_with_firecrawl(promo_url, timeout=90)
        
        if firecrawl_result.get("error"):
            logger.error(f"Firecrawl error: {firecrawl_result['error']}")
            # Try fallback to ZenRows with JS rendering
            try:
                from app.config.constants import ZENROWS_API_KEY
                if ZENROWS_API_KEY:
                    import requests
                    zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={promo_url}&js_render=true&wait=3000"
                    response = requests.get(zenrows_url, timeout=45)
                    response.raise_for_status()
                    html = response.text
                    logger.info("Successfully fetched with ZenRows (JS rendering)")
                else:
                    continue
            except Exception as e:
                logger.warning(f"Fallback fetch failed: {e}")
                continue
        else:
            html = firecrawl_result.get("html", "")
            if not html:
                logger.warning(f"No HTML content from Firecrawl for {promo_url}")
                continue
        
        # Step 2: Try multiple CSS selectors to find rebate images
        images = []
        
        # Primary selector
        primary_images = find_images_by_css_selector(html, promo_url, "img.single-rebate")
        images.extend(primary_images)
        logger.info(f"Found {len(primary_images)} images with selector 'img.single-rebate'")
        
        # Additional selectors for robustness
        additional_selectors = [
            "img[class*='rebate']",
            "img[class*='promo']",
            "img[alt*='rebate']",
            "img[alt*='tire']",
            ".rebate img",
            ".promotion img",
            "div.rebate img",
            "div[class*='rebate'] img"
        ]
        
        for selector in additional_selectors:
            try:
                additional_images = find_images_by_css_selector(html, promo_url, selector)
                # Only add if not already found
                existing_urls = {img["image_url"].lower() for img in images}
                new_images = [img for img in additional_images if img["image_url"].lower() not in existing_urls]
                if new_images:
                    logger.info(f"Found {len(new_images)} additional images with selector '{selector}'")
                    images.extend(new_images)
            except Exception as e:
                logger.debug(f"Selector '{selector}' failed: {e}")
                continue
        
        logger.info(f"Total {len(images)} unique rebate images found")
        
        if not images:
            logger.warning(f"No images found with any selector on {promo_url}")
            continue
        
        # Step 3: Process each image
        for img_data in images:
            image_url = img_data["image_url"]
            alt_text = img_data.get("alt_text", "")
            
            # Normalize image URL for deduplication
            normalized_img_url = normalize_url(promo_url, image_url).lower().strip()
            
            # Skip if we've seen this image URL before
            if normalized_img_url in seen_image_urls:
                logger.info(f"Skipping duplicate image URL: {image_url[:80]}...")
                continue
            seen_image_urls.add(normalized_img_url)
            
            # Download image
            logger.info(f"Downloading image: {image_url[:80]}...")
            img_path = download_image(normalize_url(promo_url, image_url))
            
            if not img_path:
                logger.warning(f"Failed to download image: {image_url}")
                continue
            
            # Check for duplicate image (same file content)
            img_hash = get_image_hash(img_path)
            if img_hash and img_hash in seen_image_hashes:
                logger.info(f"Skipping duplicate image content: {image_url}")
                img_path.unlink()
                continue
            seen_image_hashes.add(img_hash)
            
            # Step 4: Run OCR
            logger.info(f"Running OCR on {img_path.name}...")
            ocr_text = ocr_image(img_path)
            
            # Step 5: If OCR fails, try alt text as fallback
            if not ocr_text or len(ocr_text.strip()) < 10:
                if alt_text and len(alt_text.strip()) > 10:
                    logger.info(f"OCR failed, using alt text as fallback")
                    ocr_text = alt_text
                else:
                    logger.warning(f"No OCR text or alt text extracted from {image_url}")
                    img_path.unlink()
                    continue
            
            # Step 6: Check if it's promo-related
            # For tire rebates, be more lenient - check if we have brand name, rebate amount, or keywords
            is_promo = detect_promo_keywords(ocr_text, PROMO_KEYWORDS)
            rebate_details_check = extract_rebate_details_from_text(ocr_text, alt_text)
            
            # Also consider it a promo if we found rebate amount or brand name
            if not is_promo and not rebate_details_check.get("rebate_amount") and not rebate_details_check.get("brand_name"):
                # Last check: if alt text contains tire brand or rebate keywords
                alt_lower = alt_text.lower()
                has_tire_brand = any(brand in alt_lower for brand in ["tire", "bridgestone", "michelin", "goodyear", "bfgoodrich", "continental", "pirelli", "toyo", "falken", "hankook", "kumho", "yokohama", "dunlop", "firestone", "general", "cooper", "uniroyal", "nexen", "hercules"])
                has_rebate_keyword = any(kw in alt_lower for kw in ["rebate", "off", "discount", "save", "promo"])
                
                if not has_tire_brand and not has_rebate_keyword:
                    logger.info(f"Image doesn't contain promo keywords or rebate details: {image_url}")
                    img_path.unlink()
                    continue
            
            # Step 7: Extract rebate details from text
            rebate_details = extract_rebate_details_from_text(ocr_text, alt_text)
            
            # Step 8: Clean with LLM
            context = f"Integra Tire rebate promotion. Alt text: {alt_text}"
            cleaned_data = clean_promo_text_with_llm(ocr_text, context)
            
            # Build promotion title - be more specific to avoid duplicates
            if cleaned_data and cleaned_data.get("service_name"):
                base_title = cleaned_data.get("service_name")
                # Add brand if available
                if rebate_details.get("brand_name") and rebate_details["brand_name"].lower() not in base_title.lower():
                    promotion_title = f"{rebate_details['brand_name']} {base_title}"
                else:
                    promotion_title = base_title
            elif rebate_details.get("brand_name"):
                # Include amount in title for uniqueness
                amount_str = f" - {rebate_details['rebate_amount']}" if rebate_details.get("rebate_amount") else ""
                promotion_title = f"{rebate_details['brand_name']} Tire Rebate{amount_str}"
            elif alt_text:
                # Use alt text but make it unique if we have amount
                amount_str = f" - {rebate_details['rebate_amount']}" if rebate_details.get("rebate_amount") else ""
                promotion_title = f"{alt_text[:80]}{amount_str}"
            else:
                # Extract first line or key phrase from OCR
                first_line = ocr_text.split("\n")[0].strip()[:100]
                amount_str = f" - {rebate_details['rebate_amount']}" if rebate_details.get("rebate_amount") else ""
                promotion_title = f"{first_line if first_line else 'Tire Rebate'}{amount_str}"
            
            # Create a unique signature for deduplication: brand + amount + image hash
            brand = (rebate_details.get("brand_name") or "").lower() if rebate_details and rebate_details.get("brand_name") else ""
            amount = (rebate_details.get("rebate_amount") or "").lower() if rebate_details and rebate_details.get("rebate_amount") else ""
            
            # Use discount_value from LLM if available
            if cleaned_data and cleaned_data.get("discount_value"):
                amount = cleaned_data.get("discount_value", "").lower()
            
            # Create signature: brand + amount + first part of image hash
            promo_signature = f"{brand}|{amount}|{str(img_hash)[:8] if img_hash else 'no-hash'}"
            
            # Skip if we've seen this exact signature before
            if promo_signature in seen_promo_signatures:
                logger.info(f"Skipping duplicate signature: {promo_signature}")
                img_path.unlink()
                continue
            seen_promo_signatures.add(promo_signature)
            
            # Also check if same brand and amount but different image (likely same promo, different image)
            if brand and amount:
                brand_amount_sig = f"{brand}|{amount}"
                # Only skip if we already have this exact brand+amount combo AND images are similar
                for existing in all_promos:
                    existing_brand = (existing.get("rebate_details", {}).get("brand_name", "") or "").lower()
                    existing_amount = (existing.get("discount_value", "") or "").lower()
                    existing_sig = f"{existing_brand}|{existing_amount}"
                    
                    if brand_amount_sig == existing_sig and brand_amount_sig != "|":
                        # Same brand and amount - check if titles are very similar
                        existing_title = normalize_title(existing.get("promotion_title", ""))
                        current_title = normalize_title(promotion_title)
                        
                        # If titles are >90% similar, skip (same promo)
                        from fuzzywuzzy import fuzz
                        similarity = fuzz.ratio(existing_title, current_title)
                        if similarity > 90:
                            logger.info(f"Skipping similar promo: {promotion_title} (similarity: {similarity}%)")
                            img_path.unlink()
                            break
                else:
                    # No break = not a duplicate
                    pass
            
            # Use LLM cleaned data if available, otherwise use extracted details
            if cleaned_data:
                discount_value = cleaned_data.get("discount_value") or rebate_details.get("rebate_amount")
                expiry_date = cleaned_data.get("expiry_date") or rebate_details.get("expiry_date")
                offer_details = cleaned_data.get("promo_description") or ocr_text[:1000]
            else:
                # Fallback: use basic extraction
                discount_value = rebate_details.get("rebate_amount")
                expiry_date = rebate_details.get("expiry_date")
                offer_details = ocr_text[:1000]
            
            # Build promo object
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": rebate_details.get("brand_name", "tires"),
                "promo_description": offer_details,
                "category": "tires",
                "contact": competitor.get("address", ""),
                "location": competitor.get("address", ""),
                "offer_details": offer_details,
                "ad_title": promotion_title,
                "ad_text": alt_text[:200],
                "new_or_updated": "new",
                "date_scraped": datetime.now().isoformat(),
                "image_url": image_url,
                "discount_value": discount_value,
                "coupon_code": cleaned_data.get("coupon_code") if cleaned_data else None,
                "expiry_date": expiry_date,
                "image_path": str(img_path),
                "extraction_method": "image_ocr",
                "rebate_details": rebate_details,
                "promotion_title": promotion_title
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
    
    logger.info(f"Total unique promotions found: {len(all_promos)}")
    return all_promos


def scrape_integra(competitor: Dict) -> Dict:
    """Main entry point for Integra Tire scraper."""
    try:
        promos = process_integra_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'integra').lower().replace(' ', '_')}.json"
        result = {
            "competitor": competitor.get("name"),
            "scraped_at": datetime.now().isoformat(),
            "promotions": promos,
            "count": len(promos)
        }
        
        output_file.write_text(json.dumps(result, indent=2, default=str))
        logger.info(f"Saved {len(promos)} promotions to {output_file}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error scraping Integra Tire: {e}", exc_info=True)
        return {
            "competitor": competitor.get("name"),
            "error": str(e),
            "promotions": [],
            "count": 0
        }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Load competitor data
    competitor_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    competitors = json.loads(competitor_file.read_text())
    
    # Find Integra Tire
    integra = next((c for c in competitors if "integra" in c.get("name", "").lower()), None)
    
    if not integra:
        logger.error("Integra Tire Auto Centre not found in competitor list")
        sys.exit(1)
    
    result = scrape_integra(integra)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

