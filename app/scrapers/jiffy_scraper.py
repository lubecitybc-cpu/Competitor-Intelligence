"""Jiffy Lube scraper - Text-based HTML extraction."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "jiffy_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_with_fallback(url: str) -> str:
    """Fetch HTML using Firecrawl, fallback to ZenRows/ScraperAPI."""
    # Try Firecrawl first
    firecrawl_result = fetch_with_firecrawl(url, timeout=90)
    
    if firecrawl_result.get("html") and not firecrawl_result.get("error"):
        logger.info("Successfully fetched with Firecrawl")
        return firecrawl_result.get("html", "")
    
    logger.warning("Firecrawl failed, trying fallback methods...")
    
    # Fallback to ZenRows
    try:
        from app.config.constants import ZENROWS_API_KEY
        if ZENROWS_API_KEY:
            import requests
            zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={url}"
            response = requests.get(zenrows_url, timeout=30)
            response.raise_for_status()
            logger.info("Successfully fetched with ZenRows")
            return response.text
    except Exception as e:
        logger.warning(f"ZenRows fallback failed: {e}")
    
    # Fallback to ScraperAPI
    try:
        from app.config.constants import SCRAPERAPI_KEY
        if SCRAPERAPI_KEY:
            import requests
            scraperapi_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={url}"
            response = requests.get(scraperapi_url, timeout=30)
            response.raise_for_status()
            logger.info("Successfully fetched with ScraperAPI")
            return response.text
    except Exception as e:
        logger.warning(f"ScraperAPI fallback failed: {e}")
    
    logger.error("All fetch methods failed")
    return ""


def normalize_title(title: str) -> str:
    """Normalize title by removing generic phrases."""
    if not title:
        return ""
    
    # Convert to lowercase for processing
    normalized = title.lower().strip()
    
    # Remove generic phrases
    generic_phrases = [
        "get", "coupon", "off a", "expires", "barcode", "valid", "offer",
        "save", "now", "limited", "time", "only", "click", "here", "see",
        "more", "details", "terms", "apply", "conditions"
    ]
    
    # Remove generic phrases (whole words only)
    words = normalized.split()
    filtered_words = []
    for word in words:
        # Clean punctuation
        clean_word = re.sub(r'[^\w\s]', '', word)
        if clean_word not in generic_phrases:
            filtered_words.append(word)
    
    normalized = " ".join(filtered_words)
    
    # Remove extra whitespace
    normalized = " ".join(normalized.split())
    
    return normalized


def extract_promo_sections(html: str) -> List[Dict]:
    """Extract promotional sections from HTML - focus on actual coupon offers."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    promo_sections = []
    seen_texts = set()
    
    # Method 1: Find individual coupons by locating "GET COUPON" buttons/links and extracting their parent containers
    # This is the most reliable way to find individual coupon cards
    # Try multiple patterns to find all coupon buttons
    coupon_buttons = []
    # Pattern 1: Exact text match
    coupon_buttons.extend(soup.find_all(["a", "button"], string=re.compile(r"GET\s+COUPON", re.IGNORECASE)))
    coupon_buttons.extend(soup.find_all(["a", "button"], string=re.compile(r"GET\s+IT", re.IGNORECASE)))
    # Pattern 2: Find by text content (more flexible)
    all_links_buttons = soup.find_all(["a", "button"])
    for elem in all_links_buttons:
        text = elem.get_text(strip=True).upper()
        if "GET" in text and ("COUPON" in text or len(text) < 15):  # Short text usually means "GET COUPON"
            if elem not in coupon_buttons:
                coupon_buttons.append(elem)
    
    logger.info(f"Found {len(coupon_buttons)} 'GET COUPON' buttons/links")
    
    for button in coupon_buttons:
        # Find the parent div that contains this coupon
        # Look up the DOM tree to find a coupon container
        parent = button.parent
        max_levels = 5
        level = 0
        
        while parent and level < max_levels:
            # Check if this parent looks like a coupon container
            parent_classes = ' '.join(parent.get('class', [])).lower()
            parent_text = parent.get_text(strip=True)
            
            # Check if this parent contains a complete coupon (has discount/code/expiry)
            has_discount = bool(re.search(r'\$(\d+)|(\d+)\s*%|free|bonus', parent_text, re.IGNORECASE))
            has_code = bool(re.search(r'[A-Z0-9]{4,}', parent_text))  # Look for coupon codes
            has_expiry = bool(re.search(r'(?:expires?|valid|until)[:\s]+', parent_text, re.IGNORECASE))
            has_service = bool(re.search(r'(?:oil change|synthetic|pennzoil|service|miles)', parent_text, re.IGNORECASE))
            
            # If this looks like a complete coupon card, extract it
            # Must have actual offer (discount/code/bonus) AND service
            has_bonus = bool(re.search(r'(?:bonus|miles|rewards?|points)', parent_text, re.IGNORECASE))
            
            # Skip terms/conditions
            text_start = parent_text.strip()[:150].lower()
            is_terms_only = any(phrase in text_start for phrase in [
                "cannot be combined", "terms and conditions", "one coupon per visit",
                "valid at participating"
            ]) or parent_text.strip().startswith(("*cannot", "*terms", "cannot", "terms"))
            
            if (is_terms_only and not (has_discount or has_code or has_bonus)):
                parent = parent.parent
                level += 1
                continue
            
            if ((has_discount or has_code or has_bonus) and has_service):
                if len(parent_text) > 30:
                    text_normalized = " ".join(parent_text.lower().split())
                    
                    # Skip terms/conditions (but not if it's just part of the coupon text)
                    text_start = text_normalized[:200]
                    if any(skip in text_start for skip in ["cannot be combined", "terms and conditions", "browser does not support", "your browser does not support"]):
                        # Only skip if it's at the very start
                        if text_normalized.startswith(("cannot be combined", "terms", "browser", "your browser")):
                            parent = parent.parent
                            level += 1
                            continue
                    
                    # Create a unique signature for this coupon based on key details
                    discount_match = re.search(r'(\$\d+(?:\.\d+)?)', parent_text)
                    code_match = re.search(r'([A-Z0-9]{4,})', parent_text)
                    expiry_match = re.search(r'(?:expires?|exp\.?)[:\s]+([^\n]{5,30})', parent_text, re.IGNORECASE)
                    
                    signature_parts = []
                    if discount_match:
                        signature_parts.append(f"discount:{discount_match.group(1)}")
                    if code_match:
                        signature_parts.append(f"code:{code_match.group(1)}")
                    if expiry_match:
                        signature_parts.append(f"expiry:{expiry_match.group(1)[:20]}")
                    
                    signature = "|".join(signature_parts) if signature_parts else text_normalized[:100]
                    
                    if signature not in seen_texts and text_normalized not in seen_texts:
                        seen_texts.add(text_normalized)
                        seen_texts.add(signature)
                        promo_sections.append({
                            "html": str(parent),
                            "text": parent_text,
                            "selector": "get-coupon-parent"
                        })
                        logger.debug(f"Extracted coupon from GET COUPON button parent: {parent_text[:80]}...")
                        break
            
            parent = parent.parent
            level += 1
    
    # Method 2: Look for coupon-grid-wrapper and extract ALL individual coupon items
    # This is important because some coupons might not have GET COUPON buttons
    logger.info("Scanning coupon-grid for all individual coupon items...")
    coupon_grid = soup.find("div", class_=lambda x: x and "coupon-grid" in str(x).lower())
    
    if coupon_grid:
        # Find all wrapper divs that contain individual coupons
        # Look for divs with coupon-wrapper class that are children or descendants
        individual_coupons = coupon_grid.find_all("div", class_=lambda x: x and "coupon-wrapper" in str(x).lower())
        logger.info(f"Found {len(individual_coupons)} coupon-wrapper divs in coupon-grid")
        
        for coupon_wrapper in individual_coupons:
            text = coupon_wrapper.get_text(strip=True)
            if text and len(text) > 30:
                has_discount = bool(re.search(r'\$(\d+)|(\d+)\s*%|free|bonus', text, re.IGNORECASE))
                has_code = bool(re.search(r'[A-Z0-9]{4,}', text))
                has_expiry = bool(re.search(r'(?:expires?|valid|until)', text, re.IGNORECASE))
                has_service = bool(re.search(r'(?:oil change|synthetic|pennzoil|service|miles)', text, re.IGNORECASE))
                
                # Accept if it looks like a coupon
                # Must have: (discount OR code OR bonus/rewards) AND service
                # OR: service AND expiry AND (discount OR code) - but not just terms/conditions
                has_bonus = bool(re.search(r'(?:bonus|miles|rewards?|points)', text, re.IGNORECASE))
                
                # Skip if it's clearly just terms/conditions without an actual offer
                text_start = text.strip()[:150].lower()
                is_terms_only = any(phrase in text_start for phrase in [
                    "cannot be combined", "terms and conditions", "one coupon per visit",
                    "valid at participating", "fine print", "restrictions may apply"
                ])
                
                # Check if it starts with terms/conditions text
                starts_with_terms = text.strip().lower().startswith((
                    "cannot be combined", "terms", "one coupon", "valid at",
                    "*cannot be combined", "*terms"
                ))
                
                # If it's terms-only and has no actual discount/code/bonus, skip it
                if (is_terms_only or starts_with_terms) and not (has_discount or has_code or has_bonus):
                    logger.debug(f"Skipping terms/conditions section: {text[:80]}...")
                    continue
                
                # Accept if it has actual offer details (must have discount/code/bonus AND service)
                if ((has_discount or has_code or has_bonus) and has_service):
                    # Create signature to avoid exact duplicates
                    discount_match = re.search(r'(\$\d+(?:\.\d+)?)', text)
                    code_match = re.search(r'([A-Z0-9]{4,})', text)
                    expiry_match = re.search(r'(?:expires?|exp\.?)[:\s]+([^\n]{5,30})', text, re.IGNORECASE)
                    
                    signature_parts = []
                    if discount_match:
                        signature_parts.append(f"discount:{discount_match.group(1)}")
                    if code_match:
                        signature_parts.append(f"code:{code_match.group(1)}")
                    if expiry_match:
                        signature_parts.append(f"expiry:{expiry_match.group(1)[:20]}")
                    
                    signature = "|".join(signature_parts) if signature_parts else None
                    text_normalized = " ".join(text.lower().split())
                    
                    # Skip if we've already seen this exact coupon
                    if signature and signature in seen_texts:
                        continue
                    if text_normalized in seen_texts:
                        continue
                    
                    if signature:
                        seen_texts.add(signature)
                    seen_texts.add(text_normalized)
                    promo_sections.append({
                        "html": str(coupon_wrapper),
                        "text": text,
                        "selector": "coupon-grid-item"
                    })
                    logger.debug(f"Extracted coupon from grid: {text[:80]}...")
        
        # Also try finding individual coupon-wrapper divs that are siblings or children of wrapper-master
        wrapper_master = soup.find("div", class_=lambda x: x and "coupon-wrapper-master" in str(x).lower())
        if wrapper_master:
            # Find individual coupon wrappers within or near the master
            individual_wrappers = wrapper_master.find_all("div", class_=lambda x: x and "coupon-wrapper" in str(x).lower())
            
            for wrapper in individual_wrappers:
                text = wrapper.get_text(strip=True)
                if text and len(text) > 30:
                    # Check if this is a complete coupon (not just a container)
                    has_discount = bool(re.search(r'\$(\d+)|(\d+)\s*%|free|bonus', text, re.IGNORECASE))
                    has_code = bool(re.search(r'[A-Z0-9]{4,}', text))
                    has_expiry = bool(re.search(r'(?:expires?|valid|until)', text, re.IGNORECASE))
                    has_service = bool(re.search(r'(?:oil change|synthetic|pennzoil|service)', text, re.IGNORECASE))
                    
                    if ((has_discount or has_code) and has_service) or (has_expiry and has_service):
                        text_normalized = " ".join(text.lower().split())
                        if text_normalized not in seen_texts:
                            seen_texts.add(text_normalized)
                            promo_sections.append({
                                "html": str(wrapper),
                                "text": text,
                                "selector": "individual-coupon-wrapper"
                            })
    
    # Method 3: Fallback - if still not enough, try to split by distinct coupon patterns
    if len(promo_sections) < 2:
        logger.info("Still not enough coupons, trying to extract from coupon-grid-wrapper as a whole...")
        coupon_grid = soup.find("div", class_=lambda x: x and "coupon-grid" in str(x).lower())
        if coupon_grid:
            full_text = coupon_grid.get_text(strip=True)
            # Try to split by common patterns that indicate separate coupons
            # Split by "GET COUPON", "Code:", "Expires:", or multiple consecutive newlines
            splits = re.split(r'(?:GET\s+COUPON|Code:\s*[A-Z0-9]+|Expires?:)', full_text)
            
            for i, split_text in enumerate(splits):
                if len(split_text.strip()) > 50:
                    has_discount = bool(re.search(r'\$(\d+)|(\d+)\s*%|free|bonus', split_text, re.IGNORECASE))
                    has_service = bool(re.search(r'(?:oil change|synthetic|pennzoil|service)', split_text, re.IGNORECASE))
                    
                    if has_discount or has_service:
                        text_normalized = " ".join(split_text.lower().split())
                        if text_normalized not in seen_texts and len(text_normalized) > 30:
                            seen_texts.add(text_normalized)
                            promo_sections.append({
                                "html": full_text[:1000],  # Use original full HTML
                                "text": split_text.strip(),
                                "selector": "split-coupon-grid"
                            })
    
    logger.info(f"Extracted {len(promo_sections)} promo sections from HTML")
    return promo_sections


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


