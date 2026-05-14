"""Jiffy Lube scraper - Text-based HTML extraction."""
import base64
import json
import os
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


# ---------------------------------------------------------------------------
# V2 PIPELINE (Phase 5 — Scope of Work)
# ---------------------------------------------------------------------------
# The functions below are an ADDITIVE extension. They DO NOT replace or
# modify scrape_jiffy() / process_jiffy_promotions(). The legacy entry point
# remains 100% intact for backward compatibility.
#
# The v2 pipeline:
#   - Reads city/store-aware config from app/config/competitors.v2.json
#   - Scrapes 1 national page + N city/store pages
#   - Cross-page dedupes (a national coupon repeated on store pages is
#     attributed to "national" and not re-emitted from each store)
#   - Fans out national coupons across all applicable cities
#   - Classifies services using the standard 9-item taxonomy
#   - Adds optional output columns (city, store_name, source_scope,
#     extraction_method, confidence, needs_review) on top of the existing
#     14-column schema
#   - Adds OCR safety net for coupon-looking <img> tags (text + image_ocr)
# ---------------------------------------------------------------------------

from urllib.parse import urljoin  # noqa: E402  (deliberately after legacy code)
from app.utils.service_classifier import classify_service  # noqa: E402


# ----- v2 quality helpers --------------------------------------------------

# Common English / UI words that frequently get mis-extracted as coupon codes
# on Jiffy Lube pages where the HTML contains words like "LINK", "GET",
# "COUPON", etc. in uppercase.
_COUPON_CODE_BLOCKLIST = {
    "LINK", "LINKS", "FILL", "PER", "VISIT", "THE", "AND", "FOR", "OFF",
    "GET", "GOT", "USE", "JIFFY", "LUBE", "PENNZOIL", "SHELL", "OIL",
    "FULL", "MILES", "BONUS", "ONLY", "NOW", "HERE", "NEAR", "STORE",
    "STORES", "OPEN", "CLOSE", "TODAY", "FREE", "FREEZE", "WARRANTY",
    "APPROVED", "NECESSARY", "DIRECTIONS", "APPOINTMENT", "EXPIRES",
    "EXPIRE", "VALID", "BARCODE", "BROWS", "COUPON", "COUPONS",
    "PROMO", "OFFER", "OFFERS", "TERMS", "SAVINGS", "REWARDS", "POINTS",
    "WITH", "FROM", "INTO", "OVER", "UNDER", "ABOUT",
}


_CODE_SUFFIX_NOISE = [
    "EXPIRES", "EXPIRE", "EXPIRY", "EXPIRED", "EXP",
    "ENDS", "ENDING",
    "VALID", "VALIDUNTIL",
    "OFF", "OFFER", "OFFERS",
    "USE", "USECODE",
    "CODE",
    "TERMS", "ANDCONDITIONS",
]


def _clean_extracted_code(raw: Optional[str]) -> Optional[str]:
    """Trim common English-word suffixes accidentally concatenated onto a
    coupon code (e.g. 'ZE4Y82EXPIRES' -> 'ZE4Y82').
    """
    if not raw:
        return None
    code = raw.strip().upper()
    changed = True
    while changed and code:
        changed = False
        for noise in _CODE_SUFFIX_NOISE:
            if code.endswith(noise) and len(code) > len(noise) + 2:
                code = code[: -len(noise)]
                changed = True
                break
    code = code.strip()
    if not code or code in _COUPON_CODE_BLOCKLIST:
        return None
    if not re.search(r"\d", code) and len(code) < 5:
        return None
    if not re.fullmatch(r"[A-Z0-9_\-]{3,20}", code):
        return None
    return code


def _v2_extract_coupon_code(text: str) -> Optional[str]:
    """Stricter coupon-code extractor used by the v2 pipeline.

    Only accepts codes that follow explicit cues ("code:", "promo code:",
    "use code", "coupon code") AND aren't in the English-word blocklist.
    """
    if not text:
        return None
    explicit_patterns = [
        r"(?:promo\s*code|coupon\s*code|use\s*code|code)\s*[:\-]\s*([A-Z0-9]{3,30})",
        r"use\s*the\s*code\s*([A-Z0-9]{3,30})",
        r"enter\s*code\s*([A-Z0-9]{3,30})",
    ]
    for pat in explicit_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        cleaned = _clean_extracted_code(m.group(1))
        if cleaned:
            return cleaned
    return None


