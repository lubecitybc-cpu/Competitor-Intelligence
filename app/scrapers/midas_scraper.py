"""Midas scraper - Text-based HTML extraction only."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import DATA_DIR, PROMO_KEYWORDS
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "midas_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_with_fallback(url: str) -> str:
    """Fetch HTML using Firecrawl (Markdown + HTML mode), fallback to ZenRows/ScraperAPI."""
    # For rebates page, prefer ZenRows with JS rendering as it may have dynamic content
    if "rebates" in url.lower():
        try:
            from app.config.constants import ZENROWS_API_KEY
            if ZENROWS_API_KEY:
                import requests
                zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={url}&js_render=true&wait=3000&premium_proxy=true"
                response = requests.get(zenrows_url, timeout=45)
                response.raise_for_status()
                logger.info("Successfully fetched rebates page with ZenRows (JS rendering)")
                return response.text
        except Exception as e:
            logger.warning(f"ZenRows with JS failed, trying Firecrawl: {e}")
    
    # Try Firecrawl first - request both HTML and Markdown
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
            zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={url}&js_render=true&wait=2000"
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


def extract_promo_blocks(html: str, url: str = "") -> List[Dict]:
    """Extract promotional text blocks from HTML - Canada rebates only."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Remove script and style elements
    for script in soup(["script", "style", "noscript"]):
        script.decompose()
    
    promo_blocks = []
    seen_texts = set()
    
    # For rebates page, focus on actual rebate offer cards/sections only
    if "rebates" in url.lower() and "country=ca" in url.lower():
        logger.info("Extracting Canada rebates from rebates page...")
        
        # Method 1: Search full page text for rebate patterns (more reliable for JS-rendered content)
        full_text = soup.get_text(separator="\n")
        lines = [line.strip() for line in full_text.split("\n") if line.strip()]
        
        rebate_blocks = []
        current_block = []
        
        for line in lines:
            # Check if line contains rebate patterns
            if re.search(r'Get.*?\$\d+.*?Back|\$\d+.*?Back.*?Purchase|Get.*?up.*?to.*?\$\d+.*?Back|Bridgestone|Firestone|Michelin.*?Rebate', line, re.IGNORECASE):
                current_block.append(line)
            elif current_block:
                # Check if we have a valid rebate block
                block_text = " ".join(current_block)
                if re.search(r'\$\d+', block_text) and len(block_text) > 50:
                    rebate_blocks.append(block_text)
                current_block = []
        
        # Also check for standalone rebate lines
        for line in lines:
            if re.search(r'Get.*?up.*?to.*?\$\d+.*?Back.*?Purchase.*?4.*?Select', line, re.IGNORECASE):
                if 50 < len(line) < 500:
                    rebate_blocks.append(line)
        
        for block_text in rebate_blocks:
            # Verify it's Canada-relevant (exclude USA-only)
            mentions_usa = bool(re.search(r'\bUSA\b(?!.*Canada)', block_text, re.IGNORECASE))
            if not mentions_usa:
                text_hash = hash(block_text[:400])
                if text_hash not in seen_texts:
                    seen_texts.add(text_hash)
                    promo_blocks.append({
                        "text": block_text,
                        "html": "",
                        "selector": "rebate-text-search"
                    })
                    logger.info(f"Found rebate via text search: {block_text[:80]}... ({len(block_text)} chars)")
        
        # Method 2: Look for text nodes containing rebate patterns
        all_text_nodes = soup.find_all(string=True)
        rebate_text_nodes = []
        
        for text_node in all_text_nodes:
            text = text_node.strip()
            if re.search(r'Get.*?\$\d+.*?Back|\$\d+.*?Back.*?Purchase|Get.*?up.*?to.*?\$\d+.*?Back', text, re.IGNORECASE):
                if len(text) > 20 and len(text) < 500:  # Reasonable length for rebate text
                    rebate_text_nodes.append(text_node)
        
        # For each rebate text found, get its parent container
        for text_node in rebate_text_nodes:
            parent = text_node.find_parent()
            if parent:
                # Traverse up to find the rebate card/section container
                container = parent
                for _ in range(3):  # Check up to 3 levels up
                    if container:
                        classes = ' '.join(container.get('class', [])) if container.get('class') else ''
                        # Check if this looks like a rebate card
                        if any(word in classes.lower() for word in ['rebate', 'offer', 'card', 'promo', 'tile']):
                            break
                        container = container.find_parent()
                    else:
                        break
                
                if not container:
                    container = parent
                
                rebate_text = container.get_text(separator=" ", strip=True)
                
                # Verify it's a valid rebate: has amount, and (date or form link)
                has_rebate_amount = bool(re.search(r'\$\d+.*?Back|Get.*?\$\d+.*?Back|up.*?to.*?\$\d+.*?Back', rebate_text, re.IGNORECASE))
                has_date = bool(re.search(r'Offer\s+Valid|Postmark|Submission|Valid.*?\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', rebate_text, re.IGNORECASE))
                has_form = bool(re.search(r'Rebate\s+Form|View\s+Rebate', rebate_text, re.IGNORECASE))
                mentions_usa = bool(re.search(r'\bUSA\b(?!.*Canada)|\bUnited States\b(?!.*Canada)', rebate_text, re.IGNORECASE))
                
                if has_rebate_amount and (has_date or has_form) and not mentions_usa:
                    if 80 < len(rebate_text) < 2500:
                        text_hash = hash(rebate_text[:400])
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": rebate_text,
                                "html": str(container)[:2000],
                                "selector": "rebate-text-pattern"
                            })
                            logger.info(f"Found rebate offer: {rebate_text[:80]}... ({len(rebate_text)} chars)")
        
        # Method 2: Look for brand names in headings
        brand_names = ['Bridgestone', 'Firestone', 'Michelin', 'Goodyear']
        for brand in brand_names:
            headings = soup.find_all(['h2', 'h3', 'h4'], string=re.compile(brand, re.IGNORECASE))
            for heading in headings:
                container = heading.find_parent(['section', 'div', 'article']) or heading.find_parent()
                if container:
                    text = container.get_text(separator=" ", strip=True)
                    if re.search(r'\$\d+.*?Back|Get.*?\$\d+', text, re.IGNORECASE):
                        text_hash = hash(text[:400])
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": text,
                                "html": str(container)[:2000],
                                "selector": f"brand-{brand}"
                            })
                            logger.info(f"Found rebate via brand {brand}: {text[:80]}... ({len(text)} chars)")
        
        if promo_blocks:
            logger.info(f"Extracted {len(promo_blocks)} Canada rebate offers")
            return promo_blocks
        else:
            logger.warning("No rebate offers found on rebates page - may need JavaScript rendering")
    
    # For archive page, use more targeted extraction
    elif "archive" in url.lower():
        logger.info("Extracting promotions from archive page...")
        
        # Method 1: Look for service cards/sections with detailed content
        # Find common service section patterns
        service_keywords = ['oil change', 'brake', 'tire', 'alignment', 'battery', 'exhaust', 'transmission', 'cooling']
        
        # Look for headings with service names
        promo_headings = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'], string=re.compile(
            r'oil\s+change|tire|brake|buy.*get|free|special|offer|\$\d+|alignment|battery|exhaust|transmission|cooling', re.IGNORECASE
        ))
        
        for heading in promo_headings:
            heading_text = heading.get_text(strip=True)
            
            # Get parent container - try to find the full service section
            container = heading.find_parent(['section', 'div', 'article', 'li'])
            if not container:
                container = heading.find_parent()
            
            # Try to get more context by expanding the container
            if container:
                # Look for sibling paragraphs or description divs
                next_siblings = container.find_next_siblings(['p', 'div'], limit=5)
                for sibling in next_siblings:
                    sibling_classes = ' '.join(sibling.get('class', [])) if sibling.get('class') else ''
                    if any(word in sibling_classes.lower() for word in ['description', 'detail', 'content', 'text']):
                        container = container.find_parent(['div', 'section', 'article'])
                        if container:
                            container.append(sibling)
                        break
                
                text = container.get_text(separator=" ", strip=True)
                
                # Must have price indicator or promo keyword
                has_price = bool(re.search(r'\$\d+', text))
                has_promo = bool(re.search(r'oil\s+change|tire|brake|free|special|offer|buy.*get|includes|warranty', text, re.IGNORECASE))
                
                # Filter out navigation/menu items (too short or contains nav keywords)
                is_nav = bool(re.search(r'home|about|contact|menu|navigation', text[:100], re.IGNORECASE)) and len(text) < 200
                
                if not is_nav and (has_price or has_promo) and 80 < len(text) < 5000:
                    # Clean up text (remove excessive whitespace)
                    text = re.sub(r'\s+', ' ', text).strip()
                    text_hash = hash(text[:400])
                    if text_hash not in seen_texts:
                        seen_texts.add(text_hash)
                        promo_blocks.append({
                            "text": text,
                            "html": str(container)[:3000],
                            "selector": f"archive-{heading.name}"
                        })
                        logger.info(f"Found archive promo: {heading_text[:50]}... ({len(text)} chars)")
        
        # Method 2: Search for service description blocks by class/id patterns
        service_patterns = [
            {'tag': 'div', 'class': re.compile(r'service|promo|offer|deal', re.IGNORECASE)},
            {'tag': 'article', 'class': re.compile(r'service|promo', re.IGNORECASE)},
            {'tag': 'section', 'class': re.compile(r'service|promo', re.IGNORECASE)},
        ]
        
        for pattern in service_patterns:
            elements = soup.find_all(pattern['tag'], class_=pattern['class'])
            for elem in elements:
                text = elem.get_text(separator=" ", strip=True)
                text = re.sub(r'\s+', ' ', text).strip()
                
                if 100 < len(text) < 3000:
                    has_service = any(keyword in text.lower() for keyword in service_keywords)
                    if has_service:
                        text_hash = hash(text[:400])
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": text,
                                "html": str(elem)[:3000],
                                "selector": f"archive-service-block"
                            })
                            logger.info(f"Found archive service block: {text[:60]}... ({len(text)} chars)")
    
    # Remove very similar duplicates using fuzzy matching
    if len(promo_blocks) > 1:
        unique_blocks = []
        for block in promo_blocks:
            is_duplicate = False
            for existing in unique_blocks:
                similarity = fuzz.ratio(block["text"][:300], existing["text"][:300])
                if similarity > 90:  # Very high threshold for duplicates
                    is_duplicate = True
                    logger.debug(f"Skipping duplicate block ({similarity}% similar)")
                    break
            if not is_duplicate:
                unique_blocks.append(block)
        
        logger.info(f"Extracted {len(unique_blocks)} unique promo blocks (removed {len(promo_blocks) - len(unique_blocks)} duplicates)")
        return unique_blocks
    
    logger.info(f"Extracted {len(promo_blocks)} promo blocks")
    return promo_blocks