def extract_coupon_code(text: str) -> Optional[str]:
    """Extract coupon code from text."""
    # Look for patterns like "CODE: ABC123", "Use code XYZ", "Promo code: ABC"
    code_patterns = [
        r'(?:code|coupon|promo)[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
        r'code[:\s]*([A-Z0-9]{3,20})',
    ]
    
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    
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


def are_promos_similar(promo1: Dict, promo2: Dict) -> bool:
    """Check if two promotions are similar for merging - be more conservative."""
    discount1 = promo1.get("discount_value")
    discount2 = promo2.get("discount_value")
    code1 = promo1.get("coupon_code")
    code2 = promo2.get("coupon_code")
    expiry1 = promo1.get("expiry_date")
    expiry2 = promo2.get("expiry_date")
    
    # Use stored normalized_title if available, otherwise normalize promotion_title
    title1 = promo1.get("normalized_title") or normalize_title(promo1.get("promotion_title", ""))
    title2 = promo2.get("normalized_title") or normalize_title(promo2.get("promotion_title", ""))
    
    # If they have different coupon codes, they are DIFFERENT coupons - don't merge
    if code1 and code2 and code1 != code2:
        return False
    
    # If they have different discounts AND different codes, they are DIFFERENT coupons
    if discount1 != discount2 and (not code1 or not code2 or code1 != code2):
        # Allow merge only if they're clearly the same offer with slight variation
        if discount1 and discount2:
            # Check if discounts are very similar (e.g., $10 vs $10.00)
            val1_match = re.search(r'(\d+(?:\.\d+)?)', str(discount1))
            val2_match = re.search(r'(\d+(?:\.\d+)?)', str(discount2))
            if val1_match and val2_match:
                val1 = float(val1_match.group(1))
                val2 = float(val2_match.group(1))
                if abs(val1 - val2) > 0.01:  # More than 1 cent difference
                    return False  # Different discounts = different coupons
    
    # Same normalized title exactly AND same code/discount - merge them
    if title1 == title2 and title1:
        if (code1 == code2) or (discount1 == discount2):
            return True
    
    # Calculate overlap
    overlap = calculate_title_overlap(title1, title2)
    
    # Very high overlap (90%+) with same code AND discount - likely same promotion
    if overlap >= 90:
        if (code1 and code2 and code1 == code2) and (discount1 == discount2):
            return True
    
    # Same code and same discount but different titles - likely same promotion (title variation)
    if (code1 and code2 and code1 == code2) and (discount1 == discount2):
        if overlap >= 50:  # Lower threshold since code/discount match
            return True
    
    return False


