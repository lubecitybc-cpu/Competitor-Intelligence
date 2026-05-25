"""Upload the consolidated competitor-intelligence dashboard to Google Sheets.

Reads every ``data/promotions/*_v2.json`` + ``*_v2_url_coverage.csv`` plus an
optional previous-run snapshot, then builds and pushes 8 tabs to the
spreadsheet whose ID is set in GOOGLE_SHEETS_ID (or the default baked into
``app/sheets/dashboard_uploader.py``).

Usage:
    python run_sheets_upload.py              # live upload to Google Sheets
    python run_sheets_upload.py --dry-run    # build + write local preview only

Requires ``service_account.json`` in the project root (or the
GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service-account key)
*unless* you pass --dry-run.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.sheets.dashboard_uploader import (  # noqa: E402
    SHEET_ID,
    TARGET_CITIES,
    ALL_TABS_ORDER,
    build_dashboard_data,
    render_preview,
    upload,
    write_preview,
)


def _print_build_summary(build, dry_run: bool) -> None:
    print("=" * 70)
    print("Lube City — Sheets Dashboard")
    print("=" * 70)
    print(f"Sheet ID      : {SHEET_ID}")
    print(f"Dry run       : {dry_run}")
    print(f"Last run ts   : {build.last_run_ts}")
    print(f"Runs found    : {len(build.runs)}")
    success = sum(1 for r in build.runs if r.status == "success")
    failed = sum(1 for r in build.runs if r.status in ("failed", "parse_failed"))
    partial = sum(1 for r in build.runs if r.status == "partial")
    print(f"  success={success} partial={partial} failed={failed}")
    print(f"Current rows  : {len(build.all_current)}")
    for city in TARGET_CITIES:
        n = len(build.by_city_current.get(city, []))
        print(f"  {city:<18}: {n}")
    other = sum(
        len(v) for k, v in build.by_city_current.items() if k not in TARGET_CITIES
    )
    if other:
        print(f"  (other cities)    : {other}")
    print(f"Deltas        : new={build.deltas.get('new', 0)} "
          f"updated={build.deltas.get('updated', 0)} "
          f"needs_review={build.deltas.get('needs_review', 0)}")
    print(f"Expired       : {build.expired_removed_count}")
    print(f"Duplicates    : {build.duplicates_removed_count}")
    print(f"URL coverage rows: {len(build.coverage)}")
    print()
    print("Tabs to write :")
    for t in ALL_TABS_ORDER:
        print(f"  - {t}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload Lube City competitor dashboard to Google Sheets",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the dashboard but do not call the Google Sheets API; "
             "writes data/sheets_ready/_dashboard_preview.json instead.",
    )
    parser.add_argument(
        "--sheet-id",
        default=None,
        help="Override the target spreadsheet ID for this run.",
    )
    args = parser.parse_args()

    t0 = time.time()
    build = build_dashboard_data(today=date.today())
    _print_build_summary(build, dry_run=args.dry_run)

    if args.dry_run:
        path = write_preview(build)
        print(f"Wrote local preview: {path}")
        print(f"Build time: {time.time() - t0:.1f}s")
        return 0

    sheet_id = args.sheet_id or SHEET_ID
    try:
        upload(build, sheet_id=sheet_id)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        print("\nSetup instructions:")
        print("  1. In Google Cloud Console, create a service account and")
        print("     download its JSON key.")
        print("  2. Save the key as 'service_account.json' in the project")
        print(f"     root: {ROOT / 'service_account.json'}")
        print("  3. Share the target spreadsheet with the service account's")
        print("     email address (found in client_email of the JSON file)")
        print("     as an Editor.")
        print("  4. Re-run this script (without --dry-run).")
        # Still write the preview so the user can verify structure.
        path = write_preview(build)
        print(f"\nLocal preview written for inspection: {path}")
        return 2
    except Exception as e:
        print(f"\nERROR during upload: {e}")
        path = write_preview(build)
        print(f"Local preview written for inspection: {path}")
        return 1

    print(f"Uploaded to: https://docs.google.com/spreadsheets/d/{sheet_id}")
    print(f"Total time: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
