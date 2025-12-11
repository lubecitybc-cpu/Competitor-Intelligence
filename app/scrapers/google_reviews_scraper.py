"""Google Reviews Scraper - Extract Google review stars and review counts using SerpAPI."""
import json
import time
import re
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

import requests

from app.config.constants import DATA_DIR, SERPAPI_KEY
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "google_reviews_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
REVIEWS_DIR = DATA_DIR / "reviews"
REVIEWS_DIR.mkdir(parents=True, exist_ok=True)


def _build_query(business_name: str, address: str) -> str:
    """Build query: '{business_name} {address}' or '{business_name} Edmonton Alberta'"""
    if address and address.strip():
        return f"{business_name} {address}"
    return f"{business_name} Edmonton Alberta"


def _normalize_stars(stars: any) -> str:
    """Normalize to string, 'NA' if missing/invalid. Never treat missing as 0.0."""
    try:
        if stars is None or stars == "":
            return "NA"
        return str(float(stars))  # e.g. "4.4"
    except (ValueError, TypeError):
        return "NA"


def _normalize_count(count: any) -> str:
    """Normalize to string, 'NA' if missing/invalid."""
    try:
        if count is None or count == "":
            return "NA"
        # Extract only digits (handles "290 reviews" → "290")
        cleaned = "".join(filter(str.isdigit, str(count)))
        return str(int(cleaned)) if cleaned else "NA"
    except (ValueError, TypeError):
        return "NA"