def _v2_extract_discount(text: str) -> Optional[str]:
    """Discount extractor that resists '$150FF Full Synthetic' style typos.

    Accepts values only when followed by a clear delimiter (space, EOL,
    punctuation, %, OFF/off). Caps single-token integer matches at 3 digits.
    """
    if not text:
        return None
    boundary = r"(?=$|[\s,.\)/]|OFF|off|%|\b)"

    # (pattern, kind) — kind: "usd" for dollar amounts, "pct" for percentages
    patterns = [
        (r"\$(\d{1,3}(?:\.\d{1,2})?)\s*OFF\b", "usd"),
        (r"\$(\d{1,3}(?:\.\d{1,2})?)\s*off\b", "usd"),
        (rf"save\s*\$?(\d{{1,3}}(?:\.\d{{1,2}})?){boundary}", "usd"),
        (rf"\$(\d{{1,3}}(?:\.\d{{1,2}})?){boundary}", "usd"),
        (r"(\d{1,2})\s*%\s*off", "pct"),
        (r"(\d{1,2})\s*%", "pct"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip()
        if kind == "pct":
            try:
                return f"{int(float(raw))}%"
            except ValueError:
                continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 999:
            continue
        return f"${raw}"
    if re.search(r"\bfree\b", text, re.IGNORECASE):
        return "free"
    return None


# Tokens to drop when building a coupon signature from a noisy title.
_SIGNATURE_STOPWORDS = {
    # Address/place words
    "ave", "avenue", "st", "street", "rd", "road", "dr", "drive", "blvd",
    "way", "place", "pl", "trail", "trl", "circle", "cir", "lane", "ln",
    "nw", "ne", "sw", "se", "n", "s", "e", "w", "north", "south", "east", "west",
    "ab", "alberta",
    "calgary", "edmonton", "grande", "prairie", "highland", "beverly",
    "clareview", "downtown", "claires", "granville", "killarney", "manning",
    "northgate", "common", "ellerslie", "tamarack", "terra", "losa",
    "terwillegar", "whyte", "windermere", "blvd",
    # Page boilerplate
    "warranty", "approved", "no", "appointment", "necessary", "necessarydirections",
    "directions", "store", "stores", "location", "locations", "service",
    "services", "near", "open", "hours", "today", "phone", "call", "browse",
    "browser", "support", "does", "your", "the", "and", "or", "of", "to", "in",
    "on", "with", "for", "a", "an", "at", "is", "as", "by",
    # Phone-ish leftovers (we also regex-strip phone numbers below)
    # UI noise
    "get", "click", "here", "see", "more", "details", "view",
}


def _signature_meaningful_tokens(title: str) -> str:
    """Return only "promo-meaningful" tokens from a title for deduplication."""
    if not title:
        return ""
    # Strip phone numbers and street-number prefixes
    cleaned = re.sub(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", " ", title)
    cleaned = re.sub(r"\d", " ", cleaned)  # drop standalone digits (addresses)
    cleaned = re.sub(r"[^a-zA-Z\s]+", " ", cleaned).lower()
    tokens = [t for t in cleaned.split() if t and t not in _SIGNATURE_STOPWORDS and len(t) > 1]
    # Deduplicate + sort for a stable signature
    return " ".join(sorted(set(tokens)))[:120]


def _focused_promo_text(text: str) -> str:
    """Return a coupon-focused text snippet for service classification.

    Picks lines containing discount/code/expiry cues plus 2 lines of
    context, so that classification doesn't drift to unrelated menu text
    (e.g. 'Transmission Fluid Service' from a sidebar).
    """
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    cue = re.compile(r"\$\s*\d|\d+\s*%|free|bonus|coupon|code|expires?|pennzoil|oil change|brake|tire|battery|transmission|radiator|fuel system", re.IGNORECASE)
    keep_indices = set()
    for i, ln in enumerate(lines):
        if cue.search(ln):
            for j in range(max(0, i - 1), min(len(lines), i + 3)):
                keep_indices.add(j)
    if not keep_indices:
        return text[:400]
    focused_lines = [lines[i] for i in sorted(keep_indices)]
    return "\n".join(focused_lines)[:600]


_TERMS_PHRASES = [
    "cannot be combined",
    "one coupon per visit",
    "valid at participating",
    "terms and conditions",
    "no cash value",
    "restrictions apply",
    "browser does not support",
    "your browser",
]


def _is_terms_only(title: str, body: str, discount: Optional[str], code: Optional[str]) -> bool:
    """True when the row is pure terms/conditions boilerplate (no real offer)."""
    if discount or code:
        return False
    haystack = " ".join(filter(None, [title or "", body or ""])).lower()
    if not haystack:
        return True
    title_lower = (title or "").lower().strip().lstrip("*").strip()
    if title_lower.startswith(("cannot be combined", "terms and conditions", "terms", "one coupon per visit")):
        return True
    matched = sum(1 for phrase in _TERMS_PHRASES if phrase in haystack)
    # Strip known T&C phrases before checking for offer cues — prevents "coupon"
    # in "one coupon per visit" from being mistaken for a real promotional cue.
    offer_check_text = haystack
    for phrase in _TERMS_PHRASES:
        offer_check_text = offer_check_text.replace(phrase, " ")
    if matched >= 2 and not re.search(r"\$\d|\d+\s*%|free|bonus|miles|rewards", offer_check_text):
        return True
    if title and len(title.split()) <= 8 and matched >= 1:
        return True
    return False


def _has_real_benefit(
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    offer_details: Optional[str],
    promotion_title: Optional[str],
) -> bool:
    """True only when the row contains at least one real promotional benefit.

    A promo must have a discount, coupon code, bonus/reward language, or
    explicit promotional phrasing. Pure generic text like "Oil change at Jiffy
    Lube." without any actual offer is rejected.
    """
    if discount or code:
        return True
    combined = " ".join(filter(None, [offer_details or "", promotion_title or ""])).lower()
    if re.search(
        r"\b(save|off\b|free\b|bonus|reward|rebate|miles|air\s*miles|earn|get\s+\$|"
        r"coupon|deal|special\s+offer|promo|redeem|discount)\b",
        combined,
    ):
        return True
    # Expiry is meaningful only when genuine promotional language accompanies it.
    if expiry and re.search(r"\b(save|off\b|free\b|bonus|coupon|deal|special|promo)\b", combined):
        return True
    return False


# ----- v2 helpers (existing) ----------------------------------------------


def _normalize_discount(value: Optional[str]) -> str:
    """Normalize a discount string to a canonical form for cross-page dedupe.

    Examples:
        "$10"      -> "usd:10"
        "$10.00"   -> "usd:10"
        "$10.50"   -> "usd:10.50"
        "20%"      -> "pct:20"
        "free"     -> "free"
        None       -> "none"
    """
    if not value:
        return "none"
    s = str(value).strip().lower()
    if "free" in s:
        return "free"
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if m:
        try:
            return f"pct:{int(float(m.group(1)))}"
        except ValueError:
            return f"pct:{m.group(1)}"
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", s)
    if m:
        val = float(m.group(1))
        return f"usd:{int(val)}" if val.is_integer() else f"usd:{val}"
    return s[:40]


def _normalize_discount_value(value: Optional[str]) -> Optional[str]:
    """Normalize a display discount string: "$10.00" → "$10", "$10.50" → "$10.50"."""
    if not value:
        return value
    return re.sub(r"(\$\s*\d+)\.00\b", r"\1", str(value).strip())


def _build_promo_signature(promo: Dict) -> str:
    """Build a stable signature so the same coupon found on multiple pages
    (national + store, or store + store) is detected as one entity.

    v2 strategy:
      - When discount AND code are both present, the signature ignores the
        title entirely (titles get polluted by store-address text).
      - Otherwise we strip address / city / boilerplate tokens before
        hashing what's left.
    """
    discount = _normalize_discount(promo.get("discount_value"))
    code = (promo.get("coupon_code") or "").strip().upper() or "none"
    if discount != "none" and code != "none":
        return f"d={discount}|c={code}"
    title = promo.get("normalized_title") or normalize_title(promo.get("promotion_title", "")) or ""
    body = (promo.get("offer_details") or "")[:300]
    meaningful = _signature_meaningful_tokens(title + " " + body)
    return f"d={discount}|c={code}|t={meaningful}"


def _confidence_from_promo(promo: Dict) -> str:
    """Heuristic confidence rating used for the optional `confidence` column."""
    score = 0
    if promo.get("discount_value"):
        score += 2
    if promo.get("coupon_code"):
        score += 2
    if promo.get("expiry_date"):
        score += 1
    if promo.get("offer_details") and len(promo["offer_details"]) > 60:
        score += 1
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


_COUPON_IMG_HINTS = re.compile(
    r"(coupon|promo|offer|deal|special|save|rebate|discount)",
    re.IGNORECASE,
)

# Target cities for this Scope of Work. Any promo whose text mentions
# OTHER Canadian cities (and does NOT mention any of these) is out of area.
_TARGET_CITIES = ("calgary", "edmonton", "grande prairie")

# Common Canadian city/region tokens that indicate a coupon is NOT for our
# target area. The Kelowna-only $20 Pennzoil banner is the immediate
# motivating case; this list is intentionally narrow to avoid false positives.
_OUT_OF_AREA_TOKENS = (
    "kelowna", "westbank", "west kelowna",
    "chilliwack", "chiliwack",  # filename misspelling seen on Jiffy CDN
    "vancouver", "burnaby", "surrey", "richmond", "abbotsford",
    "victoria", "nanaimo",
    "saskatoon", "regina",
    "winnipeg",
    "toronto", "mississauga", "brampton", "scarborough", "etobicoke",
    "ottawa",
    "montreal", "quebec city",
    "halifax", "moncton", "fredericton",
    "st. john", "st john's", "newfoundland",
    "okgn",  # filename hint used on the Kelowna banner image
)


def _is_out_of_area(text: str) -> Optional[str]:
    """If `text` mentions an out-of-area locality and does NOT mention any of
    the target cities, return a human-readable reason. Otherwise None.
    """
    if not text:
        return None
    haystack = text.lower()
    if any(city in haystack for city in _TARGET_CITIES):
        return None
    for token in _OUT_OF_AREA_TOKENS:
        if token in haystack:
            return f"out_of_area:{token}"
    return None


def _image_url_is_out_of_area(image_url: str) -> Optional[str]:
    """Quick pre-OCR filter based on the image filename alone.

    Retained for backward compat / non-QA pipelines, but in QA mode we
    treat filename as a hint only and let OCR decide.
    """
    if not image_url:
        return None
    name = image_url.lower()
    for token in _OUT_OF_AREA_TOKENS:
        if token in name:
            return f"out_of_area_image:{token}"
    return None


def _filename_city_hint(image_url: str) -> Optional[str]:
    """Return the first city/region token found in the filename (or None)."""
    if not image_url:
        return None
    name = image_url.lower()
    for token in _OUT_OF_AREA_TOKENS:
        if token in name:
            return token
    for target in _TARGET_CITIES:
        if target.replace(" ", "") in name.replace(" ", ""):
            return target
    return None


_BC_TEXT_TOKENS = ("kelowna", "westbank", "west kelowna", "british columbia", " bc ", " b.c.")
_ALLOWLIST_TEXT_TOKENS = (
    "alberta", " ab ", " ab,", "canada", "participating", "all locations",
    "any location", "any jiffy lube",
)


def _ocr_text_city_decision(text: str) -> tuple:
    """Decide whether OCR-extracted text is in-scope for our target cities.

    Returns ``(verdict, reason, decision_source, note)`` where:
      - verdict: "allow" | "exclude"
      - reason: explanation if excluded (e.g. "out_of_area_ocr_text:kelowna"); None when allowed
      - decision_source: "ocr_text" | "source_url"
      - note: optional human-readable note (e.g. when falling back to source URL)
    """
    if not text:
        return "allow", None, "source_url", "No city mentioned in image; city inferred from source URL."

    haystack = " " + text.lower() + " "

    has_target = any(city in haystack for city in _TARGET_CITIES)
    bc_hit = next((t for t in _BC_TEXT_TOKENS if t in haystack), None)
    if bc_hit and not has_target:
        return "exclude", f"out_of_area_ocr_text:{bc_hit.strip()}", "ocr_text", None

    other_out = None
    for token in _OUT_OF_AREA_TOKENS:
        if token in _BC_TEXT_TOKENS:
            continue
        if token in haystack and not has_target:
            other_out = token
            break
    if other_out:
        return "exclude", f"out_of_area_ocr_text:{other_out}", "ocr_text", None

    if has_target:
        return "allow", None, "ocr_text", None

    if any(t in haystack for t in _ALLOWLIST_TEXT_TOKENS):
        return "allow", None, "ocr_text", None

    return "allow", None, "source_url", "No city mentioned in image; city inferred from source URL."


def _extract_coupon_image_urls(html: str, page_url: str) -> List[str]:
    """Find <img> tags that look like coupon/promo art and return absolute URLs."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    candidates: List[str] = []
    seen: set = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src or src.startswith("data:"):
            continue
        alt = img.get("alt", "") or ""
        cls = " ".join(img.get("class", []) or [])
        haystack = " ".join([src, alt, cls])
        if not _COUPON_IMG_HINTS.search(haystack):
            continue
        # Skip obvious non-coupon images
        if any(skip in src.lower() for skip in ["logo", "icon", "favicon", "sprite"]):
            continue
        try:
            absolute = urljoin(page_url, src)
        except Exception:
            absolute = src
        if absolute in seen:
            continue
        seen.add(absolute)
        candidates.append(absolute)
    return candidates[:8]  # cap to keep OCR cost bounded


def _ocr_with_vision_rest(image_path: Path) -> Optional[str]:
    """Call Google Cloud Vision TEXT_DETECTION via REST API using an API key.

    Used when no service-account JSON is available but GOOGLE_CLOUD_VISION_API_KEY
    is set in the environment (loaded from .env).
    """
    import requests as _requests
    api_key = os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
    if not api_key:
        return None
    try:
        content = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [{
                "image": {"content": content},
                "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
            }]
        }
        resp = _requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        error = data.get("responses", [{}])[0].get("error")
        if error:
            logger.warning(f"[ocr] Vision REST API error response: {error}")
            return None
        annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
        if annotations:
            text = annotations[0].get("description", "").strip()
            logger.info(f"[ocr] Vision REST API returned {len(text)} chars for {image_path.name}")
            return text or None
        logger.info(f"[ocr] Vision REST API returned no annotations for {image_path.name}")
        return None
    except Exception as exc:
        logger.warning(f"[ocr] Vision REST API exception for {image_path.name}: {exc}")
        return None


def _ocr_promo_from_image(image_url: str) -> tuple:
    """Download + OCR a single image.

    Returns ``(promo_or_None, status, diag_dict)`` where status is one of:
      - "download_failed"       — never reached ``ocr_image()``
      - "ocr_failed"            — ``ocr_image()`` ran and returned empty/error
      - "ocr_success_non_promo" — OCR returned text but it isn't coupon-shaped
      - "ocr_success"           — OCR returned promo-shaped text
    """
    import os as _os
    diag: Dict = {"image_url": image_url, "ocr_attempted": False}
    try:
        from app.extractors.images.image_downloader import download_image
        from app.extractors.ocr.ocr_processor import ocr_image

        path = download_image(image_url)
        if not path:
            diag["failure_reason"] = "download_image() returned None"
            logger.warning(f"[ocr] download_failed for {image_url}: download returned None")
            return None, "download_failed", diag

        diag["downloaded_path"] = str(path)
        try:
            diag["file_size"] = _os.path.getsize(str(path))
        except Exception:
            diag["file_size"] = None
        logger.info(
            f"[ocr] Downloaded {image_url} → {path} (size={diag.get('file_size')} bytes)"
        )

        diag["ocr_attempted"] = True
        # Prefer REST API (API key auth) — works without a service account JSON.
        text = _ocr_with_vision_rest(path)
        if not text:
            try:
                text = ocr_image(path)  # client library → Tesseract fallback
            except Exception as exc:  # noqa: BLE001
                diag["failure_reason"] = f"ocr_image raised: {exc}"
                logger.warning(f"[ocr] ocr_image raised for {image_url}: {exc}")
                return None, "ocr_failed", diag

        text_len = len((text or "").strip())
        diag["text_length"] = text_len
        diag["text_preview"] = (text or "")[:300] if text_len > 0 else ""
        logger.info(
            f"[ocr] OCR result for {image_url}: text_length={text_len}, "
            f"preview={diag['text_preview'][:80]!r}"
        )

        if not text or text_len < 15:
            diag["failure_reason"] = f"OCR returned too little text (len={text_len})"
            logger.warning(f"[ocr] ocr_failed (too short) for {image_url}: len={text_len}")
            return None, "ocr_failed", diag

        if not re.search(r"(coupon|offer|save|free|\$\d|\d+\s*%|expires?|valid)", text, re.IGNORECASE):
            diag["failure_reason"] = "OCR text lacks promo keywords"
            logger.info(f"[ocr] ocr_success_non_promo for {image_url}")
            return None, "ocr_success_non_promo", diag

        discount = _v2_extract_discount(text)
        code = _v2_extract_coupon_code(text)
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        logger.info(f"[ocr] ocr_success for {image_url}: discount={discount!r} code={code!r}")
        return (
            {
                "promotion_title": first_line[:100] or "Jiffy Lube Coupon (image)",
                "discount_value": discount,
                "coupon_code": code,
                "offer_details": text[:1000],
                "ad_text": text[:500],
                "_ocr_image_url": image_url,
            },
            "ocr_success",
            diag,
        )
    except Exception as exc:  # noqa: BLE001
        diag["failure_reason"] = f"pipeline exception: {exc}"
        logger.warning(f"[ocr] OCR pipeline error for {image_url}: {exc}")
        return None, "download_failed", diag


_STORE_DIRECTORY_MARKERS = (
    "warranty approved",
    "no appointment necessary",
    "no appointment needed",
    "no appointment ",
    "store hours",
    "directions",
    "get directions",
    "open today",
    "currently closed",
    "now open",
    "phone number",
    "view location",
    "find a location",
    "schedule service",
    "services we offer",
    "services offered",
    "now hiring",
)

_STORE_ADDRESS_RE = re.compile(
    r"^\s*[A-Z][\w\s\.&'-]{2,40}?"          # neighborhood/store label
    r"\s*\d{1,5}[A-Za-z]?[-\s]?\d{0,4}"      # street number (1230 / 9514-100)
    r"\s+[A-Za-z0-9 .'-]{2,30}"
    r"(?:Ave|St|Street|Avenue|Rd|Road|Blvd|Boulevard|Trail|Way|Cres|Crescent|Drive|Dr|Highway|Hwy)"
    r"(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?\b",
    re.IGNORECASE,
)


def _looks_like_store_directory_section(
    *,
    promotion_title: str,
    section_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    has_coupon_marker: bool,
) -> bool:
    """Reject sections that are obviously a store-info / services-menu block.

    A row is rejected when ALL of these hold:
      - No discount, no coupon code, no expiry date.
      - No 'coupon / save / get coupon / off / free' marker.
      - The text or title carries store-directory markers OR the title
        starts with a street-address pattern.
    """
    if discount or code or expiry or has_coupon_marker:
        return False
    haystack = f"{promotion_title}\n{section_text}".lower()
    marker_hits = sum(1 for m in _STORE_DIRECTORY_MARKERS if m in haystack)
    if marker_hits >= 1:
        return True
    if _STORE_ADDRESS_RE.match(promotion_title or ""):
        return True
    return False


_PRODUCT_PATTERNS = [
    r"Pennzoil\s+(?:Full\s+)?Synthetic(?:\s+Motor\s+Oil)?",
    r"Pennzoil[^\.\n]{0,40}",
    r"Jiffy\s+Lube\s+Signature\s+Service\s+oil\s+change",
    r"Jiffy\s+Lube\s+Signature\s+Service",
    r"oil\s+change",
    r"tire\s+rotation",
    r"battery",
    r"brake",
    r"transmission\s+fluid",
    r"radiator\s+flush",
    r"fuel\s+system\s+flush",
]

_NOISE_PHRASES = [
    r"GET\s+COUPON", r"FILL\s+IN\s+YOUR\s+INFORMATION.*",
    r"Fill\s+in\s+(?:the\s+)?form.*", r"Sign\s+up.*",
    r"Download\s+the\s+Shell\s+App.*",
    r"Not\s+a\s+Shell\s+Go\+\s+member\s+yet\??",
    r"Click\s+here.*", r"Print\s+coupon.*",
    r"Cannot\s+be\s+combined\s+with.*", r"One\s+coupon\s+per\s+visit\.?",
    r"Valid\s+at\s+participating\s+locations.*",
    r"\*?Expires?:?\s*[A-Za-z]+\s+\d{1,2}(st|nd|rd|th)?,?\s*\d{4}",
]


def _summarize_promo_description(
    *,
    promotion_title: str,
    offer_details: str,
    discount: Optional[str],
    code: Optional[str],
    std_service: str,
    ad_text: str,
    brand: str = "Jiffy Lube",
) -> str:
    """Produce a short, customer-facing summary (~1 sentence, ≤25 words).

    The full raw text continues to live in `ad_text`. This helper does NOT
    invent facts; it picks/strips from what the extractor already found.
    """
    title = (promotion_title or "").strip()
    body = (offer_details or "").strip() or (ad_text or "").strip()
    combined = body + " " + title

    # --- $10/ZE4Y82 canonical format -----------------------------------------
    if code == "ZE4Y82":
        disc_display = discount or "$10"
        return f"{disc_display} off Pennzoil Synthetic oil change at Jiffy Lube (code {code})."

    # --- Shell Go+ / AIR MILES special case ----------------------------------
    # Try to find the bonus amount in the combined text; fall back to discount.
    airmiles_m = re.search(r"(\d+)\s+bonus\s+(?:AIR\s*MILES|Miles)", combined, re.IGNORECASE)
    if airmiles_m is None and discount:
        airmiles_m = re.search(r"(\d+)\s+bonus\s+(?:AIR\s*MILES|Miles)", discount, re.IGNORECASE)
    if airmiles_m or re.search(r"shell\s+go\+", combined, re.IGNORECASE):
        amount = airmiles_m.group(1) if airmiles_m else None
        product_m = re.search(
            r"(Pennzoil\s+(?:Full\s+)?Synthetic(?:\s+Motor\s+Oil)?)",
            combined,
            re.IGNORECASE,
        )
        product_raw = product_m.group(0) if product_m else "Pennzoil Full Synthetic"
        # Strip trailing "Motor Oil" to avoid "Motor Oil oil change" redundancy.
        product = re.sub(r"\s*Motor\s*Oil\s*$", "", product_raw, flags=re.IGNORECASE).strip()
        if not product:
            product = "Pennzoil Full Synthetic"
        if amount:
            desc = f"Shell Go+ members get {amount} bonus AIR MILES with {product} oil change."
        else:
            desc = f"Shell Go+ member offer on {product} oil change at Jiffy Lube."
        if code:
            desc = desc.rstrip(".") + f" (code {code})."
        return desc

    # Strip noise before building the main summary.
    text = body
    for pat in _NOISE_PHRASES:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,-•|")

    # --- Discount prefix -----------------------------------------------------
    # Only add "off" for dollar/percentage discounts, not bonus-reward values.
    parts: List[str] = []
    if discount:
        is_bonus = bool(re.search(
            r"\bbonus\b|\bAIR\s*MILES\b|\bmiles\b|\brewards?\b|\bearn\b",
            discount,
            re.IGNORECASE,
        ))
        parts.append(discount if is_bonus else f"{discount} off")

    # --- Product/service hint (specific → generic) ---------------------------
    benefit_hint = ""
    for pattern in _PRODUCT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            benefit_hint = m.group(0).strip()
            # Append "oil change" if the text has it and the hint doesn't already.
            if "oil change" not in benefit_hint.lower() and re.search(r"\boil\s+change\b", text, re.IGNORECASE):
                benefit_hint += " oil change"
            break

    service_word = std_service if std_service and std_service != "Other" else ""

    summary_bits: List[str] = []
    if parts:
        summary_bits.append(" ".join(parts))
    if benefit_hint:
        summary_bits.append(benefit_hint)
    elif service_word:
        summary_bits.append(service_word.lower())

    summary_bits.append(f"at {brand}")
    if code:
        summary_bits.append(f"(code {code})")

    summary = " ".join(s for s in summary_bits if s).strip()
    summary = re.sub(r"\s+", " ", summary)

    if not summary or len(summary) < 6:
        first_line = next(
            (ln.strip() for ln in (title + "\n" + body).split("\n") if ln.strip()),
            f"{brand} promotion",
        )
        summary = first_line

    words = summary.split()
    if len(words) > 25:
        summary = " ".join(words[:25]).rstrip(",;:-") + "…"

    if summary and not summary.endswith((".", "…", ")")):
        summary += "."
    return summary[0].upper() + summary[1:] if summary else summary


def _scrape_jiffy_url(
    url: str,
    *,
    scope: str,
    cities_for_url: List[str],
    store_name: Optional[str],
    competitor_meta: Dict,
    do_ocr: bool = True,
    excluded_log: Optional[List[Dict]] = None,
    ocr_log: Optional[List[Dict]] = None,
) -> Dict:
    """Scrape a single Jiffy Lube URL.

    Returns a status dict with the shape:
        {
          "url": str,
          "status": "ok" | "fetch_failed" | "no_sections",
          "raw_promos": List[Dict],
          "excluded_count": int,
        }

    Each returned promo dict carries the v2 metadata columns alongside the
    existing 14-column schema. `excluded_log` (if provided) is appended to
    with one entry per excluded section (e.g. out-of-area coupons).
    """
    logger.info(f"[v2] Fetching {scope} URL: {url}")
    html = fetch_with_fallback(url)
    if not html:
        logger.error(f"[v2] Failed to fetch {url}")
        return {
            "url": url, "status": "fetch_failed", "raw_promos": [], "excluded_count": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
        }

    sections = extract_promo_sections(html)
    raw_promos: List[Dict] = []
    excluded_here = 0
    text_extracted_count = 0
    image_ocr_extracted_count = 0
    image_ocr_failed_needs_review_count = 0

    # --- Text-based extraction (primary) -----------------------------------
    for section in sections:
        section_text = section.get("text", "")
        section_html = section.get("html", "")

        # v2 stricter extractors (resists $150FF / LINK / PER false positives)
        discount = _v2_extract_discount(section_text)
        code = _v2_extract_coupon_code(section_text)

        cleaned_data = None
        try:
            cleaned_data = clean_promo_text_with_llm(
                section_text,
                context=f"Jiffy Lube coupon/promotion. HTML: {section_html[:800]}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[v2] LLM cleanup failed: {exc}")

        if isinstance(cleaned_data, list):
            cleaned_data = next((c for c in cleaned_data if isinstance(c, dict)), None)
        if not isinstance(cleaned_data, dict):
            cleaned_data = None

        # Prefer LLM-cleaned values, but validate the LLM-suggested coupon code
        # against the blocklist so an LLM hallucination can't reintroduce noise.
        if cleaned_data:
            llm_code = _clean_extracted_code(cleaned_data.get("coupon_code"))
            if llm_code:
                code = code or llm_code
            llm_discount = cleaned_data.get("discount_value")
            discount = discount or llm_discount
            expiry = cleaned_data.get("expiry_date")
        else:
            expiry = None

        # Normalize display discount value (e.g. "$10.00" → "$10").
        discount = _normalize_discount_value(discount)

        if cleaned_data and cleaned_data.get("promo_description"):
            offer_details = cleaned_data["promo_description"]
        else:
            offer_details = section_text[:1000]

        if cleaned_data and cleaned_data.get("service_name"):
            promotion_title = cleaned_data["service_name"]
        elif cleaned_data and cleaned_data.get("promo_description"):
            first_line = (cleaned_data["promo_description"]).split("\n")[0].strip()[:100]
            promotion_title = first_line or (section_text.split("\n")[0][:100] if section_text else "Jiffy Lube Promotion")
        else:
            lines = [ln.strip() for ln in section_text.split("\n") if ln.strip() and len(ln.strip()) > 10]
            promotion_title = lines[0][:100] if lines else "Jiffy Lube Promotion"

        if _is_terms_only(promotion_title, offer_details, discount, code):
            logger.debug(f"[v2] Skipping terms/conditions section: {promotion_title[:60]}")
            continue

        # Reject sections with no real promotional benefit.
        if not _has_real_benefit(discount, code, expiry, offer_details, promotion_title):
            logger.debug(f"[v2] Skipping no-benefit section: {promotion_title[:60]}")
            continue

        # Reject store-directory / services-menu sections that aren't coupons.
        has_coupon_marker = bool(re.search(
            r"\b(coupon|get\s+coupon|save|off\b|free\b|bonus|expires?|"
            r"redeem|promo|deal|special|reward|rebate)\b",
            f"{promotion_title} {section_text}",
            re.IGNORECASE,
        ))
        if _looks_like_store_directory_section(
            promotion_title=promotion_title,
            section_text=section_text,
            discount=discount,
            code=code,
            expiry=expiry,
            has_coupon_marker=has_coupon_marker,
        ):
            logger.info(
                f"[v2] Skipping store-directory section on {url}: {promotion_title[:60]!r}"
            )
            continue

        # Out-of-area filter (Kelowna, Westbank, Vancouver, etc.)
        out_reason = _is_out_of_area(" ".join([promotion_title, offer_details, section_text[:800]]))
        if out_reason:
            excluded_here += 1
            logger.info(f"[v2] Excluded out-of-area section: {out_reason} on {url}")
            if excluded_log is not None:
                excluded_log.append({
                    "url": url,
                    "scope": scope,
                    "extraction_method": "text",
                    "reason": out_reason,
                    "discount_value": discount,
                    "coupon_code": code,
                    "snippet": (offer_details or section_text)[:240],
                })
            continue

        # Service taxonomy: classify on the coupon-focused snippet only,
        # so unrelated menu text (e.g. 'Transmission Fluid Service' in a
        # sidebar) doesn't pollute the classification.
        focused = _focused_promo_text(offer_details or section_text)
        classification_text = " ".join(filter(None, [promotion_title, focused]))
        std_service = classify_service(
            classification_text,
            hint=(cleaned_data.get("service_name") if cleaned_data else None),
        )

        promo = _build_v2_row(
            competitor_meta=competitor_meta,
            page_url=url,
            scope=scope,
            cities_for_url=cities_for_url,
            store_name=store_name,
            extraction_method="text",
            promotion_title=promotion_title,
            offer_details=offer_details,
            ad_text=section_text[:500],
            discount=discount,
            code=code,
            expiry=expiry,
            std_service=std_service,
        )
        promo["source_text_type"] = "html_card"
        promo["promo_description"] = _summarize_promo_description(
            promotion_title=promotion_title,
            offer_details=offer_details,
            discount=discount,
            code=code,
            std_service=std_service,
            ad_text=section_text,
        )
        raw_promos.append(promo)
        text_extracted_count += 1

    # --- OCR safety net (only if page indicates image coupons) --------------
    # QA policy: filename is a HINT only. We download + OCR the image and let
    # the OCR-extracted text decide whether it is out-of-area. If OCR fails,
    # we drop a needs_review row instead of silently swallowing the image.
    seen_images: set = set()
    if do_ocr:
        image_urls = _extract_coupon_image_urls(html, url)
        for img_url in image_urls:
            if img_url in seen_images:
                if ocr_log is not None:
                    ocr_log.append({
                        "url": url, "scope": scope, "image_url": img_url,
                        "status": "skipped_duplicate_image",
                    })
                continue
            seen_images.add(img_url)

            filename_hint = _filename_city_hint(img_url)

            ocr_promo, ocr_status, ocr_diag = _ocr_promo_from_image(img_url)
            if ocr_log is not None:
                ocr_log.append({
                    "url": url, "scope": scope, "image_url": img_url,
                    "status": ocr_status,
                    "filename_hint": filename_hint,
                    **{k: v for k, v in ocr_diag.items() if k != "image_url"},
                })

            # OCR failed or download failed → emit a needs_review placeholder.
            if not ocr_promo:
                if ocr_status in ("ocr_failed", "download_failed", "ocr_success_non_promo"):
                    nr_promo = _build_v2_row(
                        competitor_meta=competitor_meta,
                        page_url=url,
                        scope=scope,
                        cities_for_url=cities_for_url,
                        store_name=store_name,
                        extraction_method="image_ocr_failed" if ocr_status != "ocr_success_non_promo" else "image_ocr_non_promo",
                        promotion_title=f"OCR {ocr_status} — manual review needed",
                        offer_details="",
                        ad_text="",
                        discount=None,
                        code=None,
                        expiry=None,
                        std_service="Other",
                        source_image=img_url,
                    )
                    nr_promo["needs_review"] = True
                    nr_promo["needs_review_reason"] = ocr_diag.get("failure_reason") or ocr_status
                    nr_promo["image_filename_city_hint"] = filename_hint
                    nr_promo["city_decision_source"] = "source_url"
                    nr_promo["source_text_type"] = "image_ocr_failed"
                    nr_promo["promo_description"] = (
                        f"Coupon image could not be processed (status: {ocr_status}). "
                        "Manual review recommended."
                    )
                    raw_promos.append(nr_promo)
                    image_ocr_failed_needs_review_count += 1
                continue

            # OCR succeeded — decide in/out of area from the OCR text.
            ocr_text_blob = " ".join(filter(None, [
                ocr_promo.get("promotion_title"),
                ocr_promo.get("offer_details"),
                ocr_promo.get("ad_text"),
            ]))
            verdict, exclude_reason, decision_source, decision_note = _ocr_text_city_decision(ocr_text_blob)
            if verdict == "exclude":
                excluded_here += 1
                logger.info(f"[v2] Excluded OCR text out-of-area: {exclude_reason} on {url} (img={img_url})")
                if excluded_log is not None:
                    excluded_log.append({
                        "url": url,
                        "scope": scope,
                        "extraction_method": "image_ocr",
                        "reason": exclude_reason,
                        "discount_value": ocr_promo.get("discount_value"),
                        "coupon_code": ocr_promo.get("coupon_code"),
                        "source_image": img_url,
                        "image_filename_city_hint": filename_hint,
                        "snippet": (ocr_promo.get("offer_details") or "")[:240],
                    })
                if ocr_log is not None:
                    last = ocr_log[-1] if ocr_log else None
                    if last and last.get("image_url") == img_url and last.get("status") == "ocr_success":
                        last["status"] = "ocr_success_out_of_area"
                        last["reason"] = exclude_reason
                continue

            classification_text = " ".join(
                filter(None, [ocr_promo.get("promotion_title"), ocr_promo.get("offer_details")])
            )
            std_service = classify_service(classification_text)
            promo = _build_v2_row(
                competitor_meta=competitor_meta,
                page_url=url,
                scope=scope,
                cities_for_url=cities_for_url,
                store_name=store_name,
                extraction_method="image_ocr",
                promotion_title=ocr_promo["promotion_title"],
                offer_details=ocr_promo.get("offer_details") or "",
                ad_text=ocr_promo.get("ad_text") or "",
                discount=ocr_promo.get("discount_value"),
                code=ocr_promo.get("coupon_code"),
                expiry=None,
                std_service=std_service,
                source_image=img_url,
            )
            promo["source_text_type"] = "image_ocr"
            promo["promo_description"] = _summarize_promo_description(
                promotion_title=ocr_promo.get("promotion_title", ""),
                offer_details=ocr_promo.get("offer_details") or "",
                discount=ocr_promo.get("discount_value"),
                code=ocr_promo.get("coupon_code"),
                std_service=std_service,
                ad_text=ocr_promo.get("ad_text") or "",
            )
            promo["image_filename_city_hint"] = filename_hint
            promo["city_decision_source"] = decision_source
            if decision_note:
                promo["city_decision_note"] = decision_note
            raw_promos.append(promo)
            image_ocr_extracted_count += 1

    logger.info(
        f"[v2] {url} → {len(raw_promos)} raw promos (excluded {excluded_here}, "
        f"text={text_extracted_count}, ocr_ok={image_ocr_extracted_count}, "
        f"ocr_failed_nr={image_ocr_failed_needs_review_count})"
    )
    status = "ok" if (raw_promos or sections) else "no_sections"
    return {
        "url": url,
        "status": status,
        "raw_promos": raw_promos,
        "excluded_count": excluded_here,
        "text_extracted_count": text_extracted_count,
        "image_ocr_extracted_count": image_ocr_extracted_count,
        "image_ocr_failed_needs_review_count": image_ocr_failed_needs_review_count,
    }


def _build_v2_row(
    *,
    competitor_meta: Dict,
    page_url: str,
    scope: str,
    cities_for_url: List[str],
    store_name: Optional[str],
    extraction_method: str,
    promotion_title: str,
    offer_details: str,
    ad_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    std_service: str,
    source_image: Optional[str] = None,
) -> Dict:
    """Construct the canonical v2 row.

    The 14 existing output columns are preserved exactly. New optional
    columns are appended (city/store_name/source_scope/extraction_method/
    confidence/needs_review) — these are backward-compatible.
    """
    row: Dict = {
        # Existing 14-column schema (backward-compatible) ------------------
        "website": competitor_meta.get("domain", ""),
        "page_url": page_url,
        "business_name": competitor_meta.get("competitor", "Jiffy Lube"),
        "google_reviews": None,  # filled by the reviews scraper / merger
        "service_name": std_service,
        "promo_description": offer_details,
        "category": std_service,
        "contact": competitor_meta.get("address", ""),
        "location": store_name or ", ".join(cities_for_url),
        "offer_details": offer_details,
        "ad_title": promotion_title,
        "ad_text": ad_text,
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),

        # Internal helpers retained from legacy schema for compatibility ----
        "promotion_title": promotion_title,
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "normalized_title": normalize_title(promotion_title),

        # New optional Phase-4-ready columns -------------------------------
        "city": None,  # set during fan-out
        "store_name": store_name,
        "source_scope": scope,
        "extraction_method": extraction_method,
        "confidence": None,  # set after build
        "needs_review": False,
        "applicable_cities": list(cities_for_url),
    }
    if source_image:
        row["source_image"] = source_image
    row["confidence"] = _confidence_from_promo(row)
    # Mark needs_review for low-confidence rows without any discount/code
    if row["confidence"] == "low" and not row["discount_value"] and not row["coupon_code"]:
        row["needs_review"] = True
    return row


def _fan_out_national_rows(rows: List[Dict]) -> List[Dict]:
    """Expand a national row (1 row × N cities) into per-city rows.

    Each emitted row has a single string `city` value. For national rows we
    set `location = "National"` and `store_name = "National"` so the sheet
    clearly distinguishes them from store-specific rows. Store rows keep
    their store-derived `location` and `store_name`.
    """
    expanded: List[Dict] = []
    for row in rows:
        cities = row.get("applicable_cities") or []
        scope = row.get("source_scope")
        if scope == "national":
            target_cities = cities if cities else [None]
            for c in target_cities:
                copy = dict(row)
                copy["city"] = c
                copy["store_name"] = "National"
                copy["location"] = "National"
                # National rows are never duplicates-of-national.
                copy["duplicate_of_national"] = False
                # The duplicate_group_id (stable signature) is filled in
                # by the orchestrator so store rows can reference it.
                expanded.append(copy)
        else:
            copy = dict(row)
            copy["city"] = cities[0] if cities else None
            if row.get("store_name"):
                copy["location"] = row["store_name"]
            expanded.append(copy)
    return expanded


def _consolidate_card_modal_per_url(rows: List[Dict]) -> List[Dict]:
    """Card/modal consolidation scoped per page_url so that we never collapse
    rows across different URLs in QA mode.

    Within a single page, if two extracted sections share
    (normalized_discount, service_name, expiry) and only differ on whether a
    coupon_code is present, we keep the one with the code (preferring more
    detailed offer text).
    """
    if not rows:
        return []
    groups: Dict[tuple, List[int]] = {}
    for idx, r in enumerate(rows):
        key = (
            r.get("page_url"),
            r.get("source_scope"),
            _normalize_discount(r.get("discount_value")),
            (r.get("service_name") or "").lower(),
            (r.get("expiry_date") or "").strip(),
        )
        groups.setdefault(key, []).append(idx)
    keep_indices: List[int] = []
    for key, idxs in groups.items():
        if len(idxs) == 1:
            keep_indices.append(idxs[0])
            continue
        with_code = [i for i in idxs if rows[i].get("coupon_code")]
        if with_code:
            winner = max(with_code, key=lambda i: len(rows[i].get("offer_details") or ""))
        else:
            winner = max(idxs, key=lambda i: len(rows[i].get("offer_details") or ""))
        keep_indices.append(winner)
    keep_indices.sort()
    return [rows[i] for i in keep_indices]


def scrape_jiffy_v2(
    competitor_v2: Dict,
    *,
    limit_stores_per_city: Optional[int] = None,
    enable_ocr: bool = True,
    mode: str = "qa_expanded",
) -> Dict:
    """Phase 5 entry point: scrape Jiffy Lube using the v2 city/store config.

    Args:
        competitor_v2: A single entry from `app/config/competitors.v2.json`.
        limit_stores_per_city: If set, only scrape the first N stores per
            city. Useful for fast smoke tests. None = scrape all.
        enable_ocr: Whether to run the image-OCR safety net.
        mode: Output mode.
            - "qa_expanded" (default): keep every per-URL row, even when a
              store-page promo matches a national coupon. Each such row is
              tagged with `duplicate_of_national = True` and
              `duplicate_group_id` = the stable national signature.
              Per-URL card/modal teaser pairs ARE still consolidated.
            - "final_deduped": collapse store-page duplicates of national
              coupons (client-ready delivery).

    Returns:
        Standard result dict with `promotions` (list) and `count` (int).
        QA fields: `duplicate_of_national`, `duplicate_group_id`.
    """
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError(f"mode must be qa_expanded or final_deduped, got {mode!r}")

    competitor_meta = {
        "competitor": competitor_v2.get("competitor", "Jiffy Lube"),
        "domain": competitor_v2.get("domain", "jiffylubeservice.ca"),
        "address": "",  # Jiffy Lube has many stores; address per-store handled via store_name
    }

    try:
        national_rows: List[Dict] = []
        store_rows: List[Dict] = []
        excluded_log: List[Dict] = []
        ocr_log: List[Dict] = []
        url_log: List[Dict] = []  # one entry per URL touched
        expected_urls: List[Dict] = []

        # 1) National page(s) - scrape ONCE, fan out to all 3 cities downstream
        national_signatures: set = set()
        for link in competitor_v2.get("promo_links", []):
            url = link.get("url") if isinstance(link, dict) else link
            cities = (
                (link.get("cities") if isinstance(link, dict) else None)
                or competitor_v2.get("cities", [])
            )
            expected_urls.append({"url": url, "scope": "national", "city": None, "store_name": None})
            res = _scrape_jiffy_url(
                url,
                scope="national",
                cities_for_url=cities,
                store_name=None,
                competitor_meta=competitor_meta,
                do_ocr=enable_ocr,
                excluded_log=excluded_log,
                ocr_log=ocr_log,
            )
            added_here = 0
            for promo in res["raw_promos"]:
                sig = _build_promo_signature(promo)
                if sig in national_signatures:
                    continue
                national_signatures.add(sig)
                # National rows always carry their own signature and are
                # never themselves duplicates-of-national.
                promo["duplicate_of_national"] = False
                promo["duplicate_group_id"] = sig
                national_rows.append(promo)
                added_here += 1
            url_log.append({
                "url": url,
                "scope": "national",
                "status": res["status"],
                "raw_promo_count": len(res["raw_promos"]),
                "added_unique": added_here,
                "excluded_count": res["excluded_count"],
                "text_extracted_count": res.get("text_extracted_count", 0),
                "image_ocr_extracted_count": res.get("image_ocr_extracted_count", 0),
                "image_ocr_failed_needs_review_count": res.get("image_ocr_failed_needs_review_count", 0),
                "store_name": None,
                "city": None,
            })
            logger.info(
                f"[v2] National {url}: kept {len(national_rows)} unique so far"
            )

        # 2) Store pages.
        #    - In qa_expanded mode (default): tag duplicates-of-national but
        #      KEEP the rows so every URL contributes visible output.
        #    - In final_deduped mode: drop store rows whose signature matches
        #      a national coupon (legacy behavior).
        seen_store_signatures: set = set()  # only used in final_deduped mode
        store_links_by_city = competitor_v2.get("store_links", {}) or {}
        for city, links in store_links_by_city.items():
            picked_links = links if not limit_stores_per_city else links[:limit_stores_per_city]
            for link in picked_links:
                url = link.get("url") if isinstance(link, dict) else link
                store_name = link.get("store_name") if isinstance(link, dict) else None
                expected_urls.append({"url": url, "scope": "store", "city": city, "store_name": store_name})
                res = _scrape_jiffy_url(
                    url,
                    scope="store",
                    cities_for_url=[city],
                    store_name=store_name,
                    competitor_meta=competitor_meta,
                    do_ocr=enable_ocr,
                    excluded_log=excluded_log,
                    ocr_log=ocr_log,
                )
                store_added_here = 0
                tagged_as_national_dup = 0
                dropped_as_national = 0
                for promo in res["raw_promos"]:
                    sig = _build_promo_signature(promo)
                    is_nat_dup = sig in national_signatures
                    if mode == "final_deduped":
                        if is_nat_dup:
                            dropped_as_national += 1
                            continue
                        store_key = (city, sig)
                        if store_key in seen_store_signatures:
                            continue
                        seen_store_signatures.add(store_key)
                        promo["duplicate_of_national"] = False
                        promo["duplicate_group_id"] = sig
                        store_rows.append(promo)
                        store_added_here += 1
                    else:  # qa_expanded
                        promo["duplicate_of_national"] = bool(is_nat_dup)
                        promo["duplicate_group_id"] = sig
                        if is_nat_dup:
                            tagged_as_national_dup += 1
                        store_rows.append(promo)
                        store_added_here += 1
                url_log.append({
                    "url": url,
                    "scope": "store",
                    "status": res["status"],
                    "raw_promo_count": len(res["raw_promos"]),
                    "added_unique": store_added_here,
                    "tagged_as_national_duplicate": tagged_as_national_dup,
                    "dropped_as_national_duplicate": dropped_as_national,
                    "excluded_count": res["excluded_count"],
                    "text_extracted_count": res.get("text_extracted_count", 0),
                    "image_ocr_extracted_count": res.get("image_ocr_extracted_count", 0),
                    "image_ocr_failed_needs_review_count": res.get("image_ocr_failed_needs_review_count", 0),
                    "store_name": store_name,
                    "city": city,
                })
                logger.info(
                    f"[v2|{mode}] Store {store_name or url} ({city}): kept {store_added_here} rows"
                    f" (nat_dup_tagged={tagged_as_national_dup}, nat_dropped={dropped_as_national})"
                )

        # 3) Final terms/conditions cleanup pass — drop rows that survived
        #    the per-section filter but still look like pure boilerplate.
        def _keep(row: Dict) -> bool:
            return not _is_terms_only(
                row.get("promotion_title", ""),
                row.get("offer_details", ""),
                row.get("discount_value"),
                row.get("coupon_code"),
            )

        national_rows = [r for r in national_rows if _keep(r)]
        store_rows = [r for r in store_rows if _keep(r)]

        # 3b) Card/modal consolidation.
        #     - qa_expanded: scoped per page_url (we never collapse across
        #       URLs, so every URL still produces visible rows).
        #     - final_deduped: collapse globally per (scope + discount +
        #       service + expiry) as before.
        if mode == "final_deduped":
            def _consolidate_global(rows: List[Dict]) -> List[Dict]:
                groups: Dict[tuple, List[int]] = {}
                for idx, r in enumerate(rows):
                    key = (
                        r.get("source_scope"),
                        _normalize_discount(r.get("discount_value")),
                        (r.get("service_name") or "").lower(),
                        (r.get("expiry_date") or "").strip(),
                    )
                    groups.setdefault(key, []).append(idx)
                keep: List[int] = []
                for key, idxs in groups.items():
                    if len(idxs) == 1:
                        keep.append(idxs[0])
                        continue
                    with_code = [i for i in idxs if rows[i].get("coupon_code")]
                    if with_code:
                        winner = max(with_code, key=lambda i: len(rows[i].get("offer_details") or ""))
                    else:
                        winner = max(idxs, key=lambda i: len(rows[i].get("offer_details") or ""))
                    keep.append(winner)
                keep.sort()
                return [rows[i] for i in keep]

            national_rows = _consolidate_global(national_rows)
            store_rows = _consolidate_global(store_rows)
        else:
            national_rows = _consolidate_card_modal_per_url(national_rows)
            store_rows = _consolidate_card_modal_per_url(store_rows)

        # Refresh national signatures (national rows may have been
        # consolidated). Re-stamp the duplicate_group_id so store rows that
        # were tagged earlier still point to a valid surviving signature.
        national_signatures = {r["duplicate_group_id"] for r in national_rows if r.get("duplicate_group_id")}

        # 4) Fan out national rows across all cities (1 → 3 rows)
        fanned_national = _fan_out_national_rows(national_rows)
        fanned_store = _fan_out_national_rows(store_rows)
        all_rows = fanned_national + fanned_store

        # 5) Validation summary -------------------------------------------------
        processed_urls = {entry["url"] for entry in url_log if entry["status"] == "ok"}
        failed_urls = [entry["url"] for entry in url_log if entry["status"] == "fetch_failed"]
        no_section_urls = [entry["url"] for entry in url_log if entry["status"] == "no_sections"]
        expected_url_set = {e["url"] for e in expected_urls}
        missing_urls = sorted(expected_url_set - {entry["url"] for entry in url_log})

        exclusion_reason_counts: Dict[str, int] = {}
        for x in excluded_log:
            reason = x.get("reason", "unknown")
            exclusion_reason_counts[reason] = exclusion_reason_counts.get(reason, 0) + 1

        row_count_by_url: Dict[str, int] = {}
        for r in all_rows:
            u = r.get("page_url") or ""
            row_count_by_url[u] = row_count_by_url.get(u, 0) + 1

        row_count_by_city: Dict[str, int] = {}
        for r in all_rows:
            c = r.get("city") or ""
            row_count_by_city[c] = row_count_by_city.get(c, 0) + 1

        duplicate_of_national_count = sum(1 for r in all_rows if r.get("duplicate_of_national"))
        store_unique_count = sum(
            1 for r in all_rows
            if r.get("source_scope") == "store" and not r.get("duplicate_of_national")
        )

        # Per-URL extraction source counts (aggregated from url_log).
        total_text_extracted = sum(e.get("text_extracted_count", 0) for e in url_log)
        total_image_ocr_extracted = sum(e.get("image_ocr_extracted_count", 0) for e in url_log)
        total_image_ocr_failed_nr = sum(e.get("image_ocr_failed_needs_review_count", 0) for e in url_log)
        text_vs_ocr_source_counts = {
            "html_card": sum(1 for r in all_rows if r.get("source_text_type") == "html_card"),
            "image_ocr": sum(1 for r in all_rows if r.get("source_text_type") == "image_ocr"),
            "image_ocr_failed": sum(1 for r in all_rows if r.get("source_text_type") == "image_ocr_failed"),
        }

        ocr_status_counts: Dict[str, int] = {}
        for x in ocr_log:
            s = x.get("status", "unknown")
            ocr_status_counts[s] = ocr_status_counts.get(s, 0) + 1
        # `ocr_attempted` counts only images where ocr_image() actually ran.
        # Filename-prefilter / duplicate-image / download_failed do NOT count.
        attempted_statuses = (
            "ocr_success", "ocr_success_non_promo",
            "ocr_success_out_of_area", "ocr_failed",
        )
        ocr_attempted = sum(ocr_status_counts.get(s, 0) for s in attempted_statuses)
        ocr_success = (
            ocr_status_counts.get("ocr_success", 0)
            + ocr_status_counts.get("ocr_success_non_promo", 0)
            + ocr_status_counts.get("ocr_success_out_of_area", 0)
        )
        ocr_failed = ocr_status_counts.get("ocr_failed", 0)
        ocr_prefilter_skipped = (
            ocr_status_counts.get("skipped_out_of_area_filename", 0)
            + ocr_status_counts.get("skipped_duplicate_image", 0)
            + ocr_status_counts.get("skipped_prefilter_non_promo", 0)
            + ocr_status_counts.get("download_failed", 0)
        )

        by_scope = {
            "national": sum(1 for r in all_rows if r.get("source_scope") == "national"),
            "store": sum(1 for r in all_rows if r.get("source_scope") == "store"),
        }

        # 6) Save under a v2-specific filename so we don't clobber legacy output
        base = competitor_meta["competitor"].lower().replace(" ", "_").replace(".", "")
        output_file = PROMOTIONS_DIR / f"{base}_v2.json"
        result = {
            "competitor": competitor_meta["competitor"],
            "scraped_at": datetime.now().isoformat(),
            "config_version": "v2",
            "mode": mode,
            "national_unique_count": len(national_rows),
            "store_only_unique_count": len(store_rows),
            "promotions": all_rows,
            "count": len(all_rows),
            "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
            "by_city": {
                "Calgary": sum(1 for r in all_rows if r.get("city") == "Calgary"),
                "Edmonton": sum(1 for r in all_rows if r.get("city") == "Edmonton"),
                "Grande Prairie": sum(1 for r in all_rows if r.get("city") == "Grande Prairie"),
            },
            "by_source_scope": by_scope,
            "validation": {
                "expected_url_count": len(expected_urls),
                "processed_url_count": len(processed_urls),
                "failed_url_count": len(failed_urls),
                "no_section_url_count": len(no_section_urls),
                "missing_urls": missing_urls,
                "failed_urls": failed_urls,
                "no_section_urls": no_section_urls,
                "excluded_row_count": len(excluded_log),
                "excluded_reason_counts": exclusion_reason_counts,
                "row_count_by_url": row_count_by_url,
                "row_count_by_city": row_count_by_city,
                "duplicate_of_national_count": duplicate_of_national_count,
                "store_unique_count": store_unique_count,
                "ocr_attempted": ocr_attempted,
                "ocr_success": ocr_success,
                "ocr_failed": ocr_failed,
                "ocr_skipped_prefilter": ocr_prefilter_skipped,
                "ocr_status_counts": ocr_status_counts,
                "text_extracted_count": total_text_extracted,
                "image_ocr_extracted_count": total_image_ocr_extracted,
                "image_ocr_failed_needs_review_count": total_image_ocr_failed_nr,
                "text_vs_ocr_source_counts": text_vs_ocr_source_counts,
                "ocr_log": ocr_log,
                "url_log": url_log,
                "excluded_rows": excluded_log,
            },
        }
        output_file.write_text(json.dumps(result, indent=2, default=str))
        logger.info(
            f"[v2|{mode}] Saved {len(all_rows)} rows ({len(national_rows)} national, "
            f"{len(store_rows)} store) to {output_file}"
        )
        return result

    except Exception as exc:  # noqa: BLE001
        logger.error(f"[v2] scrape_jiffy_v2 failed: {exc}", exc_info=True)
        return {
            "competitor": competitor_v2.get("competitor", "Jiffy Lube"),
            "config_version": "v2",
            "mode": mode,
            "error": str(exc),
            "promotions": [],
            "count": 0,
        }

