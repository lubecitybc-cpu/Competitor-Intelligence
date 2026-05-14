"""Run Great Canadian Oil Change v2 scraper (Phase 5).

Reads from ``app/config/competitors.v2.json`` and only scrapes Great
Canadian Oil Change. Other competitors and the legacy pipeline are NOT
affected.

Usage:
    python run_gcoc_v2.py                  # default (--qa-expanded)
    python run_gcoc_v2.py --final-deduped  # collapse cross-URL duplicates
    python run_gcoc_v2.py --no-ocr         # text-only run (skip image OCR)
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.gcoc_scraper import scrape_gcoc_v2  # noqa: E402


CSV_PREFERRED = [
    # Sheet-compatible columns
    "website", "page_url", "business_name", "google_reviews", "service_name",
    "promo_description", "category", "contact", "location", "offer_details",
    "ad_title", "ad_text", "new_or_updated", "date_scraped",
    # QA / meta columns
    "city", "store_name", "source_scope", "extraction_method", "confidence",
    "needs_review", "needs_review_reason", "discount_value", "coupon_code",
    "expiry_date", "promotion_title", "normalized_title", "applicable_cities",
    "duplicate_group_id", "duplicate_group_total", "source_image",
]


def find_gcoc(config_path: Path) -> dict:
    data = json.loads(config_path.read_text())
    for entry in data:
        if entry.get("competitor", "").strip().lower() == "great canadian oil change":
            return entry
    raise SystemExit("Great Canadian Oil Change not found in competitors.v2.json")


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
        "competitor", "url", "scope", "service_hint", "status",
        "cards_on_page", "text_extracted_count", "image_ocr_extracted_count",
        "image_ocr_failed_needs_review_count",
        "ocr_attempted", "ocr_success", "ocr_failed",
        "added_rows", "excluded_count", "row_count_written",
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
                "service_hint": e.get("service_hint", ""),
                "status": e.get("status", ""),
                "cards_on_page": e.get("cards_on_page", 0),
                "text_extracted_count": e.get("text_extracted_count", 0),
                "image_ocr_extracted_count": e.get("image_ocr_extracted_count", 0),
                "image_ocr_failed_needs_review_count":
                    e.get("image_ocr_failed_needs_review_count", 0),
                "ocr_attempted": e.get("ocr_attempted", 0),
                "ocr_success": e.get("ocr_success", 0),
                "ocr_failed": e.get("ocr_failed", 0),
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
    fields = [
        "competitor", "url", "scope", "extraction_method", "reason",
        "source_image", "raw_text",
    ]
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
                "source_image": x.get("source_image", ""),
                "raw_text": (x.get("raw_text") or "")[:1000],
            })
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Great Canadian Oil Change v2 scraper")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--qa-expanded", dest="mode", action="store_const",
                   const="qa_expanded",
                   help="QA mode (default): keep every per-URL row; tag duplicates.")
    g.add_argument("--final-deduped", dest="mode", action="store_const",
                   const="final_deduped",
                   help="Collapse cross-URL duplicates per city for client delivery.")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Skip image OCR; text-only run.")
    parser.set_defaults(mode="qa_expanded")
    args = parser.parse_args()

    cfg = ROOT / "app" / "config" / "competitors.v2.json"
    if not cfg.exists():
        print(f"Config not found: {cfg}")
        return 1
    entry = find_gcoc(cfg)

    print("=" * 70)
    print("Great Canadian Oil Change v2 - Phase 5 run")
    print("=" * 70)
    print(f"Competitor : {entry['competitor']}")
    print(f"Cities     : {', '.join(entry.get('cities', []))}")
    print(f"URLs       : {len(entry.get('promo_links', []))}")
    print(f"Mode       : {args.mode}")
    print(f"OCR        : {'OFF' if args.no_ocr else 'ON'}")
    print()

    t0 = time.time()
    result = scrape_gcoc_v2(entry, mode=args.mode, enable_ocr=not args.no_ocr)
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
    for u in (val.get("failed_urls") or []):
        print(f"     - {u}")
    missing = val.get("missing_urls") or []
    print(f"  missing_urls               : {missing}")
    print(f"  needs_review_count         : {val.get('needs_review_count', 0)}")
    print(f"  excluded_row_count         : {val.get('excluded_row_count', 0)}")
    for r, n in (val.get("excluded_reason_counts") or {}).items():
        print(f"     - {r}: {n}")
    print(f"  duplicate_group_total      : {val.get('duplicate_group_total', 0)}")
    print(f"  unique_promo_descriptions  : {len(val.get('unique_promo_descriptions') or [])}")
    print(
        f"  OCR: attempted={val.get('ocr_attempted', 0)} "
        f"success={val.get('ocr_success', 0)} failed={val.get('ocr_failed', 0)}"
    )
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
        print(f"     - [{n:>3}] {u}")
    print()

    url_log = val.get("url_log") or []
    if url_log:
        print(f"URL processing ({len(url_log)} URL(s)):")
        for e in url_log:
            print(
                f"  [{e.get('service_hint','?'):<20}|{e.get('status','?'):<13}] "
                f"text={e.get('text_extracted_count', 0):<2} "
                f"img_ocr={e.get('image_ocr_extracted_count', 0):<2} "
                f"img_ocr_fail={e.get('image_ocr_failed_needs_review_count', 0):<2} "
                f"excl={e.get('excluded_count', 0):<3} "
                f"+{e.get('added_rows', 0)} rows  {e.get('url','')}"
            )
        print()

    if promos:
        print(f"Sample rows (first 5 of {len(promos)}):")
        for i, p in enumerate(promos[:5], 1):
            nr = " [needs_review]" if p.get("needs_review") else ""
            print(
                f"  {i}. [{p.get('city'):<14}|{p.get('service_name'):<18}|"
                f"{p.get('extraction_method'):<17}|conf={p.get('confidence')!s:<5}] "
                f"d={p.get('discount_value')!r} c={p.get('coupon_code')!r} "
                f"exp={p.get('expiry_date')!r}{nr}"
            )
            print(f"     title: {p.get('ad_title','')[:90]}")
            print(f"     desc : {p.get('promo_description','')[:90]}")
        print()

    json_path = ROOT / "data" / "promotions" / "gcoc_v2.json"
    csv_path = ROOT / "data" / "promotions" / "gcoc_v2.csv"
    coverage_path = ROOT / "data" / "promotions" / "gcoc_v2_url_coverage.csv"
    excluded_path = ROOT / "data" / "promotions" / "gcoc_v2_excluded_rows.csv"

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