def _resolve_place_id(competitor: Dict) -> Optional[str]:
    """
    Step 1: Resolve place_id or data_id using SerpAPI.
    
    Method 1: Use google_maps_url from config (priority)
    Method 2: Fallback to text query
    """
    if not SERPAPI_KEY:
        logger.error("SERPAPI_KEY not found in environment variables")
        return None
    
    business_name = competitor.get("name", "")
    address = competitor.get("address", "")
    google_maps_url = competitor.get("google_maps", "")
    
    base_url = "https://serpapi.com/search.json"
    
    try:
        # Method 1: Try using google_maps_url from config
        if google_maps_url:
            logger.info(f"Attempting to resolve place_id using google_maps_url: {google_maps_url[:80]}...")
            params = {
                "engine": "google_maps",
                "google_maps_url": google_maps_url,
                "hl": "en",
                "gl": "ca",
                "api_key": SERPAPI_KEY
            }
            
            try:
                response = requests.get(base_url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                
                place_id = _extract_place_id_from_response(data)
                if place_id:
                    logger.info(f"Successfully resolved place_id using google_maps_url: {place_id[:20]}...")
                    return place_id
                else:
                    logger.warning("Could not extract place_id from google_maps_url response, trying fallback...")
            except Exception as e:
                logger.warning(f"Failed to use google_maps_url: {e}, trying fallback...")
        
        # Method 2: Fallback to text query
        query = _build_query(business_name, address)
        logger.info(f"Attempting to resolve place_id using text query: {query}")
        
        params = {
            "engine": "google_maps",
            "q": query,
            "hl": "en",
            "gl": "ca",
            "ll": "@53.5461,-113.4938,14z",  # Edmonton coordinates
            "api_key": SERPAPI_KEY
        }
        
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        place_id = _extract_place_id_from_response(data)
        if place_id:
            logger.info(f"Successfully resolved place_id using text query: {place_id[:20]}...")
            return place_id
        else:
            logger.error("Could not extract place_id from text query response")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error resolving place_id: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Error resolving place_id: {e}", exc_info=True)
        return None


def _extract_place_id_from_response(data: Dict) -> Optional[str]:
    """Extract place_id or data_id from SerpAPI response."""
    # Check place_results (dict or list)
    place_results = data.get("place_results")
    if place_results:
        if isinstance(place_results, dict):
            place_id = place_results.get("place_id") or place_results.get("data_id") or place_results.get("data_cid")
            if place_id:
                return str(place_id)
        elif isinstance(place_results, list) and len(place_results) > 0:
            first_result = place_results[0]
            if isinstance(first_result, dict):
                place_id = first_result.get("place_id") or first_result.get("data_id") or first_result.get("data_cid")
                if place_id:
                    return str(place_id)
    
    # Check local_results (dict or list)
    local_results = data.get("local_results")
    if local_results:
        if isinstance(local_results, dict):
            place_id = local_results.get("place_id") or local_results.get("data_id") or local_results.get("data_cid")
            if place_id:
                return str(place_id)
        elif isinstance(local_results, list) and len(local_results) > 0:
            first_result = local_results[0]
            if isinstance(first_result, dict):
                place_id = first_result.get("place_id") or first_result.get("data_id") or first_result.get("data_cid")
                if place_id:
                    return str(place_id)
    
    # Check organic_results (list)
    organic_results = data.get("organic_results", [])
    if organic_results and len(organic_results) > 0:
        first_result = organic_results[0]
        if isinstance(first_result, dict):
            place_id = first_result.get("place_id") or first_result.get("data_id") or first_result.get("data_cid")
            if place_id:
                return str(place_id)
    
    return None


def _fetch_reviews(place_id: str) -> Dict:
    """
    Step 2: Fetch reviews using place_id.
    
    Returns dict with rating, review_count, and maps_url.
    """
    if not SERPAPI_KEY:
        logger.error("SERPAPI_KEY not found in environment variables")
        return {}
    
    base_url = "https://serpapi.com/search.json"
    
    try:
        params = {
            "engine": "google_maps_reviews",
            "place_id": place_id,
            "hl": "en",
            "api_key": SERPAPI_KEY
        }
        
        logger.info(f"Fetching reviews for place_id: {place_id[:20]}...")
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # Extract from place_info
        place_info = data.get("place_info", {})
        rating = place_info.get("rating")
        review_count = place_info.get("reviews")
        maps_url = data.get("search_metadata", {}).get("google_maps_reviews_url", "")
        
        # Also try to get maps URL from place_info
        if not maps_url:
            maps_url = place_info.get("website") or place_info.get("url") or ""
        
        result = {
            "rating": rating,
            "review_count": review_count,
            "maps_url": maps_url
        }
        
        logger.info(f"Fetched reviews: rating={rating}, count={review_count}")
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching reviews: {e}", exc_info=True)
        return {}
    except Exception as e:
        logger.error(f"Error fetching reviews: {e}", exc_info=True)
        return {}


def scrape_google_reviews(competitor: Dict) -> Dict:
    """
    Main entry point for Google Reviews scraper.
    
    Args:
        competitor: Competitor dictionary with name, address, google_maps, etc.
    
    Returns:
        Dict with business_name, google_review_stars, google_review_count, 
        google_business_url, google_maps_url, scraped_at
    """
    business_name = competitor.get("name", "")
    address = competitor.get("address", "")
    google_maps_url = competitor.get("google_maps", "")
    
    logger.info(f"Scraping Google Reviews for {business_name}")
    
    try:
        # Step 1: Resolve place_id
        place_id = _resolve_place_id(competitor)
        
        if not place_id:
            logger.warning(f"Could not resolve place_id for {business_name}, returning NA values")
            return {
                "business_name": business_name,
                "google_review_stars": "NA",
                "google_review_count": "NA",
                "google_business_url": google_maps_url or "NA",
                "google_maps_url": google_maps_url or "NA",
                "top_review_snippets": [],
                "scraped_at": datetime.now().isoformat()
            }
        
        # Small delay before fetching reviews (rate limiting)
        time.sleep(1.2)
        
        # Step 2: Fetch reviews
        reviews_data = _fetch_reviews(place_id)
        
        rating = reviews_data.get("rating")
        review_count = reviews_data.get("review_count")
        maps_url = reviews_data.get("maps_url") or google_maps_url or "NA"
        
        # Normalize values
        stars = _normalize_stars(rating)
        count = _normalize_count(review_count)
        
        result = {
            "business_name": business_name,
            "google_review_stars": stars,
            "google_review_count": count,
            "google_business_url": maps_url,
            "google_maps_url": maps_url,
            "top_review_snippets": [],  # Not used in current Sheets output
            "scraped_at": datetime.now().isoformat()
        }
        
        # Save to file
        output_file = REVIEWS_DIR / f"{business_name.lower().replace(' ', '_')}_reviews.json"
        output_file.write_text(json.dumps(result, indent=2, default=str))
        logger.info(f"Saved reviews for {business_name} to {output_file}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error scraping Google Reviews for {business_name}: {e}", exc_info=True)
        return {
            "business_name": business_name,
            "google_review_stars": "NA",
            "google_review_count": "NA",
            "google_business_url": google_maps_url or "NA",
            "google_maps_url": google_maps_url or "NA",
            "top_review_snippets": [],
            "scraped_at": datetime.now().isoformat(),
            "error": str(e)
        }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Load competitor data
    competitor_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    competitors = json.loads(competitor_file.read_text())
    
    # Test with first competitor
    test_competitor = competitors[0] if competitors else None
    
    if not test_competitor:
        logger.error("No competitor found for testing")
        sys.exit(1)
    
    print(f"Testing Google Reviews scraper for: {test_competitor.get('name')}")
    result = scrape_google_reviews(test_competitor)
    
    print(f"\n✅ Results:")
    print(f"   Business: {result.get('business_name')}")
    print(f"   Stars: {result.get('google_review_stars')}")
    print(f"   Count: {result.get('google_review_count')}")
    print(f"   Maps URL: {result.get('google_maps_url')}")