def merge_promos(promo1: Dict, promo2: Dict) -> Dict:
    """Merge two similar promotions, keeping the best information."""
    # Start with promo1 as base
    merged = promo1.copy()
    
    # Merge discount values (prefer the higher one if both are dollar amounts)
    discount1 = promo1.get("discount_value")
    discount2 = promo2.get("discount_value")
    
    if discount1 and discount2:
        # If both are dollar amounts, keep the higher one
        val1_match = re.search(r'(\d+(?:\.\d+)?)', discount1)
        val2_match = re.search(r'(\d+(?:\.\d+)?)', discount2)
        if val1_match and val2_match:
            val1 = float(val1_match.group(1))
            val2 = float(val2_match.group(1))
            merged["discount_value"] = discount1 if val1 >= val2 else discount2
        else:
            # Otherwise keep the first one
            merged["discount_value"] = discount1
    elif discount2 and not discount1:
        merged["discount_value"] = discount2
    
    # Merge offer details (keep the longer/more complete one)
    details1 = promo1.get("offer_details", "")
    details2 = promo2.get("offer_details", "")
    merged["offer_details"] = details1 if len(details1) >= len(details2) else details2
    
    # Merge other fields (prefer non-empty values)
    for key in ["coupon_code", "expiry_date", "promotion_title"]:
        if not merged.get(key) and promo2.get(key):
            merged[key] = promo2[key]
    
    return merged