def extract_discount_value(text: str) -> Optional[str]:
    """Extract discount value from text."""
    text_lower = text.lower()
    
    # Try dollar amount first (rebate amounts)
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)\s+Back|\$(\d+(?:\.\d+)?)\s+back|Get\s+(?:up\s+to\s+)?\$(\d+(?:\.\d+)?)\s+Back', text, re.IGNORECASE)
    if dollar_match:
        amount = dollar_match.group(1) or dollar_match.group(2) or dollar_match.group(3)
        return f"${amount} back"
    
    # Try regular dollar amount
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
    code_patterns = [
        r'(?:code|coupon|promo)[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
        r'code[:\s]*([A-Z0-9]{4,15})',
    ]
    
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    
    return None


def extract_expiry_date(text: str) -> Optional[str]:
    """Extract expiry date from text."""
    date_patterns = [
        r'Postmark.*?Date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'Submission.*?Date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'Offer\s+Valid[:\s]+([^–-]+?)(?:\s+–\s+|\s+-\s+)(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(?:expires?|valid until|until|ends?)[:\s]+([A-Za-z]+\s+\d{1,2}[,\s]+\d{4})',
        r'(?:expires?|valid until|until|ends?)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(?:expires?|valid)[:\s]*(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1) if match.lastindex == 1 else f"{match.group(1)} - {match.group(2)}"
    
    return None


def map_service_category(text: str) -> str:
    """Map text to service category."""
    text_lower = text.lower()
    
    service_keywords = {
        "tires": ["tire", "tires", "wheel", "wheels", "alignment", "bridgestone", "firestone", "michelin", "goodyear"],
        "oil change": ["oil", "lube", "oil change"],
        "brakes": ["brake", "brakes", "brake pad", "brake service"],
        "battery": ["battery", "batteries"],
        "exhaust": ["exhaust", "muffler"],
        "transmission": ["transmission", "trans"],
        "cooling": ["coolant", "radiator", "cooling system"],
        "filters": ["filter", "filters", "air filter"],
    }
    
    for category, keywords in service_keywords.items():
        if any(keyword in text_lower for keyword in keywords):
            return category
    
    return "other"


def are_promos_duplicate(promo1: Dict, promo2: Dict) -> bool:
    """Check if two promotions are duplicates."""
    title1 = promo1.get("promotion_title", "").lower()
    title2 = promo2.get("promotion_title", "").lower()
    discount1 = promo1.get("discount_value", "")
    discount2 = promo2.get("discount_value", "")
    
    # Same discount and high title similarity
    if discount1 and discount2 and discount1 == discount2:
        similarity = fuzz.ratio(title1[:100], title2[:100])
        if similarity > 85:
            return True
    
    # Very high title similarity regardless of discount
    similarity = fuzz.ratio(title1[:200], title2[:200])
    if similarity > 90:
        return True
    
    return False


def process_midas_promotions(competitor: Dict) -> List[Dict]:
    """Process Midas promotions using text-based HTML extraction."""
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
        
        # Step 2: Extract promo blocks (pass URL for context)
        promo_blocks = extract_promo_blocks(html, promo_url)
        
        if not promo_blocks:
            logger.warning(f"No promo blocks found on {promo_url}")
            continue
        
        # Step 3: Process each promo block with LLM
        for block in promo_blocks:
            text = block["text"]
            
            # Skip if too short
            if len(text) < 50:
                continue
            
            logger.info(f"Processing promo block: {len(text)} chars")
            
            try:
                # Send to LLM for cleaning and structuring
                context = f"Midas promotion from {promo_url}. Block selector: {block.get('selector', 'unknown')}"
                cleaned_data = clean_promo_text_with_llm(text, context)
                
                # Handle case where LLM returns a list instead of dict
                if isinstance(cleaned_data, list):
                    if len(cleaned_data) > 0 and isinstance(cleaned_data[0], dict):
                        cleaned_data = cleaned_data[0]
                    else:
                        cleaned_data = None
                
                # Extract basic details from text
                discount_value = extract_discount_value(text)
                coupon_code = extract_coupon_code(text)
                expiry_date = extract_expiry_date(text)
                service_category = map_service_category(text)
                
                # Build promotion using LLM cleaned data if available
                if cleaned_data and isinstance(cleaned_data, dict):
                    promotion_title = cleaned_data.get("service_name") or (cleaned_data.get("promo_description") or "").split("\n")[0].strip()[:100] if cleaned_data.get("promo_description") else None
                    if not promotion_title:
                        lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 5]
                        promotion_title = lines[0][:100] if lines else "Midas Promotion"
                    
                    promo_description = cleaned_data.get("promo_description") or text[:500]
                    offer_details = cleaned_data.get("promo_description") or text[:1000]
                    discount_value = cleaned_data.get("discount_value") or discount_value
                    coupon_code = cleaned_data.get("coupon_code") or coupon_code
                    expiry_date = cleaned_data.get("expiry_date") or expiry_date
                    
                    if cleaned_data.get("service_name"):
                        service_category = map_service_category(cleaned_data.get("service_name"))
                else:
                    # Fallback to direct text extraction
                    lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 5]
                    promotion_title = lines[0][:100] if lines else "Midas Promotion"
                    promo_description = text[:500]
                    offer_details = text[:1000]
                
                # Calculate confidence score
                confidence = 0.7
                if cleaned_data:
                    confidence = 0.9
                if discount_value:
                    confidence += 0.05
                if coupon_code:
                    confidence += 0.05
                if expiry_date:
                    confidence += 0.05
                confidence = min(confidence, 1.0)
                
                promo = {
                    "website": competitor.get("domain", ""),
                    "page_url": promo_url,
                    "business_name": competitor.get("name", ""),
                    "google_reviews": None,
                    "service_name": cleaned_data.get("service_name", service_category) if (cleaned_data and isinstance(cleaned_data, dict)) else service_category,
                    "promo_description": promo_description,
                    "category": service_category,
                    "contact": competitor.get("address", ""),
                    "location": competitor.get("address", ""),
                    "offer_details": offer_details,
                    "ad_title": promotion_title,
                    "ad_text": text[:500],
                    "new_or_updated": "new",
                    "date_scraped": datetime.now().isoformat(),
                    "discount_value": discount_value,
                    "coupon_code": coupon_code,
                    "expiry_date": expiry_date,
                    "promotion_title": promotion_title,
                    "image_url": None,
                    "service_category": service_category,
                    "source": "midas_html",
                    "confidence": {
                        "overall": confidence,
                        "fields": {
                            "promotion_title": 0.8 if cleaned_data else 0.6,
                            "discount_value": 0.9 if discount_value else 0.0,
                            "coupon_code": 0.9 if coupon_code else 0.0,
                            "expiry_date": 0.8 if expiry_date else 0.0,
                            "service_category": 0.8
                        }
                    }
                }
                
                all_promos.append(promo)
                logger.info(f"✓ Added promo: {promotion_title[:50]} - {discount_value or 'N/A'}")
                
            except Exception as e:
                logger.error(f"Error processing promo block: {e}", exc_info=True)
                continue
    
    # Final deduplication pass
    logger.info(f"Found {len(all_promos)} promotions before deduplication")
    deduplicated = []
    for promo in all_promos:
        is_duplicate = False
        for existing in deduplicated:
            if are_promos_duplicate(promo, existing):
                logger.info(f"Removed duplicate: {promo.get('promotion_title')[:50]} (similar to {existing.get('promotion_title')[:50]})")
                is_duplicate = True
                break
        if not is_duplicate:
            deduplicated.append(promo)
    
    logger.info(f"Total unique promotions found: {len(deduplicated)}")
    return deduplicated


