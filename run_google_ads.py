"""Run Google Ads Transparency Center scraper.

Fetches competitor ad listings from adstransparency.google.com for all 9
competitors using their Canada-region domain or advertiser ID.

Usage:
    python run_google_ads.py                    # all 9 competitors + push to Sheets
    python run_google_ads.py --no-push          # scrape only, no Sheets upload
    python run_google_ads.py --smoke            # first 2 competitors only (quick test)
    python run_google_ads.py --final-deduped    # collapse per-competitor duplicates
    python run_google_ads.py --competitor Midas # single competitor by name
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.google_ads_scraper import (  # noqa: E402
    scrape_google_ads, COMPETITORS, ADS_DIR,
)

_ATC_BASE = "https://adstransparency.google.com/advertiser"


def _build_url(competitor: dict) -> str:
    aid = competitor.get("advertiser_id", "")
    return f"{_ATC_BASE}/{aid}" if aid else ""

SHEET_ID = "11e3ErdYFIQ3MIOEpnLEGS4MH0s2AbSIhiQsgzQG_m88"
ADS_TAB = "Advertisements"

ADS_COLUMNS = [
    "business_name",
    "ad_title",
    "ad_description",
    "discount_value",
    "ad_link",
    "displayed_link",
    "date_scraped",
]


def export_ads_csv(ads: list, dest: Path) -> Path:
    if not ads:
        dest.write_text("", encoding="utf-8")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ADS_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for a in ads:
            w.writerow({k: str(a.get(k) or "") for k in ADS_COLUMNS})
    return dest


def push_ads_to_sheets(ads: list) -> bool:
    import sys as _sys
    for _p in [None, "/tmp/gapi"]:
        if _p:
            _sys.path.insert(0, _p)
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            break
        except ImportError:
            if _p:
                _sys.path.pop(0)
            continue
    else:
        print("ERROR: google-api-python-client not installed.")
        return False

    from app.config.constants import ROOT as PROJ_ROOT

    creds_path = PROJ_ROOT / "service_account.json"
    if not creds_path.exists():
        print(f"ERROR: service_account.json not found at {creds_path}")
        return False

    creds = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    svc = build("sheets", "v4", credentials=creds)
    sheets = svc.spreadsheets()

    # Verify tab exists
    meta = sheets.get(spreadsheetId=SHEET_ID).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if ADS_TAB not in existing:
        print(f"ERROR: Tab {ADS_TAB!r} not found in spreadsheet.")
        print(f"  Available tabs: {sorted(existing)}")
        return False

    last_col = chr(ord("A") + len(ADS_COLUMNS) - 1)  # "G" for 7 columns

    # Clear existing data rows (keep header row 1)
    sheets.values().clear(
        spreadsheetId=SHEET_ID,
        range=f"'{ADS_TAB}'!A2:{last_col}10000",
    ).execute()

    if not ads:
        print(f"[sheets] {ADS_TAB!r}: no rows to write")
        return True

    values = [[str(a.get(k) or "") for k in ADS_COLUMNS] for a in ads]
    result = sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{ADS_TAB}'!A2:{last_col}{len(values) + 1}",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    print(f"[sheets] Wrote {len(ads)} rows ({result.get('updatedCells', 0)} cells) to {ADS_TAB!r}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Google Ads Transparency Center scraper")
    p.add_argument("--smoke", action="store_true",
                   help="Run only the first 2 competitors (quick test).")
    p.add_argument("--no-push", action="store_true",
                   help="Skip pushing to Google Sheets.")
    p.add_argument("--final-deduped", dest="mode", action="store_const",
                   const="final_deduped", default="qa_expanded",
                   help="Collapse duplicate ads within each competitor.")
    p.add_argument("--competitor", metavar="NAME",
                   help="Scrape a single competitor by name (case-insensitive partial match).")
    args = p.parse_args()

    if args.competitor:
        needle = args.competitor.lower()
        competitors = [c for c in COMPETITORS if needle in c["name"].lower()]
        if not competitors:
            print(f"ERROR: No competitor matching {args.competitor!r}")
            print("Available:", ", ".join(c["name"] for c in COMPETITORS))
            return 1
    elif args.smoke:
        competitors = COMPETITORS[:2]
    else:
        competitors = COMPETITORS

    print("=" * 70)
    print("Google Ads Transparency Center scraper")
    print("=" * 70)
    print(f"Competitors : {len(competitors)}")
    for c in competitors:
        print(f"  {c['name']:<32}  {_build_url(c)}")
    print(f"\nMode: {args.mode}\n")

    t0 = time.time()
    result = scrape_google_ads(competitors, mode=args.mode)
    runtime = time.time() - t0

    ads = result.get("ads", [])
    val = result.get("validation", {})

    print("-" * 70)
    print(f"Total ads extracted : {result.get('count', 0)}")
    print(f"Runtime             : {runtime:.1f}s")
    print(f"Competitors OK      : {val.get('competitors_with_ads', 0)} / {len(competitors)}")
    print()

    print("Per-competitor results:")
    header = f"  {'Competitor':<32} {'Status':<8} {'Ads':>4}  URL"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for entry in val.get("url_log", []):
        ok = entry["status"] == "ok"
        print(
            f"  {entry['competitor']:<32} "
            f"{'OK' if ok else 'FAIL':<8} "
            f"{entry['ads_found']:>4}  "
            f"{entry['url']}"
        )
    print()

    if ads:
        print(f"Sample ads (first 5 of {len(ads)}):")
        for a in ads[:5]:
            print(f"  [{a['business_name']:<28}]")
            print(f"     title    : {a['ad_title']!r}")
            print(f"     desc     : {(a.get('ad_description') or '')[:90]!r}")
            print(f"     discount : {a.get('discount_value', '')!r}")
            print(f"     link     : {a.get('displayed_link', '')!r}")
        print()

    json_path = ADS_DIR / "google_ads.json"
    csv_path = ADS_DIR / "google_ads.csv"
    export_ads_csv(ads, csv_path)
    print(f"Saved: {json_path}")
    print(f"CSV  : {csv_path}")

    if args.no_push or args.smoke:
        if args.smoke:
            print("\n[smoke] Skipping Google Sheets push.")
        else:
            print("\n[--no-push] Skipping Google Sheets push.")
        return 0

    print(f"\nPushing to Google Sheets tab {ADS_TAB!r}...")
    ok = push_ads_to_sheets(ads)
    if ok:
        print(f"  https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    else:
        print("Google Sheets push FAILED — check errors above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
