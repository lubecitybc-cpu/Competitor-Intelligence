#!/usr/bin/env python3
"""Run all scrapers and save results to JSON files."""
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List

# Add app to path
sys.path.insert(0, str(Path(__file__).parent))

from app.scrapers.speedy_scraper import scrape_speedy
from app.scrapers.midas_scraper import scrape_midas
from app.scrapers.integra_scraper import scrape_integra
from app.scrapers.trail_scraper import scrape_trail
from app.scrapers.valvoline_scraper import scrape_valvoline
from app.scrapers.kal_scraper import scrape_kal
from app.scrapers.jiffy_scraper import scrape_jiffy
from app.scrapers.fountain_scraper import scrape_fountain
from app.scrapers.goodnews_scraper import scrape_goodnews
from app.scrapers.ai_overview_scraper import scrape_ai_overview
from app.scrapers.google_reviews_scraper import scrape_google_reviews

# Map competitor names to their scraper functions
SCRAPER_MAP = {
    "speedy auto service": scrape_speedy,
    "midas": scrape_midas,
    "integra tire auto centre": scrape_integra,
    "trail tire auto centres": scrape_trail,
    "valvoline express care": scrape_valvoline,
    "kal tire": scrape_kal,
    "jiffy lube": scrape_jiffy,
    "fountain tire": scrape_fountain,
    "good news auto": scrape_goodnews,
}