def scrape_midas(competitor: Dict) -> Dict:
    """Main entry point for Midas scraper."""
    try:
        promos = process_midas_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'midas').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Midas: {e}", exc_info=True)
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
    
    # Find Midas
    midas = next((c for c in competitors if "midas" in c.get("name", "").lower()), None)
    
    if not midas:
        logger.error("Midas not found in competitor list")
        sys.exit(1)
    
    result = scrape_midas(midas)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")


# ---------------------------------------------------------------------------
# v2 pipeline (Phase 5): per-store offers pages for Calgary / Edmonton /
# Grande Prairie. Text-only. Does not affect the legacy `scrape_midas` above.
# ---------------------------------------------------------------------------

from app.utils.service_classifier import classify_service  # noqa: E402
from app.scrapers.jiffy_scraper import (  # noqa: E402
    _v2_extract_discount,
    _v2_extract_coupon_code,
    _is_terms_only,
    _summarize_promo_description,
    _normalize_discount,
    _signature_meaningful_tokens,
    _confidence_from_promo,
)

_MIDAS_OFFER_INDICATORS = re.compile(
    r"(?:\bcoupons?\b|\brebates?\b|\bsave\b|\boff\b|\bdiscount\b|\bfree\b|"
    r"\bfinancing\b|\bpromotion\b|\bspecial\b|\bdeal\b|\boffer\b|\bpromo\b|"
    r"\blimited[- ]time\b|\bexpires?\b|\bvalid\s+through\b|"
    r"\$\s*\d|\d+\s*%\s*off\b|\bget\s+\$?\d|\bup\s+to\s+\$?\d)",
    re.IGNORECASE,
)

