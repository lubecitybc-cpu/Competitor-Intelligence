#!/usr/bin/env python3
"""Test AI Overview scraper."""
import sys
import json
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent))

from app.scrapers.ai_overview_scraper import scrape_ai_overview

def main():
    # Load competitor data
    competitor_file = Path(__file__).parent / "app" / "config" / "competitor_list.json"
    
    if not competitor_file.exists():
        print(f"❌ Error: Competitor list not found at {competitor_file}")
        return 1
    
    competitors = json.loads(competitor_file.read_text())
    
    # Find Mr. Lube (use AI Overview only)
    mr_lube = next((c for c in competitors if "mr" in c.get("name", "").lower() and "lube" in c.get("name", "").lower()), None)
    
    if not mr_lube:
        print("❌ Mr. Lube not found in competitor list")
        return 1
    
    print("=" * 80)
    print("🤖 AI OVERVIEW SCRAPER - TEST")
    print("=" * 80)
    print(f"Business: {mr_lube.get('name')}")
    print(f"Location: {mr_lube.get('address', 'Edmonton')}")
    print()
    
    # Scrape AI Overview
    result = scrape_ai_overview(mr_lube, use_ai_overview_only=True)
    
    print("=" * 80)
    print("📊 RESULTS")
    print("=" * 80)
    print(f"Promotions Found: {len(result.get('promotions', []))}")
    print(f"Source Links: {len(result.get('google_ai_source_links', []))}")
    print()
    
    if result.get("promotions"):
        print("Promotions:")
        for i, promo in enumerate(result.get("promotions", []), 1):
            print(f"  {i}. {promo.get('promotion_title', 'N/A')}")
            print(f"     Discount: {promo.get('discount_value', 'N/A')}")
            print(f"     Code: {promo.get('coupon_code', 'N/A')}")
            print(f"     Expiry: {promo.get('expiry_date', 'N/A')}")
            print(f"     Confidence: {promo.get('confidence', 'N/A')}")
            print()
    
    if result.get("google_ai_source_links"):
        print("Source Links:")
        for i, link in enumerate(result.get("google_ai_source_links", [])[:5], 1):
            print(f"  {i}. {link}")
        if len(result.get("google_ai_source_links", [])) > 5:
            print(f"  ... and {len(result.get('google_ai_source_links', [])) - 5} more")
        print()
    
    if result.get("google_ai_overview_text"):
        preview = result.get("google_ai_overview_text", "")[:300]
        print(f"Business Insights Preview ({len(result.get('google_ai_overview_text', ''))} chars):")
        print(f"  {preview}...")
        print()
    
    # Save to file
    output_file = Path(__file__).parent / "data" / "promotions" / "ai_overview_test.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, indent=2, default=str))
    print(f"💾 Saved results to: {output_file}")
    print("=" * 80)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

