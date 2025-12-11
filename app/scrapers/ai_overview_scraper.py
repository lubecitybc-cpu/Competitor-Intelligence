"""AI Overview scraper - Extract promotions from Google AI Overview using SerpAPI."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.config.constants import DATA_DIR, SERPAPI_KEY
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "ai_overview_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def build_search_query(business_name: str, location: str = "Edmonton") -> str:
    """Build search query for AI Overview."""
    # Extract just the city name from location (remove street address)
    # Format is usually: "Street Address, City, Province, Country"
    if "," in location:
        parts = [p.strip() for p in location.split(",")]
        # City is usually the second part (index 1)
        city = parts[1] if len(parts) > 1 else parts[0]
    else:
        city = location
    
    # Remove any remaining street address parts
    city_parts = city.split()
    # Filter out common address parts
    filtered_parts = []
    for part in city_parts:
        part_lower = part.lower()
        if part_lower not in ["nw", "ne", "sw", "se", "st", "ave", "blvd", "street", "road", "rd"] and not part.isdigit():
            filtered_parts.append(part)
    
    city = " ".join(filtered_parts) if filtered_parts else city
    
    # Fallback to Edmonton if city is unclear
    if not city or len(city) < 3 or city.lower() in ["nw", "ne", "sw", "se"]:
        city = "Edmonton"
    
    query = f"{business_name} {city} promotions coupons discounts deals oil change tire rebates"
    return query.strip()


def fetch_ai_overview(query: str) -> Dict:
    """
    Fetch Google AI Overview using SerpAPI.
    
    Steps:
    1. Initial search with 'google' engine to get page_token
    2. Fetch AI Overview with 'google_ai_overview' engine using page_token
    3. Fallback: Extract from standard search response if no page_token
    """
    if not SERPAPI_KEY:
        logger.error("SERPAPI_KEY not found in environment variables")
        return {"error": "SERPAPI_KEY not configured"}
    
    try:
        import requests
        
        # Step 1: Initial search to get page_token
        initial_url = "https://serpapi.com/search.json"
        params = {
            "api_key": SERPAPI_KEY,
            "engine": "google",
            "q": query,
            "location": "Edmonton, Alberta, Canada",
            "hl": "en",
            "gl": "ca"
        }
        
        logger.info(f"Fetching initial search for: {query}")
        response = requests.get(initial_url, params=params, timeout=30)
        response.raise_for_status()
        initial_data = response.json()
        
        # Check for AI Overview in initial response
        ai_overview = initial_data.get("ai_overview")
        source_links = initial_data.get("source_links", [])
        
        # Step 2: Get page_token from ai_overview and fetch full AI Overview content
        page_token = None
        has_content = False
        
        if ai_overview and isinstance(ai_overview, dict):
            page_token = ai_overview.get("page_token")
            has_content = any(k in ai_overview for k in ["text", "snippet", "text_blocks", "blocks", "items"])
            logger.info(f"AI Overview found: keys={list(ai_overview.keys())}, has_page_token={bool(page_token)}, has_content={has_content}")
        else:
            logger.info("No AI Overview in initial response")
        
        # Always try to fetch if we have page_token
        if page_token:
            logger.info(f"Found page_token, fetching full AI Overview content...")
            ai_overview_url = "https://serpapi.com/search.json"
            ai_params = {
                "api_key": SERPAPI_KEY,
                "engine": "google_ai_overview",
                "page_token": page_token
            }
            
            try:
                ai_response = requests.get(ai_overview_url, params=ai_params, timeout=30)
                ai_response.raise_for_status()
                ai_data = ai_response.json()
                # Get the actual AI Overview content
                fetched_overview = ai_data.get("ai_overview")
                
                # Check if we got valid content
                if fetched_overview and isinstance(fetched_overview, dict):
                    # Check if it has actual content (not just page_token)
                    has_text_content = any(k in fetched_overview for k in ["text", "snippet", "text_blocks", "blocks", "items"])
                    if has_text_content:
                        ai_overview = fetched_overview
                        source_links = ai_data.get("source_links", []) or source_links or initial_data.get("source_links", [])
                        has_content = True
                        logger.info(f"Successfully fetched full AI Overview content with {len(fetched_overview.get('text_blocks', []))} text blocks")
                    else:
                        logger.warning(f"AI Overview fetch returned structure without content: {list(fetched_overview.keys())}")
                else:
                    logger.warning(f"AI Overview fetch returned invalid result: {type(fetched_overview)}")
            except Exception as e:
                logger.warning(f"Failed to fetch AI Overview with page_token: {e}", exc_info=True)
        
        # Fallback: Extract from standard search response if no AI Overview content yet
        if not has_content:
            logger.info("No AI Overview content found, extracting from organic results as fallback...")
            # Try answer_box, knowledge_graph, or organic_results
            answer_box = initial_data.get("answer_box", {})
            knowledge_graph = initial_data.get("knowledge_graph", {})
            organic_results = initial_data.get("organic_results", [])
            
            # Extract text from answer_box or knowledge_graph
            if answer_box.get("answer"):
                ai_overview = {"text": answer_box.get("answer"), "snippet": answer_box.get("answer")}
            elif knowledge_graph.get("description"):
                ai_overview = {"text": knowledge_graph.get("description"), "snippet": knowledge_graph.get("description")}
            elif organic_results:
                # Use first few organic results as context
                snippets = [r.get("snippet", "") for r in organic_results[:5] if r.get("snippet")]
                ai_overview = {"text": " ".join(snippets), "snippet": " ".join(snippets)}
            
            # Extract source links from organic results if not already found
            if not source_links:
                source_links = [result.get("link") for result in organic_results[:10] if result.get("link")]
        
        return {
            "ai_overview": ai_overview or {},
            "source_links": source_links[:10],  # Limit to 10 links
            "initial_data": initial_data
        }
    
    except Exception as e:
        logger.error(f"Error fetching AI Overview: {e}", exc_info=True)
        return {"error": str(e)}


def extract_text_from_ai_overview(ai_overview_data: Dict) -> str:
    """Extract text content from AI Overview data structure."""
    if not ai_overview_data:
        return ""
    
    # Try different possible structures
    text_parts = []
    
    # Structure 1: text_blocks (most common in SerpAPI)
    if ai_overview_data.get("text_blocks"):
        for block in ai_overview_data["text_blocks"]:
            if isinstance(block, dict):
                # Paragraph type
                if block.get("snippet"):
                    text_parts.append(block["snippet"])
                # List type
                if block.get("list"):
                    for list_item in block["list"]:
                        if isinstance(list_item, dict) and list_item.get("snippet"):
                            text_parts.append(list_item["snippet"])
            elif isinstance(block, str):
                text_parts.append(block)
    
    # Structure 2: Direct text/snippet
    if ai_overview_data.get("text"):
        text_parts.append(ai_overview_data["text"])
    if ai_overview_data.get("snippet"):
        text_parts.append(ai_overview_data["snippet"])
    
    # Structure 3: Blocks/paragraphs (alternative structure)
    if ai_overview_data.get("blocks"):
        for block in ai_overview_data["blocks"]:
            if isinstance(block, dict):
                if block.get("text"):
                    text_parts.append(block["text"])
                elif block.get("content"):
                    text_parts.append(block["content"])
                elif block.get("snippet"):
                    text_parts.append(block["snippet"])
            elif isinstance(block, str):
                text_parts.append(block)
    
    # Structure 4: Items/list
    if ai_overview_data.get("items"):
        for item in ai_overview_data["items"]:
            if isinstance(item, dict):
                if item.get("text"):
                    text_parts.append(item["text"])
                elif item.get("snippet"):
                    text_parts.append(item["snippet"])
            elif isinstance(item, str):
                text_parts.append(item)
    
    # Join all text parts
    full_text = "\n".join(text_parts)
    return full_text.strip()


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
            code = match.group(1).upper()
            # Skip invalid codes (too short or common words)
            if len(code) >= 4 and code not in ["CODE", "PROMO", "COUPON", "USE", "GET"]:
                return code
    
    return None


def extract_expiry_date(text: str) -> Optional[str]:
    """Extract expiry date from text."""
    date_patterns = [
        r'(?:expires?|expiry|valid until|until|ends?)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(?:expires?|expiry|valid until|until)[\s:]+(\w+\s+\d{1,2},?\s+\d{2,4})',
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    return None


def extract_service_category(text: str) -> str:
    """Extract service category from text."""
    text_lower = text.lower()
    
    if any(kw in text_lower for kw in ["oil change", "oil", "synthetic"]):
        return "oil change"
    elif any(kw in text_lower for kw in ["tire", "tires"]):
        return "tires"
    elif any(kw in text_lower for kw in ["brake", "brakes"]):
        return "brakes"
    elif any(kw in text_lower for kw in ["battery", "batteries"]):
        return "battery"
    elif any(kw in text_lower for kw in ["exhaust"]):
        return "exhaust"
    elif any(kw in text_lower for kw in ["coolant", "flush"]):
        return "coolant flush"
    elif any(kw in text_lower for kw in ["transmission"]):
        return "transmission"
    else:
        return "general"


def calculate_confidence_score(text: str, has_discount: bool, has_code: bool, has_expiry: bool) -> float:
    """Calculate confidence score based on matched indicators."""
    confidence = 0.3  # Base confidence
    
    if has_discount:
        confidence += 0.3
    if has_code:
        confidence += 0.2
    if has_expiry:
        confidence += 0.15
    
    # Bonus for service keywords
    service_keywords = ["oil change", "tire", "brake", "battery", "service"]
    if any(kw in text.lower() for kw in service_keywords):
        confidence += 0.1
    
    return min(confidence, 0.95)


def extract_promotions_from_text(text: str) -> List[Dict]:
    """
    Extract promotions from AI Overview text with STRICT filtering.
    
    Requirements:
    - Must have discount value OR coupon code
    - Deduplicate by sentence and discount+code combination
    """
    if not text:
        return []
    
    # Split into sentences (try multiple delimiters)
    sentences = re.split(r'[.!?]\s+|\n+', text)
    
    seen_signatures = set()
    promotions = []
    
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 20:  # Skip very short sentences
            continue
        
        # Extract promotion details
        discount_value = extract_discount_value(sentence)
        coupon_code = extract_coupon_code(sentence)
        expiry_date = extract_expiry_date(sentence)
        service_category = extract_service_category(sentence)
        
        # STRICT FILTERING: Must have discount OR code
        if not discount_value and not coupon_code:
            continue
        
        # Create signature for deduplication
        signature = f"{discount_value or 'no-discount'}|{coupon_code or 'no-code'}"
        normalized_sentence = " ".join(sentence.lower().split())
        
        # Check if we've seen this exact combination
        if signature in seen_signatures:
            # Check if same sentence text (exact duplicate)
            for existing in promotions:
                existing_text = existing.get("offer_details", "").lower()
                similarity = fuzz.ratio(normalized_sentence, existing_text)
                if similarity > 90:
                    continue  # Skip exact duplicate
        else:
            seen_signatures.add(signature)
        
        # Calculate confidence
        confidence = calculate_confidence_score(
            sentence,
            bool(discount_value),
            bool(coupon_code),
            bool(expiry_date)
        )
        
        # Build promotion title
        if discount_value:
            promotion_title = f"{discount_value} off {service_category}" if service_category != "general" else f"{discount_value} off"
        elif coupon_code:
            promotion_title = f"Coupon {coupon_code} - {service_category}" if service_category != "general" else f"Coupon {coupon_code}"
        else:
            promotion_title = service_category.replace("_", " ").title()
        
        promo = {
            "promotion_title": promotion_title,
            "offer_details": sentence[:500],  # Limit to 500 chars
            "discount_value": discount_value or "NA",
            "coupon_code": coupon_code or "NA",
            "expiry_date": expiry_date or "NA",
            "service_category": service_category,
            "source": "google_ai_overview",
            "confidence": round(confidence, 2)
        }
        
        promotions.append(promo)
        logger.debug(f"Extracted promo: {promotion_title} - {discount_value or coupon_code}")
    
    logger.info(f"Extracted {len(promotions)} valid promotions from AI Overview")
    return promotions


def get_business_insights(text: str) -> str:
    """
    Get business insights summary.
    
    Option 1: Use Perplexity AI (if available)
    Option 2: Use filtered text join (fallback)
    """
    # For now, use filtered text join (can be enhanced with Perplexity later)
    # Remove promotional content and keep business info
    lines = text.split("\n")
    insights = []
    
    for line in lines:
        line_lower = line.lower()
        # Skip promotional lines
        if any(kw in line_lower for kw in ["$", "%", "off", "discount", "coupon", "code", "expires"]):
            continue
        # Keep business/service info
        if len(line.strip()) > 30:
            insights.append(line.strip())
    
    # Join and limit to 5000 chars
    insights_text = " ".join(insights)
    return insights_text[:5000]


def scrape_ai_overview(competitor: Dict, use_ai_overview_only: bool = False) -> Dict:
    """
    Main entry point for AI Overview scraper.
    
    Args:
        competitor: Competitor dictionary with name, address, etc.
        use_ai_overview_only: If True, skip website scraping (e.g., for Mr. Lube)
    
    Returns:
        Dict with google_ai_overview_text, google_ai_source_links, and promotions
    """
    business_name = competitor.get("name", "")
    # Use full address for query building (it will extract city)
    location = competitor.get("address", "") or "Edmonton"
    
    logger.info(f"Fetching AI Overview for {business_name}")
    
    # Build search query (extracts city from full address)
    query = build_search_query(business_name, location)
    logger.info(f"Search query: {query}")
    
    # Fetch AI Overview
    ai_data = fetch_ai_overview(query)
    
    if ai_data.get("error"):
        logger.error(f"Failed to fetch AI Overview: {ai_data['error']}")
        return {
            "google_ai_overview_text": "",
            "google_ai_source_links": [],
            "promotions": []
        }
    
    # Extract text from AI Overview
    ai_overview_text = extract_text_from_ai_overview(ai_data.get("ai_overview", {}))
    source_links = ai_data.get("source_links", [])[:10]  # Limit to 10 links
    
    logger.info(f"Extracted {len(ai_overview_text)} chars from AI Overview")
    logger.info(f"Found {len(source_links)} source links")
    
    # Extract promotions
    promotions = extract_promotions_from_text(ai_overview_text)
    
    # Deduplicate promotions by discount+code combination
    seen_promos = {}
    deduplicated = []
    for promo in promotions:
        discount = promo.get("discount_value", "NA")
        code = promo.get("coupon_code", "NA")
        key = f"{discount}|{code}"
        
        if key not in seen_promos:
            seen_promos[key] = promo
            deduplicated.append(promo)
        else:
            # Keep the one with more complete info
            existing = seen_promos[key]
            if len(promo.get("offer_details", "")) > len(existing.get("offer_details", "")):
                seen_promos[key] = promo
                deduplicated = [p if p != existing else promo for p in deduplicated]
    
    promotions = deduplicated
    
    # Extract source links from AI Overview references if available
    ai_overview_data = ai_data.get("ai_overview", {})
    if isinstance(ai_overview_data, dict) and ai_overview_data.get("references"):
        ref_links = [ref.get("link") for ref in ai_overview_data.get("references", [])[:10] if ref.get("link")]
        if ref_links:
            source_links = ref_links
            logger.info(f"Extracted {len(source_links)} source links from AI Overview references")
    
    # If no valid promotions found, return CHECK promo
    if not promotions:
        logger.warning("No valid promotions found in AI Overview")
        promotions = [{
            "promotion_title": "CHECK",
            "offer_details": "CHECK — no promotion found",
            "discount_value": "NA",
            "coupon_code": "NA",
            "expiry_date": "NA",
            "service_category": "general",
            "source": "google_ai_overview",
            "confidence": 0.3
        }]
    
    # Get business insights (use full AI Overview text, filtered)
    business_insights = get_business_insights(ai_overview_text)
    # If business insights is empty, use the full text (filtered)
    if not business_insights and ai_overview_text:
        business_insights = ai_overview_text[:5000]
    
    result = {
        "google_ai_overview_text": business_insights[:5000] if business_insights else "",  # Limit to 5000 chars
        "google_ai_source_links": source_links,
        "promotions": promotions
    }
    
    logger.info(f"AI Overview scrape complete: {len(promotions)} promotions found")
    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Load competitor data
    competitor_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    competitors = json.loads(competitor_file.read_text())
    
    # Test with first competitor or Mr. Lube
    test_competitor = next((c for c in competitors if "mr" in c.get("name", "").lower() and "lube" in c.get("name", "").lower()), None)
    if not test_competitor:
        test_competitor = competitors[0] if competitors else None
    
    if not test_competitor:
        logger.error("No competitor found for testing")
        sys.exit(1)
    
    result = scrape_ai_overview(test_competitor, use_ai_overview_only=True)
    print(f"\n✅ AI Overview scraping complete!")
    print(f"   Found {len(result.get('promotions', []))} promotions")
    print(f"   Source links: {len(result.get('google_ai_source_links', []))}")
    print(f"\n📊 Promotions:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