_MIDAS_NAV_BLOCKLIST = re.compile(
    r"^(home|about|services|offers|coupons|locations|contact|sitemap|careers|"
    r"faq|privacy|terms|menu|tire (?:sales|services?)|brake (?:repair|services?)|"
    r"battery (?:replacement|services?)|oil change|view all|see all|learn more|"
    r"sign up|subscribe|find a store|store hours|directions|book appointment|"
    r"schedule (?:service|appointment)|skip to content|midas credit card)$",
    re.IGNORECASE,
)

_MIDAS_HEADING_NOISE = re.compile(
    r"^(?:midas\s+coupons?\s*[&and]+\s*offers|quality\s+parts\s+and\s+service|"
    r"coupons?\s*[&and]+\s*offers\s+near\s+you|deals\s+to\s+match|"
    r"request\s+appointment|print\s+offer|email\s+offer|required\s+fields)",
    re.IGNORECASE,
)

_MIDAS_TITLE_NOISE_PATTERNS = [
    re.compile(r"^get coupon$", re.IGNORECASE),
    re.compile(r"^view (?:offer|coupon|details)$", re.IGNORECASE),
    re.compile(r"^view all (?:offers|coupons)$", re.IGNORECASE),
    re.compile(r"^print coupon$", re.IGNORECASE),
]


