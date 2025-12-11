"""Kal Tire scraper - Dynamic content extraction using Playwright."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.extractors.ocr.ocr_processor import ocr_image, detect_promo_keywords
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.extractors.images.image_downloader import download_image, normalize_url
from app.config.constants import PROMO_KEYWORDS, DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "kal_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def extract_discount_value(text: str) -> Optional[str]:
    """Extract discount value from text."""
    text_lower = text.lower()
    
    # Try dollar amount first
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', text)
    if dollar_match:
        return f"${dollar_match.group(1)}"
    
    # Try percentage
    percent_match = re.search(r'(\d+)\s*%', text)
    if percent_match:
        return f"{percent_match.group(1)}%"
    
    # Try "free"
    if "free" in text_lower:
        return "free"
    
    return None


def extract_brand_name(text: str) -> Optional[str]:
    """Extract brand name from text."""
    tire_brands = [
        "michelin", "bridgestone", "goodyear", "continental", "pirelli",
        "bfgoodrich", "bfgoodrich", "toyo", "nitto", "hankook", "falken",
        "kumho", "yokohama", "dunlop", "firestone", "general", "cooper",
        "uniroyal", "nexen", "hercules", "mastercraft", "goodrich"
    ]
    
    text_lower = text.lower()
    for brand in tire_brands:
        if brand in text_lower:
            return brand.title()
    return None


def extract_expiry_date(text: str) -> Optional[str]:
    """Extract expiry date from text."""
    date_patterns = [
        r'(?:expires?|expiry|valid until|until|ends?)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, text, re.IGNORECASE)
        if date_match:
            return date_match.group(1).strip()
    return None


def calculate_title_overlap(title1: str, title2: str) -> float:
    """Calculate word overlap percentage between two titles."""
    if not title1 or not title2:
        return 0.0
    
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    
    if not union:
        return 0.0
    
    return (len(intersection) / len(union)) * 100


def are_promos_duplicate(promo1: Dict, promo2: Dict) -> bool:
    """
    Check if two promotions are duplicates based on:
    1. Exact brand + discount match
    2. Same brand with discount within $5
    3. Same discount with 70%+ title word overlap
    """
    brand1 = promo1.get("brand_name") or extract_brand_name(promo1.get("promotion_title", ""))
    brand2 = promo2.get("brand_name") or extract_brand_name(promo2.get("promotion_title", ""))
    
    discount1 = promo1.get("discount_value", "")
    discount2 = promo2.get("discount_value", "")
    
    title1 = promo1.get("promotion_title", "")
    title2 = promo2.get("promotion_title", "")
    
    # Rule 1: Exact brand + discount match
    if brand1 and brand2 and brand1.lower() == brand2.lower():
        if discount1 and discount2 and discount1 == discount2:
            return True
        
        # Rule 2: Same brand with discount within $5
        if discount1 and discount2:
            # Extract numeric values
            val1_match = re.search(r'(\d+(?:\.\d+)?)', discount1)
            val2_match = re.search(r'(\d+(?:\.\d+)?)', discount2)
            if val1_match and val2_match:
                val1 = float(val1_match.group(1))
                val2 = float(val2_match.group(1))
                if abs(val1 - val2) <= 5:
                    return True
    
    # Rule 3: Same discount with 70%+ title word overlap AND same/unknown brand
    # Only consider duplicate if both have same discount AND high title overlap AND same brand
    if discount1 and discount2 and discount1 == discount2:
        overlap = calculate_title_overlap(title1, title2)
        # Only mark as duplicate if high overlap (85%+) AND brands match (or both unknown)
        brand_match = (brand1 and brand2 and brand1.lower() == brand2.lower()) or (not brand1 and not brand2)
        if overlap >= 85 and brand_match:
            return True
    
    return False


def keep_better_promo(promo1: Dict, promo2: Dict) -> Dict:
    """Keep the promotion with more complete information."""
    # Count non-empty fields
    fields1 = sum(1 for k, v in promo1.items() if v and k not in ["date_scraped", "ocr_hash"])
    fields2 = sum(1 for k, v in promo2.items() if v and k not in ["date_scraped", "ocr_hash"])
    
    # Prefer longer offer_details
    details1_len = len(promo1.get("offer_details", ""))
    details2_len = len(promo2.get("offer_details", ""))
    
    if fields1 > fields2 or (fields1 == fields2 and details1_len > details2_len):
        return promo1
    return promo2


def extract_card_content(card_element) -> Dict:
    """Extract content from a promo card element."""
    try:
        # Extract HTML text
        card_html = card_element.inner_html()
        card_text = card_element.inner_text()
        
        # Find images in the card
        images = card_element.query_selector_all("img")
        image_urls = []
        for img in images:
            src = img.get_attribute("src") or img.get_attribute("data-src")
            if src:
                image_urls.append(src)
        
        return {
            "html": card_html,
            "text": card_text,
            "image_urls": image_urls
        }
    except Exception as e:
        logger.error(f"Error extracting card content: {e}")
        return {"html": "", "text": "", "image_urls": []}


def process_kal_promotions(competitor: Dict) -> List[Dict]:
    """Process Kal Tire promotions using Playwright for dynamic content."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    from playwright.sync_api import sync_playwright
    
    all_promos = []
    
    try:
        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()
            
            promo_url = promo_links[0]
            logger.info(f"Loading page: {promo_url}")
            page.goto(promo_url, wait_until="networkidle", timeout=60000)
            
            # Wait for page to fully load
            page.wait_for_timeout(2000)
            
            # First, check if there are tabs - if so, click them
            # Tabs are typically in a tab list or navigation
            tabs_to_click = ["Tires", "Wheels", "Services"]
            
            # Check if tabs exist on the page
            has_tabs = False
            try:
                # Look for tab elements
                tab_elements = page.query_selector_all("[role='tab'], .tab, [class*='tab']")
                if len(tab_elements) > 0:
                    has_tabs = True
                    logger.info(f"Found {len(tab_elements)} tab elements")
            except:
                pass
            
            # Helper function to extract cards from current page state
            def extract_cards_from_current_view():
                card_selector = "div.rebates-item"
                cards = page.query_selector_all(card_selector)
                
                # If no cards found, try alternative selectors
                if len(cards) == 0:
                    alt_selectors = [
                        "div.kal-service-special-card",
                        "div[class*='rebate']",
                        "div[class*='promo']",
                        "div[class*='card']",
                        "div[class*='special']",
                    ]
                    for alt_sel in alt_selectors:
                        cards = page.query_selector_all(alt_sel)
                        if len(cards) > 0:
                            logger.info(f"Found cards using alternative selector: {alt_sel}")
                            card_selector = alt_sel
                            break
                return cards, card_selector
            
            # Find tab buttons within the rebates section - use multiple strategies
            tabs_wrapper = None
            
            # Strategy 1: Look for rebates-tabs-nav-wrapper
            tabs_wrapper = page.query_selector(".rebates-tabs-nav-wrapper")
            
            # Strategy 2: Look for any tab navigation element
            if not tabs_wrapper:
                tabs_wrapper = page.query_selector("[class*='tab'], [role='tablist'], .tabs, .nav-tabs")
            
            # Strategy 3: Look for tab-like elements in the rebates section
            if not tabs_wrapper:
                rebates_section = page.query_selector("[class*='rebate'], .rebates, #rebates")
                if rebates_section:
                    tabs_wrapper = rebates_section.query_selector("[class*='tab'], [role='tablist']")
            
            tab_names_to_click = ["Tires", "Wheels", "Services", "Tire", "Wheel", "Service"]
            tabs_to_process = []  # Will collect tabs to process
            
            if tabs_wrapper:
                logger.info("Found tabs navigation, will click through each tab")
                # Try multiple selectors for tab buttons
                tab_selector_variants = [
                    "a.content-custom-btn",
                    "button",
                    "[role='button']",
                    "[role='tab']",
                    "a[class*='tab']",
                    "button[class*='tab']",
                    ".tab-button",
                    "a",
                    "button"
                ]
                
                tab_buttons = []
                for selector in tab_selector_variants:
                    try:
                        found_buttons = tabs_wrapper.query_selector_all(selector)
                        if found_buttons:
                            tab_buttons = found_buttons
                            logger.info(f"Found {len(tab_buttons)} tab buttons using selector: {selector}")
                            break
                    except:
                        continue
                
                # If tabs_wrapper exists but no buttons found, try querying the whole page
                if not tab_buttons:
                    for selector in tab_selector_variants[:4]:  # Try first few selectors on whole page
                        try:
                            found_buttons = page.query_selector_all(selector)
                            # Filter buttons that contain tab names
                            for btn in found_buttons:
                                btn_text = btn.inner_text().strip()
                                if any(tab_name.lower() in btn_text.lower() for tab_name in ["Tires", "Wheels", "Services"]):
                                    if btn not in tab_buttons:
                                        tab_buttons.append(btn)
                            if tab_buttons:
                                logger.info(f"Found {len(tab_buttons)} tab buttons from page search")
                                break
                        except:
                            continue
                
                for tab_btn in tab_buttons:
                    try:
                        tab_text = tab_btn.inner_text().strip()
                        # Normalize tab names (handle variations)
                        tab_text_lower = tab_text.lower()
                        if any(keyword in tab_text_lower for keyword in ["tire", "wheel", "service"]):
                            if tab_text not in tabs_to_process:
                                tabs_to_process.append(tab_text)
                    except:
                        continue
                
                logger.info(f"Found {len(tabs_to_process)} tabs to process: {tabs_to_process}")
                
                # If no tabs found but tabs_wrapper exists, try default view
                if not tabs_to_process:
                    tabs_to_process = ["default"]
                    logger.info("No tab buttons found, using default view")
            else:
                logger.info("No tabs navigation found, extracting from default view only")
                tabs_to_process = ["default"]
            
            # Process each tab and extract cards immediately (before DOM becomes stale)
            for tab_label in tabs_to_process:
                if tab_label != "default":
                    logger.info(f"Clicking tab: {tab_label}")
                    clicked_successfully = False
                    
                    # Try multiple methods to click the tab
                    click_methods = [
                        # Method 1: Direct text match in tabs_wrapper
                        lambda: page.query_selector(f".rebates-tabs-nav-wrapper a:has-text('{tab_label}')") or 
                                page.query_selector(f".rebates-tabs-nav-wrapper button:has-text('{tab_label}')"),
                        # Method 2: Case-insensitive text match
                        lambda: next((btn for btn in page.query_selector_all(".rebates-tabs-nav-wrapper a, .rebates-tabs-nav-wrapper button") 
                                     if tab_label.lower() in btn.inner_text().strip().lower()), None),
                        # Method 3: Look for tab with role
                        lambda: page.query_selector(f"[role='tab']:has-text('{tab_label}')"),
                        # Method 4: Search whole page for tab-like elements
                        lambda: next((btn for btn in page.query_selector_all("a, button, [role='button'], [role='tab']")
                                     if tab_label.lower() in btn.inner_text().strip().lower() and 
                                     any(keyword in btn.inner_text().strip().lower() for keyword in ["tire", "wheel", "service"])), None)
                    ]
                    
                    for method_idx, get_tab_element in enumerate(click_methods, 1):
                        try:
                            tab_element = get_tab_element()
                            if tab_element:
                                # Try multiple click methods
                                try:
                                    # Method 1: JavaScript click
                                    page.evaluate("element => element.click()", tab_element)
                                    page.wait_for_timeout(3000)  # Wait for content to load
                                    clicked_successfully = True
                                    logger.info(f"Successfully clicked tab '{tab_label}' using method {method_idx}")
                                    break
                                except:
                                    try:
                                        # Method 2: Regular click
                                        tab_element.click()
                                        page.wait_for_timeout(3000)
                                        clicked_successfully = True
                                        logger.info(f"Successfully clicked tab '{tab_label}' using regular click")
                                        break
                                    except:
                                        continue
                        except Exception as e:
                            if method_idx == len(click_methods):
                                logger.warning(f"All methods failed to click tab '{tab_label}': {e}")
                            continue
                    
                    if not clicked_successfully:
                        logger.warning(f"Could not click tab '{tab_label}', attempting to extract from current view")
                
                # Extract cards from current view IMMEDIATELY (while DOM is valid)
                cards, card_selector = extract_cards_from_current_view()
                logger.info(f"Found {len(cards)} promo cards in {tab_label} tab")
                
                # Process cards IMMEDIATELY while they're still valid in DOM
                for i, card in enumerate(cards):
                    logger.info(f"Processing card {i+1}/{len(cards)} from {tab_label}")
                    
                    # Extract card content
                    card_data = extract_card_content(card)
                    card_text = card_data["text"]
                    card_html = card_data["html"]
                    image_urls = card_data["image_urls"]
                    
                    if not card_text or len(card_text.strip()) < 10:
                        # Try OCR on images if text is missing
                        logger.info(f"Card text missing, trying OCR on images")
                        ocr_text_parts = []
                        
                        for img_url in image_urls[:1]:  # Use first image only
                            try:
                                # Download and OCR
                                img_path = download_image(img_url)
                                if img_path:
                                    ocr_text = ocr_image(img_path)
                                    if ocr_text:
                                        ocr_text_parts.append(ocr_text)
                                    img_path.unlink()
                            except Exception as e:
                                logger.warning(f"OCR error for {img_url}: {e}")
                        
                        if ocr_text_parts:
                            card_text = "\n".join(ocr_text_parts)
                        else:
                            logger.warning(f"No text or OCR content from card, skipping")
                            continue
                    
                    # Skip if text is too short or doesn't contain promo keywords
                    if not detect_promo_keywords(card_text, PROMO_KEYWORDS):
                        # Check if it has discount value or brand
                        if not extract_discount_value(card_text) and not extract_brand_name(card_text):
                            logger.info(f"Card doesn't contain promo keywords or relevant data, skipping")
                            continue
                    
                    # Extract basic details
                    discount_value = extract_discount_value(card_text)
                    brand_name = extract_brand_name(card_text)
                    expiry_date = extract_expiry_date(card_text)
                    
                    # Clean with LLM
                    context_text = f"Kal Tire promotion card from {tab_label} tab. HTML: {card_html[:500]}"
                    cleaned_data = clean_promo_text_with_llm(card_text, context_text)
                    
                    # Build promotion title
                    if cleaned_data and cleaned_data.get("service_name"):
                        promotion_title = cleaned_data.get("service_name")
                    elif cleaned_data and cleaned_data.get("promo_description"):
                        first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                        promotion_title = first_line if first_line else card_text.split("\n")[0][:100]
                    else:
                        # Extract first meaningful line
                        lines = [l.strip() for l in card_text.split("\n") if l.strip() and len(l.strip()) > 5]
                        promotion_title = lines[0][:100] if lines else "Tire Promotion"
                    
                    # Use LLM cleaned data if available
                    if cleaned_data:
                        offer_details = cleaned_data.get("promo_description") or card_text[:1000]
                        discount_value = cleaned_data.get("discount_value") or discount_value
                        expiry_date = cleaned_data.get("expiry_date") or expiry_date
                    else:
                        offer_details = card_text[:1000]
                    
                    promo = {
                        "website": competitor.get("domain", ""),
                        "page_url": promo_url,
                        "business_name": competitor.get("name", ""),
                        "google_reviews": None,
                        "service_name": cleaned_data.get("service_name", "tires") if cleaned_data else "tires",
                        "promo_description": offer_details,
                        "category": "tires",
                        "contact": competitor.get("address", ""),
                        "location": competitor.get("address", ""),
                        "offer_details": offer_details,
                        "ad_title": promotion_title,
                        "ad_text": card_text[:200],
                        "new_or_updated": "new",
                        "date_scraped": datetime.now().isoformat(),
                        "discount_value": discount_value,
                        "coupon_code": cleaned_data.get("coupon_code") if cleaned_data else None,
                        "expiry_date": expiry_date,
                        "promotion_title": promotion_title,
                        "brand_name": brand_name,
                        "tab_source": tab_label
                    }
                    
                    all_promos.append(promo)
                    logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
            
            browser.close()
    
    except Exception as e:
        logger.error(f"Error with Playwright: {e}", exc_info=True)
    
    # Deduplicate promotions
    logger.info(f"Found {len(all_promos)} promotions before deduplication")
    deduplicated_promos = []
    
    for promo in all_promos:
        is_duplicate = False
        
        for existing_promo in deduplicated_promos:
            if are_promos_duplicate(promo, existing_promo):
                # Keep the better one
                better_promo = keep_better_promo(promo, existing_promo)
                
                # Remove the worse one and add the better one
                if better_promo == promo:
                    deduplicated_promos.remove(existing_promo)
                    deduplicated_promos.append(promo)
                is_duplicate = True
                logger.info(f"Removed duplicate: {promo.get('promotion_title')} vs {existing_promo.get('promotion_title')}")
                break
        
        if not is_duplicate:
            deduplicated_promos.append(promo)
    
    logger.info(f"Total unique promotions found: {len(deduplicated_promos)}")
    return deduplicated_promos


def scrape_kal(competitor: Dict) -> Dict:
    """Main entry point for Kal Tire scraper."""
    try:
        promos = process_kal_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'kal').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Kal Tire: {e}", exc_info=True)
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
    
    # Find Kal Tire
    kal = next((c for c in competitors if "kal" in c.get("name", "").lower()), None)
    
    if not kal:
        logger.error("Kal Tire not found in competitor list")
        sys.exit(1)
    
    result = scrape_kal(kal)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

