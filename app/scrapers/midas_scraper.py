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
        
        # Look for featured monthly offers or promo sections
        # Find headings with promo content
        promo_headings = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'], string=re.compile(
            r'oil\s+change|tire|brake|buy.*get|free|special|offer|\$\d+', re.IGNORECASE
        ))
        
        for heading in promo_headings:
            heading_text = heading.get_text(strip=True)
            
            # Get parent container
            container = heading.find_parent(['section', 'div', 'article'])
            if not container:
                container = heading.find_parent()
            
            if container:
                text = container.get_text(separator=" ", strip=True)
                
                # Must have price indicator or promo keyword
                has_price = bool(re.search(r'\$\d+', text))
                has_promo = bool(re.search(r'oil\s+change|tire|brake|free|special|offer|buy.*get', text, re.IGNORECASE))
                
                if (has_price or has_promo) and 50 < len(text) < 3000:
                    text_hash = hash(text[:300])
                    if text_hash not in seen_texts:
                        seen_texts.add(text_hash)
                        promo_blocks.append({
                            "text": text,
                            "html": str(container)[:2000],
                            "selector": f"archive-{heading.name}"
                        })
                        logger.info(f"Found archive promo: {heading_text[:50]}... ({len(text)} chars)")
    
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