def _midas_extract_offer_sections(html: str) -> List[Dict]:
    """Pull candidate offer sections from a Midas /offers page.

    We scan the obvious card/section/article/li containers, plus any element
    whose class hints at a coupon/offer/promo/rebate card. A section becomes
    a candidate only if its text shows a real offer indicator AND it isn't
    a navigation/menu label.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    candidates: List = []
    class_hint_re = re.compile(r"(coupon|offer|promo|deal|rebate|special|discount)", re.IGNORECASE)

    for el in soup.find_all(["article", "section", "li", "div"]):
        classes = " ".join(el.get("class", []) or [])
        ids = el.get("id", "") or ""
        if class_hint_re.search(classes) or class_hint_re.search(ids):
            candidates.append(el)

    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if _MIDAS_OFFER_INDICATORS.search(h.get_text(" ", strip=True) or ""):
            parent = h.find_parent(["article", "section", "div"]) or h
            candidates.append(parent)

    # Keep only "leaf" offer cards: drop any candidate whose text fully
    # contains another candidate's text (those are outer wrappers).
    if candidates:
        cand_text: List[str] = [c.get_text(" ", strip=True) for c in candidates]
        cand_len = [len(t) for t in cand_text]
        kept: List = []
        for i, el in enumerate(candidates):
            ti = cand_text[i]
            is_wrapper = False
            for j, tj in enumerate(cand_text):
                if i == j:
                    continue
                if cand_len[j] < cand_len[i] and tj and tj in ti and cand_len[j] >= 40:
                    is_wrapper = True
                    break
            if not is_wrapper:
                kept.append(el)
        candidates = kept

    sections: List[Dict] = []
    seen_hashes: set = set()
    for el in candidates:
        text = el.get_text("\n", strip=True)
        if not text or len(text) < 25 or len(text) > 4000:
            continue
        if not _MIDAS_OFFER_INDICATORS.search(text):
            continue
        first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
        if not first_line or _MIDAS_NAV_BLOCKLIST.match(first_line):
            continue
        if _MIDAS_HEADING_NOISE.match(first_line):
            continue
        h = hash(text[:500])
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        sections.append({"text": text, "html": str(el)[:2000]})
    return sections


def _midas_has_real_offer(text: str, discount: Optional[str], code: Optional[str]) -> bool:
    """Real offer ⇒ has a discount, a code, OR an explicit offer marker.

    Generic service-menu text like 'Oil Change' alone must NOT pass.
    """
    if discount or code:
        return True
    return bool(re.search(
        r"(?:\bcoupons?\b|\brebates?\b|\bsave\b|\bfree\b|\bfinancing\b|"
        r"\blimited[- ]time\b|\bexpires?\b|\bvalid\s+through\b|"
        r"\$\s*\d+|\d+\s*%\s*off\b|\bget\s+\$?\d|\bup\s+to\s+\$?\d|"
        r"\bbonus\s+air\s*miles?\b)",
        text or "",
        re.IGNORECASE,
    ))


def _midas_clean_title(raw: str) -> str:
    line = (raw or "").strip().splitlines()[0] if raw else ""
    line = line.strip(" •-—|").strip()
    for pat in _MIDAS_TITLE_NOISE_PATTERNS:
        if pat.match(line):
            return ""
    return line[:120]


def _midas_signature(*, title: str, discount: Optional[str], code: Optional[str]) -> str:
    d = _normalize_discount(discount) or ""
    c = (code or "").strip().upper()
    if d and c:
        return f"d={d}|c={c}"
    title_clean = re.sub(r"\d+\s*(ave|st|street|road|rd|blvd|trail|way|cres|drive|dr)\b",
                        " ", (title or "").lower())
    tokens = _signature_meaningful_tokens(title_clean)
    return f"d={d}|c={c}|t={tokens}"


def _midas_build_row(
    *,
    competitor: str,
    page_url: str,
    city: str,
    store_name: str,
    promotion_title: str,
    offer_details: str,
    ad_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    std_service: str,
) -> Dict:
    row = {
        # Existing sheet columns (first)
        "website": "midas.com",
        "page_url": page_url,
        "business_name": competitor,
        "google_reviews": "",
        "service_name": std_service,
        "promo_description": offer_details,
        "category": std_service,
        "contact": store_name,
        "location": store_name,
        "offer_details": offer_details,
        "ad_title": promotion_title,
        "ad_text": ad_text,
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),

        # QA metadata
        "city": city,
        "store_name": store_name,
        "source_scope": "store",
        "extraction_method": "text",
        "confidence": None,
        "needs_review": False,
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": promotion_title,
        "normalized_title": (promotion_title or "").lower().strip(),
        "applicable_cities": [city],
        "duplicate_of_national": False,
        "duplicate_group_id": None,  # filled in by orchestrator
    }
    row["confidence"] = _confidence_from_promo(row)
    if row["confidence"] == "low" and not discount and not code:
        row["needs_review"] = True
    return row


def _midas_extract_code_cards(html: str) -> List[Dict]:
    """Find every Midas coupon block on the page and return the structured
    card context (title, discount, expiry, code) so we can match codes back
    to the leaf offer rows extracted upstream.

    Anchors on Midas's BEM-style `.offers__coupon` container which wraps a
    single coupon's title/subtitle/expires/promo-code spans. This avoids
    accidentally swallowing the full page (privacy text, form, etc.).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    cards: List[Dict] = []
    seen_codes: set = set()

    # Find each `.offers__coupon` container (including modifier variants
    # like `--is-odd`, `--is-last`), but NOT its children (-title / -subtitle /
    # -promo-code / etc.) and NOT the wrapping `.offers__coupons-list`.
    def _is_coupon_root(tag) -> bool:
        cls = tag.get("class") or []
        if not cls:
            return False
        for c in cls:
            if c == "offers__offer-container" or c == "offers__coupon" \
               or c.startswith("offers__coupon--"):
                return True
        return False

    for coupon in soup.find_all(_is_coupon_root):

        code_span = coupon.find(class_="offers__coupon-promo-code")
        if not code_span:
            continue
        code_text = code_span.get_text(" ", strip=True)
        m = re.search(r"Promo\s*Code\s*[:\-]?\s*([A-Z0-9]{4,12})", code_text, re.IGNORECASE)
        if not m:
            continue
        code = m.group(1).upper()

        title_el = coupon.find(class_="offers__coupon-title")
        subtitle_el = coupon.find(class_="offers__coupon-subtitle")
        expires_el = coupon.find(class_="offers__coupon-expires")

        title = (title_el.get_text(" ", strip=True) if title_el else "").strip()
        subtitle = (subtitle_el.get_text(" ", strip=True) if subtitle_el else "").strip()
        expires_text = (expires_el.get_text(" ", strip=True) if expires_el else "").strip()

        # Build a compact card text: title + subtitle + expiry only.
        card_text_parts = [p for p in [title, subtitle, expires_text] if p]
        card_text = " | ".join(card_text_parts)

        discount = _v2_extract_discount(title + " " + subtitle)
        # Detect "Free" as a discount when no $/% present.
        if not discount and re.search(r"\bfree\b", (title + " " + subtitle), re.IGNORECASE):
            discount = "free"

        expiry_match = re.search(
            r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            expires_text,
        )
        expiry = expiry_match.group(1) if expiry_match else None

        key = (code, _normalize_discount(discount), expiry or "")
        if key in seen_codes:
            continue
        seen_codes.add(key)
        cards.append({
            "code": code,
            "card_text": card_text,
            "title": title,
            "subtitle": subtitle,
            "discount": discount,
            "expiry": expiry,
            "title_snippet": title or subtitle[:80],
        })
    return cards


