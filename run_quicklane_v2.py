"""Run Quick Lane v2 scraper (Phase 5).

Usage:
    python run_quicklane_v2.py                  # default = --qa-expanded
    python run_quicklane_v2.py --final-deduped  # collapse city fan-out
    python run_quicklane_v2.py --smoke          # only first URL (battery)
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.quicklane_scraper import scrape_quicklane_v2  # noqa: E402


def find_quicklane(config_path: Path) -> dict:
    data = json.loads(config_path.read_text())
    for entry in data:
        if entry.get("competitor", "").strip().lower().startswith("quick lane"):
            return entry
    raise SystemExit("Quick Lane not found in competitors.v2.json")


CSV_PREFERRED = [
    "website", "page_url", "business_name", "google_reviews", "service_name",
    "promo_description", "category", "contact", "location", "offer_details",
    "ad_title", "ad_text", "new_or_updated", "date_scraped",
    "city", "store_name", "source_scope", "extraction_method", "confidence",
    "needs_review", "needs_review_reason", "discount_value", "coupon_code",
    "expiry_date", "promotion_title", "normalized_title", "applicable_cities",
    "duplicate_group_id", "duplicate_group_total", "region_applicability",
]


def export_promotions_to_csv(rows: list, dest: Path) -> Path:
    if not rows:
        dest.write_text("", encoding="utf-8")
        return dest
    extra = sorted(set().union(*(r.keys() for r in rows)) - set(CSV_PREFERRED))
    fieldnames = [k for k in CSV_PREFERRED if any(k in r for r in rows)] + extra
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


def export_url_coverage(result: dict, dest: Path, competitor: str) -> Path:
    val = result.get("validation") or {}
    fields = ["competitor", "service_hint", "url", "source_scope", "status",
              "cards_on_page", "added_rows", "excluded_count"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for e in val.get("url_log") or []:
            w.writerow({
                "competitor": competitor,
                "service_hint": e.get("service_hint", ""),
                "url": e.get("url", ""),
                "source_scope": e.get("scope", ""),
                "status": e.get("status", ""),
                "cards_on_page": e.get("cards_on_page", 0),
                "added_rows": e.get("added_rows", 0),
                "excluded_count": e.get("excluded_count", 0),
            })
    return dest


def export_excluded_rows(result: dict, dest: Path, competitor: str) -> Path:
    val = result.get("validation") or {}
    excluded = val.get("excluded_rows") or []
    fields = ["competitor", "url", "source_scope", "extraction_method",
              "reason", "raw_text"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in excluded:
            w.writerow({
                "competitor": competitor,
                "url": x.get("url", ""),
                "source_scope": x.get("scope", ""),
                "extraction_method": x.get("extraction_method", ""),
                "reason": x.get("reason", ""),
                "raw_text": (x.get("raw_text") or "")[:1000],
            })
    return dest


def main() -> int:
    p = argparse.ArgumentParser(description="Run Quick Lane v2 scraper")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--qa-expanded", dest="mode", action="store_const", const="qa_expanded")
    g.add_argument("--final-deduped", dest="mode", action="store_const", const="final_deduped")
    p.set_defaults(mode="qa_expanded")
    p.add_argument("--smoke", action="store_true", help="Only run the first promo_link (battery).")
    args = p.parse_args()

    cfg = ROOT / "app" / "config" / "competitors.v2.json"
    entry = find_quicklane(cfg)

    if args.smoke:
        entry = dict(entry)
        entry["promo_links"] = entry["promo_links"][:1]
        print("** SMOKE MODE: 1 URL only **")

    print("=" * 70)
    print("Quick Lane v2 — Phase 5 run")
    print("=" * 70)
    print(f"Competitor : {entry['competitor']}")
    print(f"Cities     : {', '.join(entry.get('cities', []))}")
    print(f"URLs       : {len(entry.get('promo_links', []))}")
    print(f"Mode       : {args.mode}\n")

    t0 = time.time()
    result = scrape_quicklane_v2(entry, mode=args.mode)
    runtime = time.time() - t0

    promos = result.get("promotions", [])
    print(f"Total rows  : {result.get('count', 0)}")
    for city, n in (result.get("by_city") or {}).items():
        print(f"  {city:<18}: {n}")
    print(f"needs_review: {result.get('needs_review_count', 0)}\n")

    val = result.get("validation") or {}
    print("Validation")
    print(f"  Runtime              : {runtime:.1f}s")
    print(f"  Expected URLs        : {val.get('expected_url_count', 0)}")
    print(f"  Processed URLs       : {val.get('processed_url_count', 0)}")
    print(f"  Failed URLs          : {val.get('failed_url_count', 0)}")
    for u in val.get("failed_urls") or []:
        print(f"     - {u}")
    print(f"  Missing URLs         : {len(val.get('missing_urls') or [])}")
    print(f"  needs_review_count   : {val.get('needs_review_count', 0)}")
    print(f"  possible_us_only     : {val.get('possible_us_only_offer_count', 0)}")
    print(f"  unique_descriptions  : {val.get('unique_promo_descriptions', 0)}")
    print(f"  duplicate_group_total: {val.get('duplicate_group_total', 0)}")
    print(f"  excluded_row_count   : {val.get('excluded_row_count', 0)}")
    for r, n in (val.get("excluded_reason_counts") or {}).items():
        print(f"     - {r}: {n}")
    print("  service_count_by_category:")
    for s, n in (val.get("service_count_by_category") or {}).items():
        print(f"     - {s}: {n}")
    print("  extraction_method_counts:")
    for m, n in (val.get("extraction_method_counts") or {}).items():
        print(f"     - {m}: {n}")
    print("  row_count_by_url:")
    for u, n in (val.get("row_count_by_url") or {}).items():
        slug = u.split("/savings/")[-1].rstrip("/")
        print(f"     - [{n:>2}] {slug}")
    print()

    if promos:
        print("Sample rows (first 5):")
        for i, r in enumerate(promos[:5], 1):
            nr = " [needs_review]" if r.get("needs_review") else ""
            print(
                f"  {i}. [{r['city']:<14}|{r['service_name']:<12}] "
                f"d={r['discount_value']!r}  c={r['coupon_code']!r}  exp={r['expiry_date']!r}{nr}"
            )
            print(f"     {r['ad_title'][:90]!r}")
            print(f"     desc: {r['promo_description'][:120]!r}")
        print()

    json_path = ROOT / "data" / "promotions" / "quicklane_v2.json"
    csv_path = ROOT / "data" / "promotions" / "quicklane_v2.csv"
    coverage_path = ROOT / "data" / "promotions" / "quicklane_v2_url_coverage.csv"
    excluded_path = ROOT / "data" / "promotions" / "quicklane_v2_excluded_rows.csv"
    export_promotions_to_csv(promos, csv_path)
    export_url_coverage(result, coverage_path, entry["competitor"])
    if (val.get("excluded_rows") or []):
        export_excluded_rows(result, excluded_path, entry["competitor"])
        print(f"CSV (excluded): {excluded_path}")
    print(f"Saved: {json_path}")
    print(f"CSV (main):     {csv_path}")
    print(f"CSV (coverage): {coverage_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
