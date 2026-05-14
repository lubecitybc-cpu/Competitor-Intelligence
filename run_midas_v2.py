"""Run Midas v2 scraper (Phase 5).

Reads from app/config/competitors.v2.json and only scrapes Midas. Other
competitors and the legacy pipeline are NOT affected.

Usage:
    python run_midas_v2.py                 # default = --qa-expanded
    python run_midas_v2.py --final-deduped # collapse city-level duplicates
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.midas_scraper import scrape_midas_v2  # noqa: E402


def find_midas(config_path: Path) -> dict:
    data = json.loads(config_path.read_text())
    for entry in data:
        if entry.get("competitor", "").strip().lower() == "midas":
            return entry
    raise SystemExit("Midas not found in competitors.v2.json")


def export_promotions_to_csv(rows: list, dest: Path) -> Path:
    if not rows:
        dest.write_text("", encoding="utf-8")
        return dest
    preferred = [
        # existing sheet columns first
        "website", "page_url", "business_name", "google_reviews", "service_name",
        "promo_description", "category", "contact", "location", "offer_details",
        "ad_title", "ad_text", "new_or_updated", "date_scraped",
        # QA metadata after
        "city", "store_name", "source_scope", "extraction_method", "confidence",
        "needs_review", "discount_value", "coupon_code", "expiry_date",
        "promotion_title", "normalized_title", "applicable_cities",
        "duplicate_of_national", "duplicate_group_id",
    ]
    extra = sorted(set().union(*(r.keys() for r in rows)) - set(preferred))
    fieldnames = [k for k in preferred if any(k in r for r in rows)] + extra
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k in fieldnames:
                v = r.get(k)
                if v is None:
                    flat[k] = ""
                elif isinstance(v, (list, dict)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = str(v)
            w.writerow(flat)
    return dest


def export_url_coverage_csv(result: dict, dest: Path, competitor: str) -> Path:
    val = result.get("validation") or {}
    url_log = val.get("url_log") or []
    fieldnames = [
        "competitor", "city", "store_name", "url", "source_scope", "status",
        "raw_promo_count", "added_unique", "dropped_as_city_duplicate",
        "excluded_count", "notes",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in url_log:
            w.writerow({
                "competitor": competitor,
                "city": e.get("city") or "",
                "store_name": e.get("store_name") or "",
                "url": e.get("url") or "",
                "source_scope": e.get("scope", ""),
                "status": e.get("status", ""),
                "raw_promo_count": e.get("raw_promo_count", 0),
                "added_unique": e.get("added_unique", 0),
                "dropped_as_city_duplicate": e.get("dropped_as_city_duplicate", 0),
                "excluded_count": e.get("excluded_count", 0),
                "notes": "",
            })
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Midas v2 scraper")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--qa-expanded", dest="mode", action="store_const",
                   const="qa_expanded",
                   help="QA mode (default): every per-URL row kept; duplicates tagged.")
    g.add_argument("--final-deduped", dest="mode", action="store_const",
                   const="final_deduped",
                   help="Collapse city-level duplicates for client delivery.")
    parser.set_defaults(mode="qa_expanded")
    args = parser.parse_args()

    config_path = ROOT / "app" / "config" / "competitors.v2.json"
    midas = find_midas(config_path)

    print("=" * 70)
    print("Midas v2 — Phase 5 run")
    print("=" * 70)
    print(f"Cities: {', '.join(midas.get('cities', []))}")
    total_stores = sum(len(v) for v in midas.get("store_links", {}).values())
    print(f"Stores: {total_stores}")
    print(f"Mode  : {args.mode}\n")

    t0 = time.time()
    result = scrape_midas_v2(midas, mode=args.mode)
    runtime = time.time() - t0

    promos = result.get("promotions", [])
    print(f"Total rows  : {result.get('count', 0)}")
    for city, n in (result.get("by_city") or {}).items():
        print(f"  {city:<18}: {n} rows")
    print(f"needs_review: {result.get('needs_review_count', 0)}")
    print()

    val = result.get("validation") or {}
    print("Validation")
    print(f"  Runtime              : {runtime:.1f}s")
    print(f"  Expected URLs        : {val.get('expected_url_count', 0)}")
    print(f"  Processed URLs       : {val.get('processed_url_count', 0)}")
    print(f"  Failed URLs          : {val.get('failed_url_count', 0)}")
    failed = val.get("failed_urls") or []
    for u in failed:
        print(f"     - {u}")
    missing = val.get("missing_urls") or []
    print(f"  Missing URLs         : {len(missing)}")
    for u in missing[:10]:
        print(f"     - {u}")
    print(f"  needs_review_count   : {val.get('needs_review_count', 0)}")
    print(f"  unique_promo_descriptions: {val.get('unique_promo_descriptions', 0)}")
    print(f"  duplicate_group_total: {val.get('duplicate_group_total', 0)}")
    print("  Coupon-code enrichment:")
    print(f"     - attempted : {val.get('coupon_code_recovery_attempted', 0)}")
    print(f"     - recovered : {val.get('coupon_code_recovered_count', 0)}")
    print(f"     - missing   : {val.get('coupon_code_missing_count', 0)}")
    print(f"     - ambiguous : {val.get('coupon_code_ambiguous_count', 0)}")
    cov = val.get('coupon_code_coverage', 0.0) or 0.0
    print(f"     - coverage  : {cov:.1%}")
    print("  row_count_by_city:")
    for c, n in (val.get("row_count_by_city") or {}).items():
        print(f"     - {c}: {n}")
    print("  row_count_by_url:")
    for u, n in (val.get("row_count_by_url") or {}).items():
        print(f"     - [{n:>2}] {u}")
    dup_counts = val.get("duplicate_group_counts") or {}
    multi = {k: v for k, v in dup_counts.items() if v > 1}
    if multi:
        print(f"  multi-store duplicate groups: {len(multi)}")
        for gid, n in sorted(multi.items(), key=lambda kv: -kv[1])[:10]:
            print(f"     - {n}x  {gid}")
    print()

    if promos:
        print("Sample rows (first 5):")
        for i, p in enumerate(promos[:5], 1):
            print(
                f"  {i}. [{p.get('city'):<14}|{p.get('store_name')[:30]:<30}] "
                f"{p.get('service_name'):<16} | {p.get('ad_title','')[:60]}"
            )
            print(
                f"     discount={p.get('discount_value')!r}  code={p.get('coupon_code')!r}"
                f"  expiry={p.get('expiry_date')!r}"
            )
        print()

    json_path = ROOT / "data" / "promotions" / "midas_v2.json"
    csv_path = ROOT / "data" / "promotions" / "midas_v2.csv"
    coverage_path = ROOT / "data" / "promotions" / "midas_v2_url_coverage.csv"
    export_promotions_to_csv(promos, csv_path)
    export_url_coverage_csv(result, coverage_path, midas.get("competitor", "Midas"))
    print(f"Saved: {json_path}")
    print(f"CSV (main):     {csv_path}")
    print(f"CSV (coverage): {coverage_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
