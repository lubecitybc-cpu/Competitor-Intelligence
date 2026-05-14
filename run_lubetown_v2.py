"""Run Lube Town v2 scraper (Phase 5).

Reads from app/config/competitors.v2.json and only scrapes Lube Town.
Other competitors and the legacy pipeline are NOT affected.

Usage:
    python run_lubetown_v2.py                  # full run (default --qa-expanded)
    python run_lubetown_v2.py --final-deduped  # same output for single-city
    python run_lubetown_v2.py --smoke          # alias for full (only 1 URL anyway)
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.lubetown_scraper import scrape_lubetown_v2  # noqa: E402


def find_lubetown(config_path: Path) -> dict:
    data = json.loads(config_path.read_text())
    for entry in data:
        if entry.get("competitor", "").strip().lower() == "lube town":
            return entry
    raise SystemExit("Lube Town not found in competitors.v2.json")


CSV_PREFERRED = [
    "website", "page_url", "business_name", "google_reviews", "service_name",
    "promo_description", "category", "contact", "location", "offer_details",
    "ad_title", "ad_text", "new_or_updated", "date_scraped",
    "city", "store_name", "source_scope", "extraction_method", "confidence",
    "needs_review", "needs_review_reason", "discount_value", "coupon_code",
    "expiry_date", "promotion_title", "normalized_title", "applicable_cities",
    "duplicate_group_id", "duplicate_group_total",
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
    rc_by_url = val.get("row_count_by_url") or {}
    fields = [
        "competitor", "url", "scope", "status",
        "cards_on_page", "added_rows", "excluded_count", "row_count_written",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for e in val.get("url_log") or []:
            u = e.get("url", "")
            w.writerow({
                "competitor": competitor,
                "url": u,
                "scope": e.get("scope", ""),
                "status": e.get("status", ""),
                "cards_on_page": e.get("cards_on_page", 0),
                "added_rows": e.get("added_rows", 0),
                "excluded_count": e.get("excluded_count", 0),
                "row_count_written": rc_by_url.get(u, 0),
            })
    return dest


def export_excluded_rows(result: dict, dest: Path, competitor: str) -> Path:
    val = result.get("validation") or {}
    excluded = val.get("excluded_rows") or []
    if not excluded:
        return dest
    fields = ["competitor", "url", "scope", "extraction_method", "reason", "raw_text"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in excluded:
            w.writerow({
                "competitor": competitor,
                "url": x.get("url", ""),
                "scope": x.get("scope", ""),
                "extraction_method": x.get("extraction_method", ""),
                "reason": x.get("reason", ""),
                "raw_text": (x.get("raw_text") or "")[:1000],
            })
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Lube Town v2 scraper")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--qa-expanded", dest="mode", action="store_const", const="qa_expanded")
    g.add_argument("--final-deduped", dest="mode", action="store_const", const="final_deduped")
    parser.add_argument("--smoke", action="store_true",
                        help="Smoke test (alias for full run — only 1 URL anyway).")
    parser.set_defaults(mode="qa_expanded")
    args = parser.parse_args()

    cfg = ROOT / "app" / "config" / "competitors.v2.json"
    entry = find_lubetown(cfg)

    print("=" * 70)
    print("Lube Town v2 — Phase 5 run")
    print("=" * 70)
    print(f"Competitor : {entry['competitor']}")
    print(f"Cities     : {', '.join(entry.get('cities', []))}")
    print(f"URLs       : {len(entry.get('promo_links', []))}")
    print(f"Mode       : {args.mode}")
    if args.smoke:
        print("** SMOKE MODE (single URL — same as full run)")
    print()

    t0 = time.time()
    result = scrape_lubetown_v2(entry, mode=args.mode)
    runtime = time.time() - t0

    if result.get("error"):
        print(f"ERROR: {result['error']}")
        return 1

    promos = result.get("promotions", [])
    val = result.get("validation") or {}

    print("-" * 70)
    print(f"Total rows  : {result.get('count', 0)}")
    for city, n in (result.get("by_city") or {}).items():
        print(f"  {city:<18}: {n} rows")
    print(f"needs_review: {result.get('needs_review_count', 0)}")
    print()

    print("Validation")
    print(f"  Runtime                    : {runtime:.1f}s")
    print(f"  expected_url_count         : {val.get('expected_url_count', 0)}")
    print(f"  processed_url_count        : {val.get('processed_url_count', 0)}")
    print(f"  failed_url_count           : {val.get('failed_url_count', 0)}")
    failed = val.get("failed_urls") or []
    if failed:
        for u in failed:
            print(f"     - {u}")
    missing = val.get("missing_urls") or []
    print(f"  missing_urls               : {missing}")
    print(f"  needs_review_count         : {val.get('needs_review_count', 0)}")
    print(f"  excluded_row_count         : {val.get('excluded_row_count', 0)}")
    for r, n in (val.get("excluded_reason_counts") or {}).items():
        print(f"     - {r}: {n}")
    print(f"  ocr_attempted              : {val.get('ocr_attempted', 0)}")
    print(f"  duplicate_group_total      : {val.get('duplicate_group_total', 0)}")
    print(f"  unique_promo_descriptions  : {len(val.get('unique_promo_descriptions') or [])}")
    print("  service_count_by_category:")
    for s, n in (val.get("service_count_by_category") or {}).items():
        print(f"     - {s}: {n}")
    print("  extraction_method_counts:")
    for m, n in (val.get("extraction_method_counts") or {}).items():
        print(f"     - {m}: {n}")
    print("  row_count_by_city:")
    for city, n in (val.get("row_count_by_city") or {}).items():
        print(f"     - {city}: {n}")
    print("  row_count_by_url:")
    for u, n in (val.get("row_count_by_url") or {}).items():
        print(f"     - [{n}] {u}")
    print("  unique_promo_descriptions:")
    for d in (val.get("unique_promo_descriptions") or []):
        print(f"     - {d!r}")
    print()

    # URL processing log
    url_log = val.get("url_log") or []
    if url_log:
        print(f"URL processing ({len(url_log)} URL(s)):")
        for e in url_log:
            print(
                f"  [{e.get('scope','?'):<10}|{e.get('status','?'):<12}] "
                f"{e.get('url','')}"
                f"  cards={e.get('cards_on_page',0)}"
                f"  added={e.get('added_rows',0)}"
                f"  excl={e.get('excluded_count',0)}"
            )
        print()

    # Sample rows
    if promos:
        print(f"Sample rows (all {len(promos)}):")
        for i, p in enumerate(promos, 1):
            nr = " [needs_review]" if p.get("needs_review") else ""
            print(
                f"  {i}. [{p['city']:<8}|{p['service_name']:<18}|"
                f"conf={p['confidence']:<6}] "
                f"d={p['discount_value']!r}  c={p['coupon_code']!r}  "
                f"exp={p['expiry_date']!r}{nr}"
            )
            print(f"     title: {p['ad_title']!r}")
            print(f"     desc : {p['promo_description']!r}")
        print()

    # Write outputs
    json_path = ROOT / "data" / "promotions" / "lubetown_v2.json"
    csv_path = ROOT / "data" / "promotions" / "lubetown_v2.csv"
    coverage_path = ROOT / "data" / "promotions" / "lubetown_v2_url_coverage.csv"
    excluded_path = ROOT / "data" / "promotions" / "lubetown_v2_excluded_rows.csv"

    export_promotions_to_csv(promos, csv_path)
    export_url_coverage(result, coverage_path, entry["competitor"])
    if val.get("excluded_rows"):
        export_excluded_rows(result, excluded_path, entry["competitor"])
        print(f"CSV (excluded): {excluded_path}")

    print(f"Saved: {json_path}")
    print(f"CSV (main):     {csv_path}")
    print(f"CSV (coverage): {coverage_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
