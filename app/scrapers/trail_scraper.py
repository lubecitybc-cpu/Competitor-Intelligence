"""Trail Tire Auto Centres scraper - Banner image OCR for tire promotions."""
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

logger = setup_logger(__name__, "trail_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def normalize_title(title: str) -> str:
    """Normalize title for deduplication."""
    return " ".join(title.lower().strip().split())


def calculate_ocr_hash(ocr_text: str) -> str:
    """Calculate hash of OCR text for similarity checking."""
    normalized = " ".join(ocr_text.lower().strip().split())
    return hashlib.md5(normalized.encode()).hexdigest()


def are_texts_similar(text1: str, text2: str, threshold: int = 85) -> bool:
    """Check if two OCR texts are similar."""
    if not text1 or not text2:
        return False
    
    from fuzzywuzzy import fuzz
    similarity = fuzz.token_set_ratio(text1.lower(), text2.lower())
    return similarity >= threshold


def _extract_image_url(img, base_url: str) -> str:
    """Extract image URL from img element, trying multiple attributes."""
    from urllib.parse import urljoin
    
    # Try multiple attributes in priority order
    for attr in ["data-src", "data-lazy-src", "data-original", "src", "data-url"]:
        if img.get(attr) and img.get(attr).strip():
            url = urljoin(base_url, img.get(attr))
            if url and url.strip() and url.strip() != "/":
                return url
    
    # Try srcset
    if img.get("srcset"):
        srcset = img.get("srcset")
        first_url = srcset.split(",")[0].strip().split()[0]
        url = urljoin(base_url, first_url)
        if url and url.strip():
            return url
    
    return None


def extract_promo_details_from_text(text: str) -> Dict:
    """Extract promotion details from OCR text."""
    text_lower = text.lower()
    
    # Extract discount value
    discount_value = None
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', text)
    if dollar_match:
        discount_value = f"${dollar_match.group(1)}"
    else:
        percent_match = re.search(r'(\d+)\s*%', text)
        if percent_match:
            discount_value = f"{percent_match.group(1)}%"
        elif "free" in text_lower:
            discount_value = "free"
    
    # Extract coupon code (look for patterns like "CODE: ABC123" or "Use code XYZ")
    coupon_code = None
    code_patterns = [
        r'code[:\s]+([A-Z0-9]{3,20})',
        r'coupon[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
    ]
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            coupon_code = match.group(1).upper()
            break
    
    # Extract expiry date
    expiry_date = None
    date_patterns = [
        r'(?:expires?|expiry|valid until|until|ends?)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, text, re.IGNORECASE)
        if date_match:
            expiry_date = date_match.group(1).strip()
            break
    
    return {
        "discount_value": discount_value,
        "coupon_code": coupon_code,
        "expiry_date": expiry_date
    }


def extract_brand_name(text: str) -> str:
    """Extract tire brand name from text."""
    tire_brands = [
        "michelin", "bridgestone", "goodyear", "continental", "pirelli",
        "bfgoodrich", "toyo", "nitto", "hankook", "falken", "kumho",
        "yokohama", "dunlop", "firestone", "general", "cooper", "uniroyal",
        "nexen", "hercules", "laufenn", "sumitomo", "sailun"
    ]
    
    text_lower = text.lower()
    for brand in tire_brands:
        if brand in text_lower:
            return brand.title()
    return ""


def process_trail_promotions(competitor: Dict) -> List[Dict]:
    """Process Trail Tire promotions using banner image OCR - enhanced method."""
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
        
        # Step 1: Fetch with Firecrawl (try with JS rendering fallback)
        firecrawl_result = fetch_with_firecrawl(promo_url, timeout=90)
        
        if firecrawl_result.get("error"):
            logger.warning(f"Firecrawl error: {firecrawl_result['error']}, trying fallback...")
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
        
        # Step 2: Find banner images using multiple methods for robustness
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        
        images = []
        seen_urls = set()
        
        # Method 1: Primary selector - divs with "probox" class (includes both promotion_width and other promo divs)
        promo_divs = soup.find_all("div", class_=lambda x: x and "probox" in x)
        logger.info(f"Found {len(promo_divs)} promo divs with 'probox' class")
        
        for div in promo_divs:
            imgs = div.find_all("img")
            for img in imgs:
                image_url = _extract_image_url(img, promo_url)
                if image_url and image_url.lower().strip() not in seen_urls:
                    normalized_url = image_url.lower().strip()
                    # Skip invalid URLs (page URL itself)
                    if normalized_url == promo_url.lower() or normalized_url == promo_url.lower() + "/" or normalized_url.endswith("/promotions/"):
                        continue
                    # Skip logos and non-promo images
                    if any(skip in normalized_url for skip in ["logo", "icon"]):
                        continue
                    # Must be a valid image URL (has extension or is from wp-content/uploads)
                    has_extension = any(ext in normalized_url for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"])
                    is_upload = "wp-content" in normalized_url or "uploads" in normalized_url
                    
                    if has_extension or is_upload:
                        seen_urls.add(normalized_url)
                        images.append({
                            "image_url": image_url,
                            "alt_text": img.get("alt", "")
                        })
                        logger.debug(f"Found promo image: {image_url.split('/')[-1]}")
        
        # Method 2: Try CSS selector approach
        try:
            css_images = find_images_by_css_selector(html, promo_url, "div.probox img")
            for img_data in css_images:
                if img_data["image_url"].lower().strip() not in seen_urls:
                    seen_urls.add(img_data["image_url"].lower().strip())
                    images.append(img_data)
        except Exception as e:
            logger.debug(f"CSS selector method failed: {e}")
        
        # Method 3: Try alternative class combinations
        alt_divs = soup.find_all("div", class_=lambda x: x and ("probox" in x or "promotion" in x or "promo" in x))
        for div in alt_divs:
            imgs = div.find_all("img")
            for img in imgs:
                image_url = _extract_image_url(img, promo_url)
                if image_url and image_url.lower().strip() not in seen_urls:
                    # Verify it's likely a promo image (skip placeholders and non-image URLs)
                    alt_text = img.get("alt", "")
                    normalized_url = image_url.lower().strip()
                    
                    # Skip placeholders and non-image URLs
                    if any(skip in normalized_url for skip in ["placeholder", "blank", "1x1", "spacer", "logo", "icon"]):
                        continue
                    # Skip if URL is the page URL itself
                    if normalized_url == promo_url.lower() or normalized_url == promo_url.lower() + "/":
                        continue
                    # Must be an image file
                    if not any(ext in normalized_url for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"]):
                        # But allow if it's from wp-content (likely WordPress media)
                        if "wp-content" not in normalized_url and "uploads" not in normalized_url:
                            continue
                    
                    seen_urls.add(normalized_url)
                    images.append({
                        "image_url": image_url,
                        "alt_text": alt_text
                    })
        
        # Method 4: Also search for promo-related images anywhere on the page (Easy Pay, Protection Plan, etc.)
        # Look for images with promo-related keywords in filename
        all_page_images = soup.find_all("img")
        for img in all_page_images:
            image_url = _extract_image_url(img, promo_url)
            if image_url and image_url.lower().strip() not in seen_urls:
                normalized_url = image_url.lower().strip()
                
                # Skip invalid URLs
                if normalized_url == promo_url.lower() or normalized_url == promo_url.lower() + "/" or normalized_url.endswith("/promotions/"):
                    continue
                
                # Skip logos, icons, placeholders
                if any(skip in normalized_url for skip in ["logo", "icon", "placeholder", "blank", "1x1", "spacer"]):
                    continue
                
                # Check if it's a promo-related image by filename
                filename_lower = normalized_url.lower()
                is_promo_image = any(keyword in filename_lower for keyword in [
                    "rebate", "promo", "promotion", "deal", "offer", "easy", "pay", 
                    "protection", "plan", "financing", "special", "tire-rebate", "fall"
                ])
                
                # Or check if it's in wp-content/uploads (likely promotional)
                is_upload_image = "wp-content" in normalized_url or "uploads" in normalized_url
                
                if is_promo_image or (is_upload_image and any(ext in normalized_url for ext in [".png", ".jpg", ".jpeg"])):
                    seen_urls.add(normalized_url)
                    images.append({
                        "image_url": image_url,
                        "alt_text": img.get("alt", "")
                    })
                    logger.debug(f"Found additional promo image: {image_url.split('/')[-1]}")
        
        logger.info(f"Total {len(images)} unique banner images found")
        
        if not images:
            logger.warning(f"No images found on {promo_url}")
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
            
            # Step 4: Run OCR (Google Vision primary, Tesseract fallback)
            logger.info(f"Running OCR on {img_path.name}...")
            ocr_text = ocr_image(img_path)
            
            # Step 5: Skip if OCR text too short
            if not ocr_text or len(ocr_text.strip()) < 10:
                logger.warning(f"OCR text too short (< 10 chars) from {image_url}")
                # Try alt text as fallback before skipping
                if alt_text and len(alt_text.strip()) >= 10:
                    logger.info("Using alt text as fallback for short OCR")
                    ocr_text = alt_text
                else:
                    img_path.unlink()
                    continue
            
            # Step 6: Check if it's promo-related
            # Be more lenient for tire promotions - check for discount value or keywords
            is_promo = detect_promo_keywords(ocr_text, PROMO_KEYWORDS)
            extracted_details = extract_promo_details_from_text(ocr_text)
            
            # Also consider it a promo if we found discount value OR if it's clearly a promo image
            if not is_promo and not extracted_details.get("discount_value"):
                # Last check: if alt text or image URL contains promo keywords
                alt_lower = alt_text.lower()
                url_lower = image_url.lower()
                has_keyword = any(kw in alt_lower or kw in url_lower for kw in ["rebate", "off", "discount", "save", "promo", "tire", "easy pay", "protection", "plan", "financing"])
                
                # Skip generic headers like "ROLLING OUT THE REBATES" that don't have actual offers
                is_generic_header = any(phrase in ocr_text.lower() for phrase in ["rolling out", "the rebates", "promotions", "special offers"]) and not extracted_details.get("discount_value")
                
                if not has_keyword or (is_generic_header and not extracted_details.get("discount_value")):
                    logger.info(f"Image doesn't contain promo keywords or discount: {image_url}")
                    img_path.unlink()
                    continue
            
            # Step 7: Extract brand and basic details before deduplication
            extracted_details = extract_promo_details_from_text(ocr_text)
            
            # Step 8: Extract basic details from text
            extracted_details = extract_promo_details_from_text(ocr_text)
            
            # Step 9: Clean with LLM
            context = f"Trail Tire promotion banner. Alt text: {alt_text}"
            cleaned_data = clean_promo_text_with_llm(ocr_text, context)
            
            # Step 10: Extract brand name from OCR text
            brand_name = extract_brand_name(ocr_text) or extract_brand_name(alt_text)
            
            # Step 11: Build promotion title - include brand and amount for uniqueness
            if cleaned_data and cleaned_data.get("service_name"):
                base_title = cleaned_data.get("service_name")
                if brand_name and brand_name.lower() not in base_title.lower():
                    promotion_title = f"{brand_name} {base_title}" if brand_name else base_title
                else:
                    promotion_title = base_title
            elif cleaned_data and cleaned_data.get("promo_description"):
                first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                if brand_name and brand_name.lower() not in first_line.lower():
                    promotion_title = f"{brand_name} {first_line}" if first_line else (f"{brand_name} Tire Promotion" if brand_name else "Tire Promotion")
                else:
                    promotion_title = first_line if first_line else "Tire Promotion"
            elif alt_text and len(alt_text.strip()) > 5:
                promotion_title = alt_text[:100]
            else:
                lines = [l.strip() for l in ocr_text.split("\n") if l.strip() and len(l.strip()) > 5]
                base_title = lines[0][:100] if lines else "Tire Promotion"
                if brand_name and brand_name.lower() not in base_title.lower():
                    promotion_title = f"{brand_name} {base_title}"
                else:
                    promotion_title = base_title
            
            # Add discount to title for uniqueness if available (but avoid duplicate amounts in title)
            discount_value_temp = extracted_details.get("discount_value") or (cleaned_data.get("discount_value") if cleaned_data else None)
            if discount_value_temp:
                # Only add if not already in title
                discount_lower = discount_value_temp.lower()
                title_lower = promotion_title.lower()
                if discount_lower not in title_lower:
                    promotion_title = f"{promotion_title} - {discount_value_temp}"
                else:
                    # Remove duplicate amount from title if it appears twice
                    # Check if amount appears at end already
                    if promotion_title.endswith(f" - {discount_value_temp}"):
                        pass  # Already has it once at end, that's fine
                    elif f" - {discount_value_temp} - {discount_value_temp}" in promotion_title:
                        promotion_title = promotion_title.replace(f" - {discount_value_temp} - {discount_value_temp}", f" - {discount_value_temp}")
            
            # Create unique signature for deduplication: brand + amount + image hash
            brand = brand_name.lower() if brand_name else ""
            amount = (discount_value_temp or "").lower() if discount_value_temp else ""
            promo_signature = f"{brand}|{amount}|{str(img_hash)[:8] if img_hash else 'no-hash'}"
            
            # Skip if we've seen this exact signature before
            if promo_signature in seen_promo_signatures:
                logger.info(f"Skipping duplicate signature: {promo_signature}")
                img_path.unlink()
                continue
            seen_promo_signatures.add(promo_signature)
            
            # Also check if same brand and amount but different image (likely same promo)
            if brand and amount:
                brand_amount_sig = f"{brand}|{amount}"
                for existing in all_promos:
                    existing_brand = extract_brand_name(existing.get("ad_text", "") + " " + existing.get("promotion_title", "")).lower()
                    existing_amount = (existing.get("discount_value", "") or "").lower()
                    existing_sig = f"{existing_brand}|{existing_amount}"
                    
                    if brand_amount_sig == existing_sig and brand_amount_sig != "|":
                        # Check title similarity
                        existing_title = normalize_title(existing.get("promotion_title", ""))
                        current_title = normalize_title(promotion_title)
                        from fuzzywuzzy import fuzz
                        similarity = fuzz.ratio(existing_title, current_title)
                        if similarity > 90:
                            logger.info(f"Skipping similar promo: {promotion_title} (similarity: {similarity}%)")
                            img_path.unlink()
                            break
                else:
                    # No break = not a duplicate
                    pass
            
            # Step 12: Build structured promo dict
            if cleaned_data:
                discount_value = cleaned_data.get("discount_value") or extracted_details.get("discount_value")
                coupon_code = cleaned_data.get("coupon_code") or extracted_details.get("coupon_code")
                expiry_date = cleaned_data.get("expiry_date") or extracted_details.get("expiry_date")
                offer_details = cleaned_data.get("promo_description") or ocr_text[:1000]
            else:
                # Fallback: use basic extraction
                discount_value = extracted_details.get("discount_value")
                coupon_code = extracted_details.get("coupon_code")
                expiry_date = extracted_details.get("expiry_date")
                offer_details = ocr_text[:1000]
            
            # Calculate confidence score (based on OCR text length and LLM success)
            confidence_score = 0.7  # Default
            if cleaned_data:
                confidence_score = 0.9
            if len(ocr_text) > 200:
                confidence_score += 0.05
            confidence_score = min(confidence_score, 1.0)
            
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": brand_name or (cleaned_data.get("service_name", "tires") if cleaned_data else "tires"),
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
                "coupon_code": coupon_code,
                "expiry_date": expiry_date,
                "image_path": str(img_path),
                "extraction_method": "image_ocr",
                "source": "image_llm",
                "confidence_score": round(confidence_score, 2),
                "promotion_title": promotion_title,
                "brand_name": brand_name
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
    
    logger.info(f"Total unique promotions found: {len(all_promos)}")
    return all_promos


def scrape_trail(competitor: Dict) -> Dict:
    """Main entry point for Trail Tire scraper."""
    try:
        promos = process_trail_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'trail').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Trail Tire: {e}", exc_info=True)
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
    
    # Find Trail Tire
    trail = next((c for c in competitors if "trail" in c.get("name", "").lower()), None)
    
    if not trail:
        logger.error("Trail Tire Auto Centres not found in competitor list")
        sys.exit(1)
    
    result = scrape_trail(trail)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

