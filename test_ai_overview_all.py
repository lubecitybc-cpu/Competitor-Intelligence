#!/usr/bin/env python3
"""Test AI Overview scraper on all competitors for QA testing."""
import sys
import json
import time
from pathlib import Path
from datetime import datetime

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
    
    print("=" * 100)
    print("🤖 AI OVERVIEW SCRAPER - TESTING ALL COMPETITORS")
    print("=" * 100)
    print(f"Testing {len(competitors)} competitors")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    results = []
    total_promotions = 0
    
    for i, competitor in enumerate(competitors, 1):
        name = competitor.get("name", "Unknown")
        print("=" * 100)
        print(f"[{i}/{len(competitors)}] Testing: {name}")
        print("=" * 100)
        
        start_time = time.time()
        
        try:
            # Test AI Overview scraper (use_ai_overview_only=False for all except Mr. Lube)
            use_ai_only = "mr" in name.lower() and "lube" in name.lower()
            result = scrape_ai_overview(competitor, use_ai_overview_only=use_ai_only)
            
            elapsed = time.time() - start_time
            
            promotions = result.get("promotions", [])
            source_links = result.get("google_ai_source_links", [])
            insights_length = len(result.get("google_ai_overview_text", ""))
            
            # Count valid promotions (excluding CHECK)
            valid_promos = [p for p in promotions if p.get("promotion_title") != "CHECK"]
            promo_count = len(valid_promos)
            total_promotions += promo_count
            
            status = "✅" if promo_count > 0 else "⚠️"
            
            print(f"{status} Status: {'SUCCESS' if promo_count > 0 else 'NO PROMOTIONS'}")
            print(f"   Promotions Found: {promo_count}")
            print(f"   Source Links: {len(source_links)}")
            print(f"   Business Insights: {insights_length} chars")
            print(f"   Execution Time: {elapsed:.2f}s")
            print()
            
            if valid_promos:
                print("   📊 Promotions:")
                for j, promo in enumerate(valid_promos[:5], 1):  # Show first 5
                    title = promo.get("promotion_title", "N/A")
                    discount = promo.get("discount_value", "N/A")
                    code = promo.get("coupon_code", "N/A")
                    print(f"      {j}. {title}")
                    print(f"         Discount: {discount} | Code: {code}")
                if len(valid_promos) > 5:
                    print(f"      ... and {len(valid_promos) - 5} more")
                print()
            
            if source_links:
                print("   🔗 Source Links (first 3):")
                for j, link in enumerate(source_links[:3], 1):
                    # Clean up the link (remove URL fragments)
                    clean_link = link.split('#')[0] if '#' in link else link
                    print(f"      {j}. {clean_link[:80]}...")
                if len(source_links) > 3:
                    print(f"      ... and {len(source_links) - 3} more")
                print()
            
            results.append({
                "competitor": name,
                "promotions_found": promo_count,
                "total_promotions": len(promotions),
                "source_links": len(source_links),
                "insights_length": insights_length,
                "execution_time": round(elapsed, 2),
                "status": "success" if promo_count > 0 else "no_promotions",
                "promotions": valid_promos
            })
            
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"❌ ERROR: {str(e)}")
            print()
            
            results.append({
                "competitor": name,
                "promotions_found": 0,
                "total_promotions": 0,
                "source_links": 0,
                "insights_length": 0,
                "execution_time": round(elapsed, 2),
                "status": "error",
                "error": str(e),
                "promotions": []
            })
        
        # Small delay between requests to avoid rate limiting
        if i < len(competitors):
            time.sleep(2)
    
    # Print summary
    print()
    print("=" * 100)
    print("📊 SUMMARY - AI OVERVIEW TEST RESULTS")
    print("=" * 100)
    
    successful = sum(1 for r in results if r["status"] == "success")
    no_promotions = sum(1 for r in results if r["status"] == "no_promotions")
    errors = sum(1 for r in results if r["status"] == "error")
    total_time = sum(r["execution_time"] for r in results)
    
    print(f"Total Competitors Tested: {len(competitors)}")
    print(f"✅ With Promotions: {successful}")
    print(f"⚠️  No Promotions: {no_promotions}")
    print(f"❌ Errors: {errors}")
    print(f"📦 Total Promotions Found: {total_promotions}")
    print(f"⏱️  Total Execution Time: {total_time:.2f}s ({total_time/60:.1f} minutes)")
    print()
    
    print("Detailed Results:")
    for r in results:
        status_icon = "✅" if r["status"] == "success" else ("⚠️" if r["status"] == "no_promotions" else "❌")
        print(f"   {status_icon} {r['competitor']:35s} → {r['promotions_found']:2d} promotions ({r['execution_time']:.1f}s)")
        if r.get("error"):
            print(f"      Error: {r['error'][:80]}")
    
    # Save results to JSON
    output_file = Path(__file__).parent / "data" / "promotions" / "ai_overview_all_test.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    summary_data = {
        "test_run_at": datetime.now().isoformat(),
        "total_competitors": len(competitors),
        "summary": {
            "with_promotions": successful,
            "no_promotions": no_promotions,
            "errors": errors,
            "total_promotions_found": total_promotions,
            "total_execution_time": total_time
        },
        "results": results
    }
    
    output_file.write_text(json.dumps(summary_data, indent=2, default=str))
    print()
    print(f"💾 Full results saved to: {output_file}")
    print("=" * 100)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