def main():
    """Run all scrapers and save results."""
    print("=" * 80)
    print("🚀 COMPETITOR INTELLIGENCE PLATFORM - FULL TEST RUN")
    print("=" * 80)
    print()
    
    # Load competitor data
    competitor_file = Path(__file__).parent / "app" / "config" / "competitor_list.json"
    
    if not competitor_file.exists():
        print(f"❌ Error: Competitor list not found at {competitor_file}")
        return 1
    
    competitors = json.loads(competitor_file.read_text())
    
    # Filter competitors: either have promo_links+scraper OR use_ai_overview_only
    competitors_to_scrape = []
    for comp in competitors:
        name_lower = comp.get("name", "").lower()
        use_ai_only = comp.get("use_ai_overview_only", False)
        
        # Include if: (has promo_links and scraper) OR (uses AI Overview only)
        if use_ai_only or (comp.get("promo_links") and name_lower in SCRAPER_MAP):
            competitors_to_scrape.append(comp)
    
    print(f"📋 Found {len(competitors_to_scrape)} competitors to scrape:")
    for comp in competitors_to_scrape:
        ai_only = "🤖 AI Overview Only" if comp.get("use_ai_overview_only", False) else ""
        print(f"   • {comp.get('name')} {ai_only}")
    print()
    
    # Results tracking
    all_results = {
        "test_run_at": datetime.now().isoformat(),
        "total_competitors": len(competitors_to_scrape),
        "results": []
    }
    
    # Run each scraper
    for i, competitor in enumerate(competitors_to_scrape, 1):
        name = competitor.get("name", "Unknown")
        name_lower = name.lower()
        use_ai_only = competitor.get("use_ai_overview_only", False)
        
        print("=" * 80)
        print(f"[{i}/{len(competitors_to_scrape)}] Testing: {name}")
        print("=" * 80)
        
        if use_ai_only:
            print(f"   🤖 Mode: AI Overview Only (skipping website scraping)")
        else:
            print(f"   URLs: {', '.join(competitor.get('promo_links', []))}")
        print()
        
        start_time = time.time()
        result = None
        website_promos_count = 0
        ai_overview_result = None
        
        try:
            # Step 1: Run website scraper with retry logic (unless AI Overview only)
            website_promos_count = 0
            result = None
            max_retries = 3  # Initial attempt + 2 retries
            
            if not use_ai_only:
                scraper_func = SCRAPER_MAP.get(name_lower)
                if scraper_func:
                    # Retry logic: try up to 3 times
                    for attempt in range(1, max_retries + 1):
                        if attempt > 1:
                            print(f"   🔄 Retry attempt {attempt}/{max_retries}...")
                            time.sleep(2)  # Small delay between retries
                        
                        try:
                            result = scraper_func(competitor)
                            website_promos_count = result.get("count", 0) if result and not result.get("error") else 0
                            
                            if website_promos_count > 0:
                                print(f"   ✅ Website scraper: {website_promos_count} promotions found (attempt {attempt})")
                                break  # Success, exit retry loop
                            elif attempt < max_retries:
                                print(f"   ⚠️  Attempt {attempt}: Found 0 promotions, retrying...")
                            else:
                                print(f"   ⚠️  Website scraper: 0 promotions found after {max_retries} attempts")
                        except Exception as e:
                            if attempt < max_retries:
                                print(f"   ⚠️  Attempt {attempt} failed: {str(e)[:100]}, retrying...")
                            else:
                                print(f"   ❌ Website scraper failed after {max_retries} attempts: {str(e)[:100]}")
                                result = {
                                    "competitor": name,
                                    "error": str(e),
                                    "promotions": [],
                                    "count": 0,
                                    "scraped_at": datetime.now().isoformat()
                                }
                else:
                    print(f"   ⚠️  No website scraper found for {name}")
                    result = {
                        "competitor": name,
                        "promotions": [],
                        "count": 0,
                        "scraped_at": datetime.now().isoformat()
                    }
            else:
                # AI Overview only - return empty website result (skip website scraping entirely)
                print(f"   🤖 AI Overview Only mode - skipping website scraping")
                result = {
                    "competitor": name,
                    "promotions": [],
                    "count": 0,
                    "scraped_at": datetime.now().isoformat()
                }
            
            # Step 2: Use AI Overview if: (AI Overview only) OR (website scraper returned 0 promotions after retries)
            # IMPORTANT: Mr. Lube should ONLY use AI Overview, not as fallback
            if use_ai_only:
                # AI Overview only mode - always use it
                print(f"   🤖 Using AI Overview (AI Overview Only mode)...")
                ai_overview_result = scrape_ai_overview(competitor, use_ai_overview_only=True)
                ai_promos = ai_overview_result.get("promotions", [])
                ai_count = len([p for p in ai_promos if p.get("promotion_title") != "CHECK"])
                print(f"   ✅ AI Overview: {ai_count} promotions found")
                
                # Use AI Overview result as main result
                valid_ai_promos = [p for p in ai_promos if p.get("promotion_title") != "CHECK"]
                result = {
                    "competitor": name,
                    "scraped_at": datetime.now().isoformat(),
                    "promotions": valid_ai_promos,
                    "count": ai_count,
                    "google_ai_overview_text": ai_overview_result.get("google_ai_overview_text", ""),
                    "google_ai_source_links": ai_overview_result.get("google_ai_source_links", []),
                    "ai_overview_used": True,
                    "ai_overview_promotions_count": ai_count,
                    "website_attempts": 0  # No website attempts for AI-only mode
                }
            elif website_promos_count == 0:
                # Website scraper returned 0 promotions after retries, use AI Overview as fallback
                print(f"   ⚠️  Website scraper returned 0 promotions after {max_retries} attempts, using AI Overview as fallback...")
                ai_overview_result = scrape_ai_overview(competitor, use_ai_overview_only=False)
                ai_promos = ai_overview_result.get("promotions", [])
                ai_count = len([p for p in ai_promos if p.get("promotion_title") != "CHECK"])
                print(f"   ✅ AI Overview: {ai_count} promotions found")
                
                # Merge AI Overview promotions with website results
                if ai_count > 0:
                    if result:
                        # Add AI Overview promotions to result
                        existing_promos = result.get("promotions", [])
                        
                        # Filter out "CHECK" promotions from AI Overview
                        valid_ai_promos = [p for p in ai_promos if p.get("promotion_title") != "CHECK"]
                        
                        # Merge promotions
                        result["promotions"] = existing_promos + valid_ai_promos
                        result["count"] = len(result["promotions"])
                        
                        # Add AI Overview metadata
                        result["google_ai_overview_text"] = ai_overview_result.get("google_ai_overview_text", "")
                        result["google_ai_source_links"] = ai_overview_result.get("google_ai_source_links", [])
                        result["ai_overview_used"] = True
                        result["ai_overview_promotions_count"] = ai_count
                        result["website_attempts"] = max_retries
                    else:
                        # If no website result, use AI Overview result as main result
                        valid_ai_promos = [p for p in ai_promos if p.get("promotion_title") != "CHECK"]
                        result = {
                            "competitor": name,
                            "scraped_at": datetime.now().isoformat(),
                            "promotions": valid_ai_promos,
                            "count": ai_count,
                            "google_ai_overview_text": ai_overview_result.get("google_ai_overview_text", ""),
                            "google_ai_source_links": ai_overview_result.get("google_ai_source_links", []),
                            "ai_overview_used": True,
                            "ai_overview_promotions_count": ai_count,
                            "website_attempts": max_retries
                        }
                else:
                    # No AI Overview promotions found either
                    if result:
                        result["ai_overview_used"] = False
                        result["ai_overview_promotions_count"] = 0
                        result["website_attempts"] = max_retries
            else:
                # Website scraper found promotions, don't use AI Overview
                if result:
                    result["ai_overview_used"] = False
                    result["ai_overview_promotions_count"] = 0
                    result["website_attempts"] = 1  # Success on first attempt (or update if we track which attempt succeeded)
            
            elapsed = time.time() - start_time
            
            # Add timing info
            if result:
                result["execution_time_seconds"] = round(elapsed, 2)
                result["status"] = "success" if not result.get("error") else "error"
                
                # Save results to file
                output_file = Path(__file__).parent / "data" / "promotions" / f"{name.lower().replace(' ', '_')}.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(result, indent=2, default=str))
            
            # Store result in summary
            all_results["results"].append({
                "competitor": name,
                "status": result.get("status", "unknown") if result else "unknown",
                "promotions_found": result.get("count", 0) if result else 0,
                "website_promotions": website_promos_count,
                "ai_overview_promotions": result.get("ai_overview_promotions_count", 0) if result else 0,
                "ai_overview_used": result.get("ai_overview_used", False) if result else False,
                "execution_time_seconds": result.get("execution_time_seconds", 0) if result else round(elapsed, 2),
                "error": result.get("error") if result else None,
                "scraped_at": result.get("scraped_at") if result else None,
            })
            
            if result and result.get("error"):
                print(f"❌ Error: {result['error']}")
            else:
                total_promos = result.get("count", 0) if result else 0
                ai_used = result.get("ai_overview_used", False) if result else False
                source_info = " (AI Overview)" if ai_used and use_ai_only else (" (Website + AI Overview fallback)" if ai_used else " (Website)")
                print(f"✅ Success! Found {total_promos} promotions{source_info} in {elapsed:.2f}s")
                print(f"   Saved to: data/promotions/{name.lower().replace(' ', '_')}.json")
            
            # Show sample promotions
            if result and result.get("promotions"):
                print(f"\n   📊 Sample promotions (first 3):")
                for promo in result.get("promotions", [])[:3]:
                    title = promo.get('promotion_title', promo.get('ad_title', promo.get('service_name', 'N/A')))
                    discount = promo.get('discount_value', 'N/A')
                    source = promo.get('source', 'N/A')
                    print(f"      • [{source}] {title[:60]}: {discount}")
                if len(result.get("promotions", [])) > 3:
                    print(f"      ... and {len(result['promotions']) - 3} more")
        
        except Exception as e:
            elapsed = time.time() - start_time
            error_msg = str(e)
            print(f"❌ Exception occurred: {error_msg}")
            
            all_results["results"].append({
                "competitor": name,
                "status": "exception",
                "promotions_found": 0,
                "website_promotions": website_promos_count,
                "ai_overview_promotions": 0,
                "ai_overview_used": False,
                "execution_time_seconds": round(elapsed, 2),
                "error": error_msg,
            })
        
        print()
        time.sleep(2)  # Small delay between scrapers (increased for API rate limiting)
    
    # Step 3: Run Google Reviews scraper for all competitors
    print("=" * 80)
    print("📊 SCRAPING GOOGLE REVIEWS FOR ALL COMPETITORS")
    print("=" * 80)
    print()
    
    all_reviews = []
    reviews_start_time = time.time()
    
    for i, competitor in enumerate(competitors, 1):
        name = competitor.get("name", "Unknown")
        print(f"[{i}/{len(competitors)}] Scraping reviews for: {name}")
        
        try:
            review_result = scrape_google_reviews(competitor)
            all_reviews.append(review_result)
            
            stars = review_result.get("google_review_stars", "NA")
            count = review_result.get("google_review_count", "NA")
            print(f"   ✅ Stars: {stars} | Count: {count}")
            
        except Exception as e:
            print(f"   ❌ Error: {str(e)}")
            all_reviews.append({
                "business_name": name,
                "google_review_stars": "NA",
                "google_review_count": "NA",
                "google_business_url": competitor.get("google_maps", "NA"),
                "google_maps_url": competitor.get("google_maps", "NA"),
                "top_review_snippets": [],
                "scraped_at": datetime.now().isoformat(),
                "error": str(e)
            })
        
        # Rate limiting: 1.2-1.6 seconds between reviews queries
        if i < len(competitors):
            time.sleep(1.4)
    
    reviews_elapsed = time.time() - reviews_start_time
    print()
    print(f"✅ Completed Google Reviews scraping for {len(all_reviews)} competitors in {reviews_elapsed:.2f}s")
    print()
    
    # Save all reviews to a single file
    reviews_output_file = Path(__file__).parent / "data" / "reviews" / "all_reviews.json"
    reviews_output_file.parent.mkdir(parents=True, exist_ok=True)
    reviews_summary = {
        "scraped_at": datetime.now().isoformat(),
        "total_competitors": len(all_reviews),
        "reviews": all_reviews
    }
    reviews_output_file.write_text(json.dumps(reviews_summary, indent=2, default=str))
    print(f"💾 All reviews saved to: {reviews_output_file}")
    print()
    
    # Step 4: Merge all data for Google Sheets
    print("=" * 80)
    print("🔄 MERGING DATA FOR GOOGLE SHEETS")
    print("=" * 80)
    print()
    
    try:
        from app.mergers.promotions_reviews_merger import merge_all_data, save_merged_data
        
        merged_rows = merge_all_data()
        merged_output_file = save_merged_data(merged_rows)
        
        print(f"✅ Merged {len(merged_rows)} rows for Google Sheets")
        print(f"💾 Merged data saved to: {merged_output_file}")
        print()
        
        # Show sample rows
        if merged_rows:
            print("📊 Sample merged rows (first 3):")
            for i, row in enumerate(merged_rows[:3], 1):
                print(f"\n   {i}. {row.get('business_name')}")
                print(f"      Promo Description: {row.get('promo_description', 'N/A')[:70]}...")
                print(f"      Ad Text: {row.get('ad_text', 'N/A')[:70] if row.get('ad_text') else 'N/A'}...")
                print(f"      Google Reviews: {row.get('google_reviews', 'N/A')}")
            if len(merged_rows) > 3:
                print(f"\n   ... and {len(merged_rows) - 3} more rows")
            print()
            
            # Step 5: Write to Google Sheets
            print("=" * 80)
            print("📝 WRITING TO GOOGLE SHEETS")
            print("=" * 80)
            print()
            
            try:
                from app.sheets.google_sheets_writer import write_to_sheets
                
                success = write_to_sheets(merged_rows)
                if success:
                    print(f"✅ Successfully wrote {len(merged_rows)} rows to Google Sheets")
                    print(f"   Sheet: https://docs.google.com/spreadsheets/d/15vOEjTo4bNSZsWmMA2ilPp44PbMie14P1hWFtIKO_B8/edit")
                else:
                    print(f"❌ Failed to write to Google Sheets")
            except Exception as e:
                print(f"⚠️  Warning: Could not write to Google Sheets: {e}")
                print(f"   Merged data is available at: {merged_output_file}")
            print()
    except Exception as e:
        print(f"⚠️  Warning: Could not merge data for Google Sheets: {e}")
        print()
    
    # Calculate summary statistics
    successful = sum(1 for r in all_results["results"] if r["status"] == "success")
    failed = sum(1 for r in all_results["results"] if r["status"] != "success")
    total_promotions = sum(r["promotions_found"] for r in all_results["results"])
    total_time = sum(r["execution_time_seconds"] for r in all_results["results"]) + reviews_elapsed
    
    # Print final summary
    print("=" * 80)
    print("📊 FINAL SUMMARY")
    print("=" * 80)
    print(f"   ✅ Successful: {successful}/{len(competitors_to_scrape)}")
    print(f"   ❌ Failed: {failed}/{len(competitors_to_scrape)}")
    print(f"   📦 Total Promotions Found: {total_promotions}")
    print(f"   ⏱️  Total Execution Time: {total_time:.2f}s ({total_time/60:.1f} minutes)")
    print()
    print("   Detailed Results:")
    for r in all_results["results"]:
        status_icon = "✅" if r["status"] == "success" else "❌"
        source_info = ""
        if r.get("ai_overview_used"):
            if r.get("website_promotions", 0) == 0:
                source_info = " [🤖 AI Overview Only]"
            else:
                source_info = f" [🌐 Website: {r.get('website_promotions', 0)} + 🤖 AI: {r.get('ai_overview_promotions', 0)}]"
        else:
            source_info = f" [🌐 Website: {r.get('website_promotions', 0)}]"
        print(f"   {status_icon} {r['competitor']}: {r['promotions_found']} promotions{source_info} ({r['execution_time_seconds']:.1f}s)")
        if r.get("error"):
            print(f"      Error: {r['error'][:100]}")
    print()
    
    # Save summary to JSON
    summary_file = Path(__file__).parent / "data" / "promotions" / "test_run_summary.json"
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"📄 Summary saved to: {summary_file}")
    print("=" * 80)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

