"""Run Jiffy Lube v2 scraper (Phase 5).

Reads from app/config/competitors.v2.json and only scrapes Jiffy Lube.
Other competitors and the legacy pipeline are NOT affected.

Usage:
    python run_jiffy_v2.py                       # full run (1 national + 32 stores)
    python run_jiffy_v2.py --limit 1             # 1 store per city = 4 URLs total
    python run_jiffy_v2.py --limit 2 --no-ocr    # 7 URLs, text only
    python run_jiffy_v2.py --export-csv-only     # write jiffy_lube_v2.csv from existing JSON only
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.jiffy_scraper import scrape_jiffy_v2  # noqa: E402


def find_jiffy(config_path: Path) -> dict:
    data = json.loads(config_path.read_text())
    for entry in data:
        if entry.get("competitor", "").strip().lower() == "jiffy lube":
            return entry
    raise SystemExit("Jiffy Lube not found in competitors.v2.json")


def export_promotions_to_csv(rows: list, dest: Path) -> Path:
    """Write promotion dicts to UTF-8 CSV (sheet columns first, then extras)."""
    if not rows:
        dest.write_text("", encoding="utf-8")
        return dest
    preferred = [
        "website",
        "page_url",
        "business_name",
        "google_reviews",
        "service_name",
        "promo_description",
        "category",
        "contact",
        "location",
        "offer_details",
        "ad_title",
        "ad_text",
        "new_or_updated",
        "date_scraped",
        "city",
        "store_name",
        "source_scope",
        "extraction_method",
        "confidence",
        "needs_review",
        "discount_value",
        "coupon_code",
        "expiry_date",
        "promotion_title",
        "normalized_title",
        "applicable_cities",
        "source_image",
        "duplicate_of_national",
        "duplicate_group_id",
        "image_filename_city_hint",
        "city_decision_source",
        "city_decision_note",
        "needs_review_reason",
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
    """Per-URL coverage CSV."""
    val = result.get("validation") or {}
    url_log = val.get("url_log") or []
    excluded = val.get("excluded_rows") or []
    rc_by_url: dict = val.get("row_count_by_url") or {}
    reasons_by_url: dict = {}
    for x in excluded:
        u = x.get("url") or ""
        reasons_by_url.setdefault(u, {})
        r = x.get("reason", "unknown")
        reasons_by_url[u][r] = reasons_by_url[u].get(r, 0) + 1
    fieldnames = [
        "competitor", "city", "store_name", "url", "source_scope", "status",
        "raw_promo_count", "added_unique", "tagged_as_national_duplicate",
        "dropped_as_national_duplicate", "excluded_count", "excluded_reasons",
        "row_count_written_for_url", "text_extracted_count",
        "image_ocr_extracted_count", "image_ocr_failed_needs_review_count", "notes",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in url_log:
            u = e.get("url", "")
            er = reasons_by_url.get(u, {})
            w.writerow({
                "competitor": competitor,
                "city": e.get("city") or "",
                "store_name": e.get("store_name") or "",
                "url": u,
                "source_scope": e.get("scope", ""),
                "status": e.get("status", ""),
                "raw_promo_count": e.get("raw_promo_count", 0),
                "added_unique": e.get("added_unique", 0),
                "tagged_as_national_duplicate": e.get("tagged_as_national_duplicate", 0),
                "dropped_as_national_duplicate": e.get("dropped_as_national_duplicate", 0),
                "excluded_count": e.get("excluded_count", 0),
                "excluded_reasons": json.dumps(er, ensure_ascii=False) if er else "",
                "row_count_written_for_url": rc_by_url.get(u, 0),
                "text_extracted_count": e.get("text_extracted_count", 0),
                "image_ocr_extracted_count": e.get("image_ocr_extracted_count", 0),
                "image_ocr_failed_needs_review_count": e.get("image_ocr_failed_needs_review_count", 0),
                "notes": "",
            })
    return dest


def export_excluded_rows_csv(result: dict, dest: Path, competitor: str) -> Path:
    val = result.get("validation") or {}
    excluded = val.get("excluded_rows") or []
    url_log = val.get("url_log") or []
    meta_by_url = {e["url"]: e for e in url_log}
    fieldnames = [
        "competitor", "city", "store_name", "url", "source_scope",
        "extraction_method", "reason", "source_image", "raw_text",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for x in excluded:
            u = x.get("url", "")
            m = meta_by_url.get(u, {})
            w.writerow({
                "competitor": competitor,
                "city": m.get("city") or "",
                "store_name": m.get("store_name") or "",
                "url": u,
                "source_scope": x.get("scope") or m.get("scope", ""),
                "extraction_method": x.get("extraction_method", ""),
                "reason": x.get("reason", ""),
                "source_image": x.get("source_image", ""),
                "raw_text": (x.get("snippet") or "")[:1000],
            })
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Jiffy Lube v2 scraper")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit stores scraped per city (None = all). E.g. --limit 1 → 1 store per city.",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Disable the image-OCR safety net (text-only run).",
    )
    parser.add_argument(
        "--export-csv-only",
        action="store_true",
        help="Read data/promotions/jiffy_lube_v2.json and write jiffy_lube_v2.csv (no scrape).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--qa-expanded",
        dest="mode",
        action="store_const",
        const="qa_expanded",
        help="QA mode (default): keep every per-URL row; tag national duplicates instead of dropping.",
    )
    mode_group.add_argument(
        "--final-deduped",
        dest="mode",
        action="store_const",
        const="final_deduped",
        help="Client-delivery mode: collapse store rows that duplicate national coupons.",
    )
    parser.set_defaults(mode="qa_expanded")
    args = parser.parse_args()

    json_path = ROOT / "data" / "promotions" / "jiffy_lube_v2.json"
    csv_path = ROOT / "data" / "promotions" / "jiffy_lube_v2.csv"

    if args.export_csv_only:
        if not json_path.exists():
            print(f"❌ Not found: {json_path}")
            return 1
        data = json.loads(json_path.read_text())
        rows = data.get("promotions", [])
        export_promotions_to_csv(rows, csv_path)
        print(f"✅ Wrote {len(rows)} rows → {csv_path}")
        return 0

    config_path = ROOT / "app" / "config" / "competitors.v2.json"
    if not config_path.exists():
        print(f"❌ Config not found: {config_path}")
        return 1

    jiffy_entry = find_jiffy(config_path)

    total_national = len(jiffy_entry.get("promo_links", []))
    total_stores = sum(len(v) for v in (jiffy_entry.get("store_links") or {}).values())
    picked_stores = (
        total_stores
        if args.limit is None
        else sum(min(args.limit, len(v)) for v in (jiffy_entry.get("store_links") or {}).values())
    )

    print("=" * 70)
    print("Jiffy Lube v2 — Phase 5 run")
    print("=" * 70)
    print(f"Competitor : {jiffy_entry.get('competitor')}")
    print(f"Cities     : {', '.join(jiffy_entry.get('cities', []))}")
    print(f"National   : {total_national} URL(s)")
    print(f"Stores     : {picked_stores}/{total_stores} (limit={args.limit})")
    print(f"OCR        : {'OFF' if args.no_ocr else 'ON'}")
    print(f"Mode       : {args.mode}")
    print()

    t0 = time.time()
    result = scrape_jiffy_v2(
        jiffy_entry,
        limit_stores_per_city=args.limit,
        enable_ocr=not args.no_ocr,
        mode=args.mode,
    )
    runtime = time.time() - t0

    if result.get("error"):
        print(f"❌ Error: {result['error']}")
        return 1

    print()
    print("-" * 70)
    print(f"Total rows  : {result.get('count', 0)}")
    print(f"  National  : {result.get('national_unique_count', 0)} unique × cities → fanned out")
    print(f"  Store-only: {result.get('store_only_unique_count', 0)}")
    by_city = result.get("by_city") or {}
    for city, n in by_city.items():
        print(f"  {city:<18}: {n} rows")
    by_scope = result.get("by_source_scope") or {}
    print(f"  By source_scope: national={by_scope.get('national', 0)}  store={by_scope.get('store', 0)}")
    print(f"needs_review: {result.get('needs_review_count', 0)} row(s)")
    print()

    val = result.get("validation") or {}
    if val:
        print("Validation")
        print(f"  Runtime       : {runtime:.1f}s")
        print(f"  Expected URLs : {val.get('expected_url_count', 0)}")
        print(f"  Processed URLs: {val.get('processed_url_count', 0)}")
        print(f"  Failed URLs   : {val.get('failed_url_count', 0)}")
        print(f"  No-section URL: {val.get('no_section_url_count', 0)}")
        missing = val.get("missing_urls") or []
        if missing:
            print(f"  Missing URLs ({len(missing)}):")
            for u in missing[:10]:
                print(f"     - {u}")
            if len(missing) > 10:
                print(f"     ... and {len(missing) - 10} more")
        else:
            print("  Missing URLs : none")
        failed = val.get("failed_urls") or []
        if failed:
            print(f"  Failed URLs ({len(failed)}):")
            for u in failed[:10]:
                print(f"     - {u}")
        excl_counts = val.get("excluded_reason_counts") or {}
        print(f"  Excluded rows : {val.get('excluded_row_count', 0)}")
        for reason, n in sorted(excl_counts.items(), key=lambda kv: -kv[1]):
            print(f"     - {reason}: {n}")
        print(f"  duplicate_of_national rows: {val.get('duplicate_of_national_count', 0)}")
        print(f"  store_unique rows         : {val.get('store_unique_count', 0)}")
        rc_by_city = val.get("row_count_by_city") or {}
        if rc_by_city:
            print("  row_count_by_city:")
            for city, n in sorted(rc_by_city.items(), key=lambda kv: -kv[1]):
                print(f"     - {city or '(none)'}: {n}")
        rc_by_url = val.get("row_count_by_url") or {}
        if rc_by_url:
            zero_urls = [u for u in (e["url"] for e in (val.get("url_log") or [])) if rc_by_url.get(u, 0) == 0]
            print(f"  URLs with 0 rows: {len(zero_urls)}")
            for u in zero_urls[:10]:
                print(f"     - {u}")
        print(
            f"  OCR: attempted={val.get('ocr_attempted', 0)} "
            f"success={val.get('ocr_success', 0)} "
            f"failed={val.get('ocr_failed', 0)} "
            f"skipped_prefilter={val.get('ocr_skipped_prefilter', 0)}"
        )
        ocr_counts = val.get("ocr_status_counts") or {}
        for s, n in sorted(ocr_counts.items(), key=lambda kv: -kv[1]):
            print(f"     - {s}: {n}")
        excl_rows = val.get("excluded_rows") or []
        if excl_rows:
            print("  Excluded samples:")
            for x in excl_rows[:5]:
                snippet = (x.get("snippet") or x.get("source_image") or "")[:90]
                print(f"     - [{x.get('scope','?'):<8}|{x.get('extraction_method','?'):<9}] {x.get('reason')}: {snippet}")
        print()

    # Show per-URL processing log (compact)
    url_log = (val or {}).get("url_log") or []
    if url_log:
        print(f"URL processing ({len(url_log)} URLs):")
        for entry in url_log:
            scope = entry.get("scope", "?")
            tag = entry.get("store_name") or entry.get("city") or "national"
            added = entry.get("added_unique", 0)
            excl = entry.get("excluded_count", 0)
            dropped = entry.get("dropped_as_national_duplicate", 0)
            tagged = entry.get("tagged_as_national_duplicate", 0)
            status = entry.get("status", "ok")
            extra = ""
            if scope == "store":
                extra = f", nat_dup_tagged={tagged}, nat_dropped={dropped}"
            print(f"  [{scope:<8}|{status:<13}] {tag[:38]:<38}  +{added} rows, excluded={excl}{extra}")
        print()

    promos = result.get("promotions", [])
    if promos:
        print("Sample rows (first 5):")
        for i, p in enumerate(promos[:5], 1):
            print(
                f"  {i}. [{p.get('city'):<14}|{p.get('source_scope'):<8}|{p.get('extraction_method'):<9}|conf={p.get('confidence'):<6}] "
                f"{p.get('service_name'):<16} | {p.get('ad_title','')[:70]}"
            )
            print(
                f"     discount={p.get('discount_value')!r}  code={p.get('coupon_code')!r}  expiry={p.get('expiry_date')!r}  store={p.get('store_name')!r}"
            )
        if len(promos) > 5:
            print(f"  ... and {len(promos) - 5} more")
        print()

    nr_rows = [p for p in promos if p.get("needs_review")]
    if nr_rows:
        print(f"needs_review rows ({len(nr_rows)}):")
        for p in nr_rows[:5]:
            print(f"   - [{p.get('city')}|{p.get('source_scope')}] {p.get('ad_title','')[:80]}")
        print()

    out_path = Path("data/promotions/jiffy_lube_v2.json")
    print(f"Saved: {out_path}")
    export_promotions_to_csv(promos, csv_path)
    print(f"CSV (main):     {csv_path}")
    coverage_path = ROOT / "data" / "promotions" / "jiffy_lube_v2_url_coverage.csv"
    excluded_path = ROOT / "data" / "promotions" / "jiffy_lube_v2_excluded_rows.csv"
    export_url_coverage_csv(result, coverage_path, jiffy_entry.get("competitor", "Jiffy Lube"))
    export_excluded_rows_csv(result, excluded_path, jiffy_entry.get("competitor", "Jiffy Lube"))
    print(f"CSV (coverage): {coverage_path}")
    print(f"CSV (excluded): {excluded_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