def process_jiffy_promotions(competitor: Dict) -> List[Dict]:
    """Process Jiffy Lube promotions using text-based HTML extraction."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Step 1: Fetch HTML with fallback
        html = fetch_with_fallback(promo_url)
        
        if not html:
            logger.error(f"Failed to fetch HTML from {promo_url}")
            continue
        
        # Step 2: Extract promo sections from HTML
        promo_sections = extract_promo_sections(html)
        
        if not promo_sections:
            logger.warning(f"No promo sections found in HTML")
            # Try extracting from entire page
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            body_text = soup.get_text(strip=True)
            if body_text and len(body_text) > 100:
                promo_sections = [{
                    "html": html,
                    "text": body_text,
                    "selector": "full_page"
                }]
        
        # Step 3: Process each promo section
        for section in promo_sections:
            section_text = section["text"]
            section_html = section["html"]
            
            # Extract basic details
            discount_value = extract_discount_value(section_text)
            coupon_code = extract_coupon_code(section_text)
            
            # Clean with LLM
            context = f"Jiffy Lube coupon/promotion. HTML: {section_html[:1000]}"
            cleaned_data = clean_promo_text_with_llm(section_text, context)
            
            # Build promotion title
            if cleaned_data and cleaned_data.get("service_name"):
                promotion_title = cleaned_data.get("service_name")
            elif cleaned_data and cleaned_data.get("promo_description"):
                first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                promotion_title = first_line if first_line else section_text.split("\n")[0][:100]
            else:
                # Extract first meaningful line
                lines = [l.strip() for l in section_text.split("\n") if l.strip() and len(l.strip()) > 10]
                promotion_title = lines[0][:100] if lines else "Jiffy Lube Promotion"
            
            # Use LLM cleaned data if available
            if cleaned_data:
                offer_details = cleaned_data.get("promo_description") or section_text[:1000]
                discount_value = cleaned_data.get("discount_value") or discount_value
                coupon_code = cleaned_data.get("coupon_code") or coupon_code
                expiry_date = cleaned_data.get("expiry_date")
            else:
                offer_details = section_text[:1000]
                expiry_date = None
            
            # Skip if this is clearly just terms/conditions (no actual offer)
            # Check title, offer_details, and section_text for terms-only content
            promotion_title_lower = promotion_title.lower().strip()
            offer_details_lower = offer_details.lower()[:200] if offer_details else ""
            section_text_lower = section_text.lower()[:200] if section_text else ""
            
            # Remove leading * or other punctuation for checking
            title_clean = promotion_title_lower.lstrip('*').strip()
            
            is_terms_only = (
                title_clean.startswith(("cannot be combined", "terms", "one coupon")) or
                promotion_title_lower.startswith(("*cannot", "*terms")) or
                any(phrase in promotion_title_lower for phrase in ["one coupon per visit", "valid at participating", "cannot be combined"]) or
                (offer_details_lower.startswith("cannot be combined") and not discount_value and not coupon_code and not has_bonus) or
                (section_text_lower.startswith("cannot be combined") and not discount_value and not coupon_code and not has_bonus)
            )
            
            # Also check if title is mostly just terms/conditions phrases
            terms_phrases = ["cannot be combined", "one coupon per visit", "valid at participating", "terms"]
            title_word_count = len(promotion_title.split())
            terms_in_title = sum(1 for phrase in terms_phrases if phrase in promotion_title_lower)
            
            # If title is short and mostly terms phrases, and no discount/code, skip it
            if title_word_count <= 10 and terms_in_title >= 2 and not discount_value and not coupon_code:
                is_terms_only = True
            
            if is_terms_only and not discount_value and not coupon_code:
                logger.info(f"Skipping terms/conditions section: {promotion_title[:60]}...")
                continue
            
            # Skip promotions that have no discount, no code, and no bonus (likely not a valid coupon)
            has_bonus_in_text = bool(re.search(r'(?:bonus|miles|rewards?|points)', section_text, re.IGNORECASE)) if section_text else False
            has_bonus_in_offer = bool(re.search(r'(?:bonus|miles|rewards?|points)', offer_details, re.IGNORECASE)) if offer_details else False
            has_any_bonus = has_bonus_in_text or has_bonus_in_offer or bool(discount_value and "bonus" in str(discount_value).lower())
            
            if not discount_value and not coupon_code and not has_any_bonus:
                # Allow if it has a service and expiry (might be a valid offer)
                has_service_check = bool(re.search(r'(?:oil change|synthetic|pennzoil|service)', section_text or "", re.IGNORECASE))
                if not (has_service_check and expiry_date):
                    logger.info(f"Skipping promotion with no discount/code/bonus: {promotion_title[:60]}...")
                    continue
            
            # Skip invalid coupon codes (like "PER" from "per visit")
            if coupon_code and len(coupon_code) <= 3:
                # Check if it's a valid code (not just a word fragment)
                invalid_codes = ["PER", "VISIT", "THE", "AND", "FOR", "OFF", "GET"]
                if coupon_code.upper() in invalid_codes:
                    coupon_code = None
            
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": cleaned_data.get("service_name", "oil change") if cleaned_data else "oil change",
                "promo_description": offer_details,
                "category": "oil change",
                "contact": competitor.get("address", ""),
                "location": competitor.get("address", ""),
                "offer_details": offer_details,
                "ad_title": promotion_title,
                "ad_text": section_text[:500],
                "new_or_updated": "new",
                "date_scraped": datetime.now().isoformat(),
                "discount_value": discount_value,
                "coupon_code": coupon_code,
                "expiry_date": expiry_date,
                "promotion_title": promotion_title,
                "normalized_title": normalize_title(promotion_title)
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
    
    # Step 4: Deduplicate using complex rules
    logger.info(f"Found {len(all_promos)} promotions before deduplication")
    
    # Group 1: By discount_value + coupon_code
    # Normalize discount values (e.g., "$10" = "$10.00")
    groups_by_discount_code = {}
    for promo in all_promos:
        discount = promo.get("discount_value")
        code = promo.get("coupon_code") or "none"
        
        # Normalize discount for grouping (e.g., "$10" = "$10.00")
        normalized_discount = "none"
        if discount and discount != "N/A":
            # Extract numeric value
            discount_match = re.search(r'(\d+(?:\.\d+)?)', str(discount))
            if discount_match:
                # Normalize to same format
                numeric_val = float(discount_match.group(1))
                normalized_discount = f"${numeric_val:.2f}" if numeric_val % 1 != 0 else f"${int(numeric_val)}"
            else:
                normalized_discount = str(discount).lower()
        
        key = (normalized_discount, code)
        if key not in groups_by_discount_code:
            groups_by_discount_code[key] = []
        groups_by_discount_code[key].append(promo)
    
    # Select best promo from each discount+code group
    deduplicated_by_discount_code = []
    for key, group in groups_by_discount_code.items():
        if len(group) == 1:
            deduplicated_by_discount_code.append(group[0])
        else:
            # Select best promo (most complete info - prefer one with coupon code)
            best = max(group, key=lambda p: (
                bool(p.get("coupon_code") and p.get("coupon_code") != "none"),  # Prefer one with code
                len(p.get("offer_details", "")),
                len(p.get("promotion_title", "")),
                bool(p.get("expiry_date"))
            ))
            # Merge all in group to get complete info
            if len(group) > 1:
                for other_promo in group:
                    if other_promo != best:
                        best = merge_promos(best, other_promo)
            deduplicated_by_discount_code.append(best)
            logger.info(f"Grouped {len(group)} promos by discount+code, merged and kept best")
    
    # Group 2: By normalized title (if no discount/code)
    groups_by_title = {}
    for promo in deduplicated_by_discount_code:
        if not promo.get("discount_value") and not promo.get("coupon_code"):
            norm_title = promo.get("normalized_title", "")
            # Skip if normalized title is too short (likely not a real promo)
            if norm_title and len(norm_title.split()) >= 3:
                if norm_title not in groups_by_title:
                    groups_by_title[norm_title] = []
                groups_by_title[norm_title].append(promo)
    
    # Select best promo from each title group
    final_promos = []
    title_grouped_indices = set()
    
    for norm_title, group in groups_by_title.items():
        if len(group) > 1:
            best = max(group, key=lambda p: (
                len(p.get("offer_details", "")),
                len(p.get("promotion_title", "")),
                bool(p.get("expiry_date"))
            ))
            final_promos.append(best)
            # Mark these for removal from deduplicated list
            for p in group:
                if p != best:
                    title_grouped_indices.add(id(p))
            logger.info(f"Grouped {len(group)} promos by normalized title, kept best")
        else:
            final_promos.append(group[0])
    
    # Add promos that weren't grouped by title
    # Also merge promotions with same normalized discount (e.g., $10 and $10.00)
    discount_groups = {}
    for promo in deduplicated_by_discount_code:
        if (promo.get("discount_value") or promo.get("coupon_code")) or id(promo) not in title_grouped_indices:
            discount = promo.get("discount_value")
            if discount and discount != "N/A":
                # Normalize discount for grouping
                discount_match = re.search(r'(\d+(?:\.\d+)?)', str(discount))
                if discount_match:
                    numeric_val = float(discount_match.group(1))
                    normalized_discount = f"${numeric_val:.2f}" if numeric_val % 1 != 0 else f"${int(numeric_val)}"
                    
                    if normalized_discount not in discount_groups:
                        discount_groups[normalized_discount] = []
                    discount_groups[normalized_discount].append(promo)
                else:
                    # Non-numeric discount, add directly
                    if promo not in final_promos:
                        final_promos.append(promo)
            else:
                # No discount, add directly
                if promo not in final_promos:
                    final_promos.append(promo)
    
    # Merge promotions with same normalized discount
    for normalized_discount, group in discount_groups.items():
        if len(group) == 1:
            if group[0] not in final_promos:
                final_promos.append(group[0])
        else:
            # Merge all with same normalized discount
            merged = group[0]
            for other in group[1:]:
                merged = merge_promos(merged, other)
            if merged not in final_promos:
                final_promos.append(merged)
            logger.info(f"Merged {len(group)} promos with same normalized discount ({normalized_discount})")
    
    # Final pass: Merge promos with same code but different discounts, and similar titles (60%+ overlap)
    merged_promos = []
    processed_indices = set()
    
    for i, promo1 in enumerate(final_promos):
        if i in processed_indices:
            continue
        
        merged = promo1.copy()
        processed_indices.add(i)
        
        # Look for similar promos to merge
        for j, promo2 in enumerate(final_promos[i+1:], start=i+1):
            if j in processed_indices:
                continue
            
            if are_promos_similar(merged, promo2):
                merged = merge_promos(merged, promo2)
                processed_indices.add(j)
                logger.info(f"Merged similar promos: {merged.get('promotion_title')[:50]} with {promo2.get('promotion_title')[:50]}")
        
        merged_promos.append(merged)
    
    # Final cleanup: Remove any remaining terms/conditions promotions
    final_cleaned = []
    for promo in merged_promos:
        title = promo.get("promotion_title", "").lower()
        discount = promo.get("discount_value")
        code = promo.get("coupon_code")
        
        # Skip if it's clearly just terms/conditions
        if title.startswith(("*cannot", "cannot be combined", "*terms")) or \
           ("one coupon per visit" in title and "valid at participating" in title):
            if not discount and not code:
                logger.info(f"Removing terms/conditions from final results: {promo.get('promotion_title')[:60]}")
                continue
        
        final_cleaned.append(promo)
    
    logger.info(f"Total unique promotions found: {len(final_cleaned)}")
    return final_cleaned


def scrape_jiffy(competitor: Dict) -> Dict:
    """Main entry point for Jiffy Lube scraper."""
    try:
        promos = process_jiffy_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'jiffy').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Jiffy Lube: {e}", exc_info=True)
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
    
    # Find Jiffy Lube
    jiffy = next((c for c in competitors if "jiffy" in c.get("name", "").lower()), None)
    
    if not jiffy:
        logger.error("Jiffy Lube not found in competitor list")
        sys.exit(1)
    
    result = scrape_jiffy(jiffy)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