def _title_tokens(text: str) -> set:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    stop = {"the", "a", "an", "and", "or", "of", "off", "on", "to", "for",
            "with", "any", "select", "each", "per", "your", "our", "in",
            "at", "by", "from", "is", "more", "than"}
    tokens = {t for t in text.split() if len(t) > 2 and t not in stop}
    return tokens


def _enrich_rows_with_codes(rows: List[Dict], cards: List[Dict]) -> Dict[str, int]:
    """Match rows to coupon-code cards using stable fields. Mutates `rows` and
    returns per-pass counters.
    """
    counters = {"attempted": 0, "recovered": 0, "missing": 0, "ambiguous": 0}
    if not rows:
        return counters

    for row in rows:
        if row.get("coupon_code"):
            continue  # already have a code from primary extraction
        counters["attempted"] += 1

        row_disc = _normalize_discount(row.get("discount_value"))
        row_exp = (row.get("expiry_date") or "").strip()
        row_tokens = _title_tokens(row.get("promotion_title", ""))

        matches: List[Dict] = []
        for card in cards:
            card_disc = _normalize_discount(card.get("discount"))
            card_exp = (card.get("expiry") or "").strip()
            card_tokens = _title_tokens(card.get("title_snippet", "") + " " + card["card_text"])

            disc_ok = (row_disc and card_disc and row_disc == card_disc) or (not row_disc and not card_disc)
            exp_ok = (row_exp and card_exp and row_exp == card_exp) or (not row_exp or not card_exp)
            token_overlap = len(row_tokens & card_tokens)
            token_ok = token_overlap >= max(1, min(2, len(row_tokens) // 3))

            score = 0
            if row_disc and card_disc and row_disc == card_disc:
                score += 3
            if row_exp and card_exp and row_exp == card_exp:
                score += 2
            score += token_overlap

            if disc_ok and (token_ok or exp_ok) and score >= 2:
                matches.append({"card": card, "score": score})

        if not matches:
            counters["missing"] += 1
            continue

        matches.sort(key=lambda m: m["score"], reverse=True)
        top = matches[0]
        # ambiguous if top two have the same score and propose different codes
        if (
            len(matches) > 1
            and matches[1]["score"] == top["score"]
            and matches[1]["card"]["code"] != top["card"]["code"]
        ):
            counters["ambiguous"] += 1
            row["needs_review"] = True
            row["needs_review_reason"] = "coupon_code_ambiguous"
            continue

        row["coupon_code"] = top["card"]["code"]
        row["coupon_code_source"] = "enrichment_pass"
        counters["recovered"] += 1

    return counters


def _scrape_midas_store(
    *,
    url: str,
    city: str,
    store_name: str,
    competitor: str,
) -> Dict:
    logger.info(f"[midas-v2] Fetching {city} | {store_name} | {url}")
    html = fetch_with_fallback(url)
    if not html:
        return {"url": url, "status": "fetch_failed", "raw_promos": [], "enrichment": {"attempted": 0, "recovered": 0, "missing": 0, "ambiguous": 0}}

    sections = _midas_extract_offer_sections(html)
    raw_promos: List[Dict] = []
    for sec in sections:
        text = sec["text"]
        discount = _v2_extract_discount(text)
        code = _v2_extract_coupon_code(text)

        first_line = _midas_clean_title(text)
        if not first_line:
            first_line = "Midas Offer"

        if not _midas_has_real_offer(text, discount, code):
            continue
        if _is_terms_only(first_line, text, discount, code):
            continue

        expiry_match = re.search(
            r"(?:expires?|valid\s+through|offer\s+valid)\s*[:\-]?\s*"
            r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            text, re.IGNORECASE,
        )
        expiry = expiry_match.group(1) if expiry_match else None

        combined_for_classify = first_line + " " + text[:400]
        if re.search(r"\boil\s+change\b", combined_for_classify, re.IGNORECASE):
            std_service = "Oil Change"
        elif re.search(r"\bbrake\s+pads?\b|\bbrake\s+rotors?\b|\bbrake\s+service\b",
                       combined_for_classify, re.IGNORECASE):
            std_service = "Brake"
        elif re.search(r"\btires?\b", combined_for_classify, re.IGNORECASE) and \
             re.search(r"\bbuy\s+\d|\bfree\s+tire|\boff\s+\d|\balignment\b|select\s+tires?",
                       combined_for_classify, re.IGNORECASE):
            std_service = "Tire Sales"
        else:
            std_service = classify_service(combined_for_classify)
        offer_details = text[:1000]

        row = _midas_build_row(
            competitor=competitor,
            page_url=url,
            city=city,
            store_name=store_name,
            promotion_title=first_line,
            offer_details=offer_details,
            ad_text=text[:500],
            discount=discount,
            code=code,
            expiry=expiry,
            std_service=std_service,
        )
        row["promo_description"] = _summarize_promo_description(
            promotion_title=first_line,
            offer_details=offer_details,
            discount=discount,
            code=code,
            std_service=std_service,
            ad_text=text,
            brand="Midas",
        )
        raw_promos.append(row)

    # Per-page dedupe: nested DOM containers can yield the same offer
    # multiple times (the outer card + an inner block + a title heading).
    # Group rows by stable signature; for each group keep the most informative
    # row (prefer discount+code+expiry, then longest offer_details).
    groups: Dict[str, List[int]] = {}
    for idx, r in enumerate(raw_promos):
        sig = _midas_signature(
            title=r.get("promotion_title", ""),
            discount=r.get("discount_value"),
            code=r.get("coupon_code"),
        )
        groups.setdefault(sig, []).append(idx)

    keep_indices: List[int] = []
    for sig, idxs in groups.items():
        if len(idxs) == 1:
            keep_indices.append(idxs[0])
            continue

        def _score(i: int) -> tuple:
            r = raw_promos[i]
            return (
                1 if r.get("discount_value") else 0,
                1 if r.get("coupon_code") else 0,
                1 if r.get("expiry_date") else 0,
                len(r.get("offer_details") or ""),
            )

        winner = max(idxs, key=_score)
        keep_indices.append(winner)
    keep_indices.sort()
    deduped = [raw_promos[i] for i in keep_indices]

    # ---- Coupon-code enrichment pass (does NOT add or drop rows) ----------
    code_cards = _midas_extract_code_cards(html)
    enrichment = _enrich_rows_with_codes(deduped, code_cards)
    logger.info(
        f"[midas-v2] {url} → codes attempted={enrichment['attempted']}, "
        f"recovered={enrichment['recovered']}, missing={enrichment['missing']}, "
        f"ambiguous={enrichment['ambiguous']} (cards on page: {len(code_cards)})"
    )

    return {
        "url": url,
        "status": "ok",
        "raw_promos": deduped,
        "enrichment": enrichment,
        "code_cards_on_page": len(code_cards),
    }


def scrape_midas_v2(competitor_v2: Dict, *, mode: str = "qa_expanded") -> Dict:
    """Phase 5 entry point for Midas. Per-store text-based scraping.

    QA-expanded mode keeps every per-URL row, even when the same coupon
    appears across multiple stores; matching rows share a stable
    `duplicate_group_id`. final_deduped mode collapses identical
    signatures within a city.
    """
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError("mode must be qa_expanded or final_deduped")

    competitor_name = competitor_v2.get("competitor", "Midas")
    store_links = competitor_v2.get("store_links", {}) or {}
    expected_urls: List[str] = []
    url_log: List[Dict] = []
    rows: List[Dict] = []
    seen_in_city: set = set()
    enrichment_total = {"attempted": 0, "recovered": 0, "missing": 0, "ambiguous": 0}

    for city, links in store_links.items():
        for link in links:
            url = link["url"]
            store_name = link["store_name"]
            expected_urls.append(url)
            res = _scrape_midas_store(
                url=url, city=city, store_name=store_name, competitor=competitor_name,
            )
            added = 0
            dropped = 0
            for r in res["raw_promos"]:
                sig = _midas_signature(
                    title=r.get("promotion_title", ""),
                    discount=r.get("discount_value"),
                    code=r.get("coupon_code"),
                )
                r["duplicate_group_id"] = sig
                if mode == "final_deduped":
                    key = (city, sig)
                    if key in seen_in_city:
                        dropped += 1
                        continue
                    seen_in_city.add(key)
                rows.append(r)
                added += 1
            enr = res.get("enrichment") or {}
            for k in enrichment_total:
                enrichment_total[k] += enr.get(k, 0)
            url_log.append({
                "url": url, "city": city, "store_name": store_name,
                "scope": "store",
                "status": res["status"],
                "raw_promo_count": len(res["raw_promos"]),
                "added_unique": added,
                "dropped_as_city_duplicate": dropped,
                "excluded_count": 0,
                "code_cards_on_page": res.get("code_cards_on_page", 0),
                "codes_attempted": enr.get("attempted", 0),
                "codes_recovered": enr.get("recovered", 0),
                "codes_missing": enr.get("missing", 0),
                "codes_ambiguous": enr.get("ambiguous", 0),
            })
            logger.info(f"[midas-v2] {city} | {store_name}: kept {added} rows")

    # Validation aggregates ---------------------------------------------------
    processed_urls = {e["url"] for e in url_log if e["status"] == "ok"}
    failed_urls = [e["url"] for e in url_log if e["status"] == "fetch_failed"]
    missing_urls = sorted(set(expected_urls) - {e["url"] for e in url_log})

    row_count_by_url: Dict[str, int] = {}
    row_count_by_city: Dict[str, int] = {}
    dup_groups: Dict[str, int] = {}
    for r in rows:
        u = r.get("page_url") or ""
        row_count_by_url[u] = row_count_by_url.get(u, 0) + 1
        c = r.get("city") or ""
        row_count_by_city[c] = row_count_by_city.get(c, 0) + 1
        gid = r.get("duplicate_group_id") or ""
        dup_groups[gid] = dup_groups.get(gid, 0) + 1

    unique_promo_descriptions = len({(r.get("promo_description") or "").strip().lower() for r in rows})

    base = competitor_name.lower().replace(" ", "_").replace(".", "")
    output_file = PROMOTIONS_DIR / f"{base}_v2.json"
    result = {
        "competitor": competitor_name,
        "scraped_at": datetime.now().isoformat(),
        "config_version": "v2",
        "mode": mode,
        "promotions": rows,
        "count": len(rows),
        "needs_review_count": sum(1 for r in rows if r.get("needs_review")),
        "by_city": row_count_by_city,
        "validation": {
            "expected_url_count": len(expected_urls),
            "processed_url_count": len(processed_urls),
            "failed_url_count": len(failed_urls),
            "failed_urls": failed_urls,
            "missing_urls": missing_urls,
            "row_count_by_url": row_count_by_url,
            "row_count_by_city": row_count_by_city,
            "duplicate_group_counts": dup_groups,
            "duplicate_group_total": len(dup_groups),
            "needs_review_count": sum(1 for r in rows if r.get("needs_review")),
            "unique_promo_descriptions": unique_promo_descriptions,
            "coupon_code_recovery_attempted": enrichment_total["attempted"],
            "coupon_code_recovered_count": enrichment_total["recovered"],
            "coupon_code_missing_count": enrichment_total["missing"],
            "coupon_code_ambiguous_count": enrichment_total["ambiguous"],
            "coupon_code_coverage": (
                sum(1 for r in rows if r.get("coupon_code")) / max(1, len(rows))
            ),
            "url_log": url_log,
        },
    }
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[midas-v2|{mode}] Saved {len(rows)} rows to {output_file}")
    return result
