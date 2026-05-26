"""Google Sheets writer — writes merged city-split promotions to the live sheet.

Target spreadsheet: https://docs.google.com/spreadsheets/d/11e3ErdYFIQ3MIOEpnLEGS4MH0s2AbSIhiQsgzQG_m88

Tabs written:
    "Edmonton Promos"      — rows where city == "Edmonton"
    "Calgary Promos"       — rows where city == "Calgary"
    "Grande Prairie Promos"— rows where city == "Grande Prairie"
    "Advertisements"       — all rows combined (master tab)

The service account sheet-writer@lubecity-competitor-intel.iam.gserviceaccount.com
must be added as an Editor on the spreadsheet before this will work.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

_ENV = Path(__file__).resolve().parents[2] / ".env"
if _ENV.exists():
    load_dotenv(_ENV, override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SHEET_ID = "11e3ErdYFIQ3MIOEpnLEGS4MH0s2AbSIhiQsgzQG_m88"

# Tab name → city filter (None = write all rows).
# Tab names are exactly as they appear in the spreadsheet (note leading space on Grande Prairie).
CITY_TABS: List[Tuple[str, Optional[str]]] = [
    ("Advertisements",         None),           # master tab — all cities
    ("Edmonton Promos",        "Edmonton"),
    ("Calgary Promos",         "Calgary"),
    (" Grande Prairie Promos", "Grande Prairie"),  # leading space is intentional
]

# 14 columns written to every tab, in this exact order.
SHEET_COLUMNS = [
    "website",
    "page_url",
    "business_name",
    "google_reviews",
    "service_name",
    "promo_description",
    "category",
    "offer_details",
    "ad_title",
    "ad_text",
    "new_or_updated",
    "date_scraped",
    "city",
    "extraction_method",
]

# Last column letter for the 14-column range (A=1 … N=14).
_LAST_COL = "N"

from app.config.constants import ROOT
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "google_sheets_writer.log")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_service():
    """Build and return an authenticated Sheets v4 service object."""
    # Prefer installed package; fall back to /tmp/gapi used during setup.
    for path in [None, "/tmp/gapi"]:
        if path:
            sys.path.insert(0, path)
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            break
        except ImportError:
            if path:
                sys.path.pop(0)
            continue
    else:
        raise ImportError(
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth"
        )

    creds_path = ROOT / "service_account.json"
    if not creds_path.exists():
        env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if env_path:
            creds_path = Path(env_path)
        else:
            raise FileNotFoundError(
                "service_account.json not found in project root. "
                "Set GOOGLE_APPLICATION_CREDENTIALS or place the file there."
            )

    creds = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def discover_tabs(sheet_id: str = SHEET_ID) -> Dict[str, int]:
    """Return {tab_name: sheet_id} for every tab in the spreadsheet."""
    svc = _get_service()
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in meta.get("sheets", [])
    }


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _row_to_list(row: Dict) -> List:
    """Convert a promo dict to an ordered list matching SHEET_COLUMNS."""
    return [str(row.get(col) or "") for col in SHEET_COLUMNS]


def _clear_and_write(
    svc,
    sheet_id: str,
    tab_name: str,
    rows: List[Dict],
) -> int:
    """Clear data rows (keep header row 1), then write *rows* starting at A2.

    Returns the number of cells updated.
    """
    sheets = svc.spreadsheets()

    # Clear A2:R10000 (preserve the header in row 1).
    clear_range = f"'{tab_name}'!A2:{_LAST_COL}10000"
    sheets.values().clear(
        spreadsheetId=sheet_id,
        range=clear_range,
    ).execute()
    logger.info(f"[sheets] Cleared {tab_name!r}")

    if not rows:
        logger.info(f"[sheets] {tab_name!r}: no rows to write")
        return 0

    values = [_row_to_list(r) for r in rows]
    write_range = f"'{tab_name}'!A2:{_LAST_COL}{len(values) + 1}"
    result = sheets.values().update(
        spreadsheetId=sheet_id,
        range=write_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    updated = result.get("updatedCells", 0)
    logger.info(f"[sheets] {tab_name!r}: wrote {len(rows)} rows ({updated} cells)")
    return updated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_city_tabs(
    all_rows: List[Dict],
    sheet_id: str = SHEET_ID,
    city_tabs: Optional[List[Tuple[str, Optional[str]]]] = None,
) -> bool:
    """Write *all_rows* to each city tab defined in *city_tabs*.

    Each tab receives only the rows whose city field matches the tab's city
    filter.  The "Advertisements" tab (filter=None) receives all rows.

    Args:
        all_rows:  Full merged promo row list (already deduplicated).
        sheet_id:  Target spreadsheet ID.
        city_tabs: List of (tab_name, city_or_None) tuples.

    Returns:
        True on full success, False if any tab write failed.
    """
    if city_tabs is None:
        city_tabs = CITY_TABS

    try:
        svc = _get_service()
    except Exception as exc:
        logger.error(f"[sheets] Failed to initialise Sheets service: {exc}")
        return False

    # Verify the spreadsheet is accessible and discover existing tabs.
    try:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        logger.info(
            f"[sheets] Connected to {meta['properties']['title']!r}. "
            f"Tabs: {sorted(existing)}"
        )
    except Exception as exc:
        logger.error(
            f"[sheets] Cannot access spreadsheet {sheet_id}: {exc}\n"
            "Make sure sheet-writer@lubecity-competitor-intel.iam.gserviceaccount.com "
            "is added as an Editor on the spreadsheet."
        )
        return False

    success = True
    for tab_name, city_filter in city_tabs:
        if tab_name not in existing:
            logger.warning(
                f"[sheets] Tab {tab_name!r} not found in spreadsheet — skipping. "
                f"Available: {sorted(existing)}"
            )
            success = False
            continue

        if city_filter is None:
            tab_rows = all_rows
        else:
            tab_rows = [r for r in all_rows if (r.get("city") or "").strip() == city_filter]

        try:
            _clear_and_write(svc, sheet_id, tab_name, tab_rows)
        except Exception as exc:
            logger.error(f"[sheets] Error writing to {tab_name!r}: {exc}", exc_info=True)
            success = False

    return success


def write_from_file(
    merged_file: Optional[Path] = None,
    sheet_id: str = SHEET_ID,
) -> bool:
    """Load rows from the merged JSON file and write to all city tabs."""
    if merged_file is None:
        merged_file = (
            Path(__file__).resolve().parents[2]
            / "data" / "sheets_ready" / "promotions_merged_for_sheets.json"
        )

    if not merged_file.exists():
        logger.error(f"[sheets] Merged file not found: {merged_file}")
        return False

    data = json.loads(merged_file.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    logger.info(f"[sheets] Loaded {len(rows)} rows from {merged_file.name}")
    return write_city_tabs(rows, sheet_id=sheet_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Push merged promotions to Google Sheets")
    p.add_argument("--sheet-id", default=SHEET_ID)
    p.add_argument("--file", type=Path, help="Path to merged JSON file (optional).")
    p.add_argument("--discover-tabs", action="store_true",
                   help="Just print tab names and exit.")
    args = p.parse_args()

    if args.discover_tabs:
        try:
            tabs = discover_tabs(args.sheet_id)
            print(f"Tabs in {args.sheet_id}:")
            for name, gid in tabs.items():
                print(f"  gid={gid:<12} {name!r}")
        except Exception as e:
            print(f"ERROR: {e}")
        raise SystemExit(0)

    ok = write_from_file(args.file, sheet_id=args.sheet_id)
    raise SystemExit(0 if ok else 1)
