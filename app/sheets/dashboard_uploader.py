"""Dashboard uploader for the Lube City competitor-intelligence Google Sheet.

Builds 8 tabs from the v2 scraper outputs under ``data/promotions/``:

    1. Dashboard            — high-level summary metrics + colored cards
    2. Edmonton             — final current offers where city == "Edmonton"
    3. Calgary              — final current offers where city == "Calgary"
    4. Grande Prairie       — final current offers where city == "Grande Prairie"
    5. All Current Offers   — master table across all cities
    6. Run Summary          — one row per competitor's last run
    7. Expired / Removed    — expired + duplicate + scraper-failed + outside-scope
    8. URL Coverage         — concatenated <competitor>_v2_url_coverage.csv

Run via ``run_sheets_upload.py``.

Required:
    service_account.json in project root (or GOOGLE_APPLICATION_CREDENTIALS env)
    with Editor access to the spreadsheet identified by SHEET_ID.

Snapshot file (for delta computation):
    data/sheets_ready/_dashboard_snapshot.json
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app.config.constants import ROOT
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "dashboard_uploader.log")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SHEET_ID = os.getenv(
    "GOOGLE_SHEETS_ID", "15vOEjTo4bNSZsWmMA2ilPp44PbMie14P1hWFtIKO_B8"
)

PROMOTIONS_DIR = ROOT / "data" / "promotions"
SHEETS_READY_DIR = ROOT / "data" / "sheets_ready"
SHEETS_READY_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_FILE = SHEETS_READY_DIR / "_dashboard_snapshot.json"
PREVIEW_FILE = SHEETS_READY_DIR / "_dashboard_preview.json"

TARGET_CITIES = ("Edmonton", "Calgary", "Grande Prairie")

# Sheet-compatible columns first, then QA / meta columns. This is the exact
# schema documented in the project context.
SCHEMA_COLUMNS: List[str] = [
    "website", "page_url", "business_name", "google_reviews", "service_name",
    "promo_description", "category", "contact", "location", "offer_details",
    "ad_title", "ad_text", "new_or_updated", "date_scraped",
    "city", "store_name", "source_scope", "extraction_method", "confidence",
    "needs_review", "needs_review_reason", "discount_value", "coupon_code",
    "expiry_date", "promotion_title", "normalized_title", "applicable_cities",
    "duplicate_group_id", "duplicate_group_total", "source_image",
]

# Helper columns we append after the schema for color/diff logic. They are
# *not* part of the canonical schema but they let us drive conditional
# formatting cleanly.
HELPER_COLUMNS: List[str] = ["_change_status"]

CITY_TAB_NAMES = {
    "Edmonton": "Edmonton",
    "Calgary": "Calgary",
    "Grande Prairie": "Grande Prairie",
}
TAB_DASHBOARD = "Dashboard"
TAB_ALL = "All Current Offers"
TAB_RUN_SUMMARY = "Run Summary"
TAB_EXPIRED = "Expired / Removed"
TAB_URL_COVERAGE = "URL Coverage"

ALL_TABS_ORDER = [
    TAB_DASHBOARD,
    "Edmonton", "Calgary", "Grande Prairie",
    TAB_ALL, TAB_RUN_SUMMARY, TAB_EXPIRED, TAB_URL_COVERAGE,
]

# Colors (Google Sheets uses 0..1 floats).
COLOR_HEADER_BG = {"red": 0.20, "green": 0.27, "blue": 0.36}
COLOR_HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}
COLOR_LIGHT_GREEN = {"red": 0.85, "green": 0.95, "blue": 0.85}
COLOR_LIGHT_YELLOW = {"red": 1.0, "green": 0.96, "blue": 0.78}
COLOR_LIGHT_ORANGE = {"red": 1.0, "green": 0.87, "blue": 0.73}
COLOR_LIGHT_RED = {"red": 0.99, "green": 0.85, "blue": 0.85}
COLOR_DASH_CARD = {"red": 0.93, "green": 0.95, "blue": 1.00}
COLOR_DASH_CARD_ALT = {"red": 0.95, "green": 1.00, "blue": 0.95}
COLOR_DASH_TITLE_BG = {"red": 0.16, "green": 0.21, "blue": 0.32}

# Map from a competitor name to the runner script that produced the file.
RUN_SCRIPTS: Dict[str, str] = {
    "Jiffy Lube":               "run_jiffy_v2.py",
    "Midas":                    "run_midas_v2.py",
    "Quick Lane Tire & Auto Center": "run_quicklane_v2.py",
    "Lube Town":                "run_lubetown_v2.py",
    "Valvoline Express Care":   "run_valvoline.py",
    "Great Canadian Oil Change": "run_gcoc_v2.py",
    "Mr. Lube + Tires":         "run_mrlube_v2.py",
    "LubeFx Plus":              "run_lubefx_v2.py",
    "Mobil 1 Lube Express":     "run_mobil1_lube_express_v2.py",
    "Econo Lube":               "run_econolube_v2.py",
    "Pit Stop Oil Change":      "run_pitstop_v2.py",
}

JSON_FILENAME_TO_COMPETITOR_GUESS: Dict[str, str] = {
    "jiffy_v2":             "Jiffy Lube",
    "midas_v2":             "Midas",
    "quicklane_v2":         "Quick Lane Tire & Auto Center",
    "lubetown_v2":          "Lube Town",
    "valvoline_v2":         "Valvoline Express Care",
    "gcoc_v2":              "Great Canadian Oil Change",
    "mrlube_v2":            "Mr. Lube + Tires",
    "lubefx_v2":            "LubeFx Plus",
    "mobil1_lube_express_v2": "Mobil 1 Lube Express",
    "econolube_v2":         "Econo Lube",
    "pitstop_v2":           "Pit Stop Oil Change",
}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class RunInfo:
    competitor: str
    json_path: Path
    status: str
    rows_generated: int = 0
    current_rows_kept: int = 0
    expired_rows_removed: int = 0
    duplicate_rows_removed: int = 0
    needs_review_count: int = 0
    error_message: str = ""
    run_time_seconds: Optional[float] = None
    scraped_at: str = ""
    promotions: List[Dict] = field(default_factory=list)
    excluded_rows: List[Dict] = field(default_factory=list)
    url_log: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _guess_competitor(stem: str, fallback: str = "Unknown") -> str:
    return JSON_FILENAME_TO_COMPETITOR_GUESS.get(stem, fallback)


def load_v2_outputs(promotions_dir: Path = PROMOTIONS_DIR) -> List[RunInfo]:
    runs: List[RunInfo] = []
    for json_path in sorted(promotions_dir.glob("*_v2.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to parse {json_path}: {e}")
            runs.append(RunInfo(
                competitor=_guess_competitor(json_path.stem),
                json_path=json_path,
                status="parse_failed",
                error_message=str(e),
            ))
            continue

        competitor = data.get("competitor") or _guess_competitor(json_path.stem)
        promotions = data.get("promotions") or []
        validation = data.get("validation") or {}
        expected = validation.get("expected_url_count", 0)
        processed = validation.get("processed_url_count", 0)
        failed = validation.get("failed_url_count", 0)
        status = (
            "success" if (failed == 0 and processed >= expected and expected > 0)
            else "partial" if processed > 0 else "failed"
        )
        runs.append(RunInfo(
            competitor=competitor,
            json_path=json_path,
            status=status,
            rows_generated=len(promotions),
            current_rows_kept=0,  # filled later (post-dedup, post-expiry)
            expired_rows_removed=0,
            duplicate_rows_removed=0,
            needs_review_count=validation.get("needs_review_count", 0),
            scraped_at=data.get("scraped_at", ""),
            promotions=promotions,
            excluded_rows=validation.get("excluded_rows") or [],
            url_log=validation.get("url_log") or [],
        ))
    return runs


def load_url_coverage_csvs(promotions_dir: Path = PROMOTIONS_DIR) -> List[Dict]:
    """Concatenate every <competitor>_v2_url_coverage.csv file, tagging each
    row with the source file and the inferred competitor name."""
    out: List[Dict] = []
    for csv_path in sorted(promotions_dir.glob("*_v2_url_coverage.csv")):
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    enriched = dict(row)
                    enriched["competitor"] = row.get(
                        "competitor"
                    ) or _guess_competitor(csv_path.stem.replace("_url_coverage", ""))
                    enriched["source_file"] = csv_path.name
                    out.append(enriched)
        except Exception as e:
            logger.warning(f"Failed to read {csv_path}: {e}")
    return out


def load_snapshot() -> Dict[str, Dict]:
    """Return previous run's snapshot keyed by row identity, or empty dict."""
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_snapshot(snapshot: Dict[str, Dict]) -> None:
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2, default=str))


# ---------------------------------------------------------------------------
# Transformations
# ---------------------------------------------------------------------------
def _row_key(row: Dict) -> str:
    """Stable identity for a current row across runs.

    Use competitor + duplicate_group_id + city + page_url so that the same
    coupon appearing on the same URL for the same city is recognized across
    runs even if minor wording changes.
    """
    return "|".join([
        str(row.get("business_name") or ""),
        str(row.get("duplicate_group_id") or ""),
        str(row.get("city") or ""),
        str(row.get("page_url") or ""),
    ])


def _content_fingerprint(row: Dict) -> str:
    return "|".join([
        str(row.get("discount_value") or ""),
        str(row.get("coupon_code") or ""),
        str(row.get("expiry_date") or ""),
        str(row.get("promotion_title") or ""),
        str(row.get("promo_description") or ""),
        str(row.get("service_name") or ""),
    ])


def final_dedupe(rows: List[Dict]) -> Tuple[List[Dict], int]:
    """Keep one row per (duplicate_group_id, city). Returns (kept, removed).

    A row without a duplicate_group_id is always kept (unique by definition).
    """
    kept: List[Dict] = []
    seen: set = set()
    removed = 0
    for r in rows:
        gid = r.get("duplicate_group_id") or ""
        city = r.get("city") or ""
        if not gid:
            kept.append(r)
            continue
        key = (gid, city)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(r)
    return kept, removed


_DATE_PATTERNS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
    "%B %d", "%b %d",
]


def _parse_expiry(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    s = str(raw).strip().rstrip(".,")
    for fmt in _DATE_PATTERNS:
        try:
            d = datetime.strptime(s, fmt).date()
            if d.year == 1900:
                d = d.replace(year=date.today().year)
            return d
        except ValueError:
            continue
    return None


def classify_expired(rows: List[Dict], *, today: date) -> Tuple[List[Dict], List[Dict]]:
    """Split rows into (current, expired) based on expiry_date < today."""
    current: List[Dict] = []
    expired: List[Dict] = []
    for r in rows:
        exp = _parse_expiry(r.get("expiry_date"))
        if exp is not None and exp < today:
            expired.append(r)
        else:
            current.append(r)
    return current, expired


def compute_change_status(
    current_rows: List[Dict], previous_snapshot: Dict[str, Dict],
) -> List[Dict]:
    """Stamp each current row with _change_status: new/updated/current/needs_review."""
    out: List[Dict] = []
    for r in current_rows:
        r2 = dict(r)
        if r.get("needs_review"):
            r2["_change_status"] = "needs_review"
        else:
            key = _row_key(r)
            prev = previous_snapshot.get(key)
            if prev is None:
                r2["_change_status"] = "new"
            elif prev.get("content_fingerprint") != _content_fingerprint(r):
                r2["_change_status"] = "updated"
            else:
                r2["_change_status"] = "current"
        out.append(r2)
    return out


def detect_expired_from_snapshot(
    current_rows: List[Dict], previous_snapshot: Dict[str, Dict],
) -> List[Dict]:
    """Rows that were in the previous snapshot but are absent now."""
    current_keys = {_row_key(r) for r in current_rows}
    expired: List[Dict] = []
    for key, snap in previous_snapshot.items():
        if key not in current_keys:
            # Reconstruct a minimal expired row from the snapshot.
            r = dict(snap.get("row") or {})
            if r:
                expired.append(r)
    return expired


# ---------------------------------------------------------------------------
# Tab builders — return (headers, rows-as-list-of-list, optional metadata)
# ---------------------------------------------------------------------------
def _row_to_list(row: Dict, headers: List[str]) -> List[str]:
    out: List[str] = []
    for h in headers:
        v = row.get(h)
        if v is None:
            out.append("")
        elif isinstance(v, (list, dict)):
            out.append(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, bool):
            out.append("TRUE" if v else "FALSE")
        else:
            out.append(str(v))
    return out


def _by(rows: List[Dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "")
        out[k] = out.get(k, 0) + 1
    return out


def build_dashboard_grid(
    *,
    runs: List[RunInfo],
    all_current: List[Dict],
    deltas: Dict[str, int],
    duplicates_removed: int,
    expired_removed: int,
    last_run_ts: str,
) -> List[List[str]]:
    """Build the Dashboard tab as a 2D string grid (column A and B mostly)."""
    successful = sum(1 for r in runs if r.status == "success")
    failed = sum(1 for r in runs if r.status in ("failed", "parse_failed"))
    needs_review = sum(1 for r in all_current if r.get("needs_review"))

    by_city = _by(all_current, "city")
    by_service = _by(all_current, "service_name")
    by_competitor = _by(all_current, "business_name")

    grid: List[List[str]] = []
    grid.append(["Lube City Competitor Intelligence — Dashboard"])
    grid.append([f"Last run: {last_run_ts}"])
    grid.append([])
    grid.append(["Metric", "Value"])
    grid.append(["Total current offers", str(len(all_current))])
    grid.append(["New offers this run", str(deltas.get("new", 0))])
    grid.append(["Updated offers this run", str(deltas.get("updated", 0))])
    grid.append(["Expired offers removed", str(expired_removed)])
    grid.append(["Duplicate rows removed", str(duplicates_removed)])
    grid.append(["Competitors run", str(len(runs))])
    grid.append(["Successful scrapers", str(successful)])
    grid.append(["Failed scrapers", str(failed)])
    grid.append(["Needs-review rows", str(needs_review)])
    grid.append([])
    grid.append(["Offers by city", ""])
    for city in TARGET_CITIES:
        grid.append([f"  {city}", str(by_city.get(city, 0))])
    for city, n in sorted(by_city.items()):
        if city not in TARGET_CITIES and city:
            grid.append([f"  {city} (other)", str(n)])
    grid.append([])
    grid.append(["Offers by service category", ""])
    for svc, n in sorted(by_service.items(), key=lambda kv: (-kv[1], kv[0])):
        if svc:
            grid.append([f"  {svc}", str(n)])
    grid.append([])
    grid.append(["Offers by competitor", ""])
    for comp, n in sorted(by_competitor.items(), key=lambda kv: (-kv[1], kv[0])):
        if comp:
            grid.append([f"  {comp}", str(n)])
    return grid


def build_data_tab_rows(
    rows: List[Dict], *, include_helper: bool = True,
) -> Tuple[List[str], List[List[str]]]:
    headers = list(SCHEMA_COLUMNS) + (HELPER_COLUMNS if include_helper else [])
    body = [_row_to_list(r, headers) for r in rows]
    return headers, body


def build_run_summary_rows(runs: List[RunInfo]) -> Tuple[List[str], List[List[str]]]:
    headers = [
        "competitor", "script", "status",
        "rows_generated", "current_rows_kept",
        "expired_rows_removed", "duplicate_rows_removed",
        "needs_review_count", "error_message", "run_time_seconds",
        "scraped_at",
    ]
    body: List[List[str]] = []
    for r in runs:
        body.append([
            r.competitor,
            RUN_SCRIPTS.get(r.competitor, ""),
            r.status,
            str(r.rows_generated),
            str(r.current_rows_kept),
            str(r.expired_rows_removed),
            str(r.duplicate_rows_removed),
            str(r.needs_review_count),
            r.error_message or "",
            "" if r.run_time_seconds is None else f"{r.run_time_seconds:.1f}",
            r.scraped_at or "",
        ])
    return headers, body


def build_expired_removed_rows(
    *,
    expired: List[Dict],
    duplicates: List[Dict],
    scraper_failed: List[Dict],
    outside_scope: List[Dict],
) -> Tuple[List[str], List[List[str]]]:
    headers = [
        "removed_reason", "competitor", "city", "page_url", "service_name",
        "promo_description", "discount_value", "expiry_date", "source_image",
        "raw_text_or_reason",
    ]
    body: List[List[str]] = []

    def push(reason: str, items: List[Dict]) -> None:
        for r in items:
            body.append([
                reason,
                r.get("business_name") or r.get("competitor") or "",
                r.get("city") or "",
                r.get("page_url") or r.get("url") or "",
                r.get("service_name") or "",
                r.get("promo_description") or "",
                r.get("discount_value") or "",
                r.get("expiry_date") or "",
                r.get("source_image") or "",
                (r.get("raw_text") or r.get("reason") or "")[:1000],
            ])

    push("expired", expired)
    push("duplicate", duplicates)
    push("scraper_failed", scraper_failed)
    push("outside_scope", outside_scope)
    return headers, body


def build_url_coverage_rows(coverage: List[Dict]) -> Tuple[List[str], List[List[str]]]:
    # Build a union of all keys observed; put canonical fields first.
    preferred = [
        "competitor", "url", "scope", "page_kind", "service_hint", "is_homepage",
        "status", "cards_on_page",
        "text_extracted_count", "image_ocr_extracted_count",
        "image_ocr_failed_needs_review_count",
        "pdf_extracted_count", "pdf_failed_count",
        "ocr_attempted", "ocr_success", "ocr_failed",
        "added_rows", "excluded_count", "row_count_written",
        "source_file",
    ]
    seen_keys: List[str] = []
    keys_set: set = set()
    for r in coverage:
        for k in r.keys():
            if k not in keys_set:
                keys_set.add(k)
                seen_keys.append(k)
    headers = [k for k in preferred if k in keys_set] + [
        k for k in seen_keys if k not in preferred
    ]
    body = [[(r.get(k) or "") for k in headers] for r in coverage]
    return headers, [[str(c) for c in row] for row in body]


# ---------------------------------------------------------------------------
# Sheets API
# ---------------------------------------------------------------------------
def get_service():
    """Build the Sheets API client, raising a clean error if creds missing."""
    from google.oauth2 import service_account  # noqa: WPS433
    from googleapiclient.discovery import build  # noqa: WPS433

    creds_path = ROOT / "service_account.json"
    if not creds_path.exists():
        env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if env_path and Path(env_path).exists():
            creds_path = Path(env_path)
        else:
            raise FileNotFoundError(
                "service_account.json not found at "
                f"{ROOT / 'service_account.json'} and "
                "GOOGLE_APPLICATION_CREDENTIALS env var is unset. "
                "See app/sheets/dashboard_uploader.py docstring for setup."
            )
    credentials = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials)


def fetch_tab_ids(service, sheet_id: str) -> Dict[str, int]:
    """Return {tab_title: sheet_id}."""
    spread = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in spread.get("sheets", [])
    }


def ensure_tabs(service, sheet_id: str, tab_names: Iterable[str]) -> Dict[str, int]:
    existing = fetch_tab_ids(service, sheet_id)
    requests = []
    for name in tab_names:
        if name not in existing:
            requests.append({"addSheet": {"properties": {"title": name}}})
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests},
        ).execute()
        existing = fetch_tab_ids(service, sheet_id)
    return existing


def clear_tab(service, sheet_id: str, tab_name: str) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A1:ZZ100000",
    ).execute()


def write_grid(
    service, sheet_id: str, tab_name: str, grid: List[List[str]],
) -> None:
    if not grid:
        return
    body = {"values": grid}
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()


def _bg(color: Dict[str, float]) -> Dict:
    return {"backgroundColor": color}


def _bold_white_header_format() -> Dict:
    return {
        "backgroundColor": COLOR_HEADER_BG,
        "textFormat": {
            "foregroundColor": COLOR_HEADER_FG,
            "bold": True,
        },
        "horizontalAlignment": "LEFT",
    }


def apply_data_tab_formatting(
    service, sheet_id: str, tab_sheet_id: int, *,
    header_cols: int, data_row_count: int, wrap_cols: List[int],
) -> None:
    """Standard formatting for data tabs: bold header, freeze row 1, filter,
    wrap long cols, basic borders."""
    requests: List[Dict] = []

    # Bold + colored header row.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": header_cols,
            },
            "cell": {"userEnteredFormat": _bold_white_header_format()},
            "fields": (
                "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
            ),
        }
    })

    # Freeze top row.
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": tab_sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Set a filter over the whole header + data range.
    if data_row_count >= 0:
        requests.append({
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": tab_sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": max(1, data_row_count + 1),
                        "startColumnIndex": 0,
                        "endColumnIndex": header_cols,
                    },
                }
            }
        })

    # Wrap text on selected columns (promo_description / offer_details / etc).
    for col_idx in wrap_cols:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": tab_sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(1, data_row_count + 1),
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }
        })

    # Auto-resize columns (best effort).
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": tab_sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": header_cols,
            }
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()


def apply_city_tab_conditional_formatting(
    service, sheet_id: str, tab_sheet_id: int, *,
    header_cols: int, data_row_count: int, helper_col_idx: int,
    needs_review_col_idx: int,
) -> None:
    """Color rows based on the trailing helper column _change_status and the
    needs_review column."""
    if data_row_count <= 0:
        return
    # Helper column letter -> A1 reference for the row, e.g. $AE2.
    helper_a1 = _col_letter(helper_col_idx)
    needs_review_a1 = _col_letter(needs_review_col_idx)

    rules = [
        # needs_review (highest priority) → light orange
        ('=$' + needs_review_a1 + '2="TRUE"', COLOR_LIGHT_ORANGE),
        ('=$' + helper_a1 + '2="new"', COLOR_LIGHT_GREEN),
        ('=$' + helper_a1 + '2="updated"', COLOR_LIGHT_YELLOW),
        ('=$' + helper_a1 + '2="needs_review"', COLOR_LIGHT_ORANGE),
    ]

    requests: List[Dict] = []
    for formula, color in rules:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": tab_sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": data_row_count + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": header_cols,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
                "index": 0,
            }
        })
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()


def apply_dashboard_formatting(
    service, sheet_id: str, tab_sheet_id: int, *, total_rows: int,
) -> None:
    requests: List[Dict] = []

    # Title row — big bold, dark background, white text.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 4,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_DASH_TITLE_BG,
                    "textFormat": {
                        "foregroundColor": COLOR_HEADER_FG,
                        "bold": True, "fontSize": 16,
                    },
                    "horizontalAlignment": "LEFT",
                    "padding": {"top": 4, "bottom": 4, "left": 8, "right": 8},
                }
            },
            "fields": (
                "userEnteredFormat(backgroundColor,textFormat,"
                "horizontalAlignment,padding)"
            ),
        }
    })

    # Merge title row across 4 columns.
    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 4,
            },
            "mergeType": "MERGE_ALL",
        }
    })

    # Italic subtitle row.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1, "endRowIndex": 2,
                "startColumnIndex": 0, "endColumnIndex": 4,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"italic": True},
                }
            },
            "fields": "userEnteredFormat.textFormat",
        }
    })

    # Freeze the top 2 title rows so the metric table scrolls under them.
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": tab_sheet_id,
                "gridProperties": {"frozenRowCount": 2},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Bold the "Metric | Value" header row.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 3, "endRowIndex": 4,
                "startColumnIndex": 0, "endColumnIndex": 2,
            },
            "cell": {"userEnteredFormat": _bold_white_header_format()},
            "fields": (
                "userEnteredFormat(backgroundColor,textFormat,"
                "horizontalAlignment)"
            ),
        }
    })

    # Card-style fill on the first metric block (rows 4..12).
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 4, "endRowIndex": min(13, total_rows),
                "startColumnIndex": 0, "endColumnIndex": 2,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_DASH_CARD,
                    "textFormat": {"bold": False},
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })

    # Light borders on the data column range.
    requests.append({
        "updateBorders": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 3, "endRowIndex": max(4, total_rows),
                "startColumnIndex": 0, "endColumnIndex": 2,
            },
            "top":    {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}},
            "bottom": {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}},
            "left":   {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}},
            "right":  {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}},
            "innerHorizontal": {
                "style": "SOLID", "color": {"red": 0.9, "green": 0.9, "blue": 0.9},
            },
            "innerVertical": {
                "style": "SOLID", "color": {"red": 0.9, "green": 0.9, "blue": 0.9},
            },
        }
    })

    # Auto-resize the two main columns.
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": tab_sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 4,
            }
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()


def apply_run_summary_conditional_formatting(
    service, sheet_id: str, tab_sheet_id: int, *,
    header_cols: int, data_row_count: int, status_col_idx: int,
) -> None:
    if data_row_count <= 0:
        return
    status_a1 = _col_letter(status_col_idx)
    rules = [
        ('=$' + status_a1 + '2="success"', COLOR_LIGHT_GREEN),
        ('=$' + status_a1 + '2="partial"', COLOR_LIGHT_YELLOW),
        ('=$' + status_a1 + '2="failed"', COLOR_LIGHT_RED),
        ('=$' + status_a1 + '2="parse_failed"', COLOR_LIGHT_RED),
    ]
    requests: List[Dict] = []
    for formula, color in rules:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": tab_sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": data_row_count + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": header_cols,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
                "index": 0,
            }
        })
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()


def clear_conditional_format_rules(service, sheet_id: str, tab_sheet_id: int) -> None:
    """Wipe existing CF rules on a tab before reapplying."""
    spread = service.spreadsheets().get(
        spreadsheetId=sheet_id, ranges=[], includeGridData=False,
    ).execute()
    for s in spread.get("sheets", []):
        if s["properties"]["sheetId"] != tab_sheet_id:
            continue
        rules = s.get("conditionalFormats") or []
        if not rules:
            return
        # Delete in reverse order.
        reqs = [
            {"deleteConditionalFormatRule": {"sheetId": tab_sheet_id, "index": i}}
            for i in range(len(rules) - 1, -1, -1)
        ]
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": reqs},
        ).execute()
        return


def _col_letter(idx0: int) -> str:
    """Zero-based column index -> spreadsheet column letter ('A', 'AE')."""
    n = idx0
    letters = ""
    while True:
        n, rem = divmod(n, 26)
        letters = chr(ord("A") + rem) + letters
        if n == 0:
            return letters
        n -= 1


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@dataclass
class BuildResult:
    runs: List[RunInfo]
    all_current: List[Dict]
    by_city_current: Dict[str, List[Dict]]
    expired_rows: List[Dict]
    duplicates_removed: List[Dict]
    outside_scope: List[Dict]
    scraper_failed_rows: List[Dict]
    deltas: Dict[str, int]
    coverage: List[Dict]
    duplicates_removed_count: int
    expired_removed_count: int
    last_run_ts: str
    snapshot: Dict[str, Dict]


def build_dashboard_data(
    *, today: Optional[date] = None,
) -> BuildResult:
    today = today or date.today()
    runs = load_v2_outputs()
    coverage = load_url_coverage_csvs()
    previous = load_snapshot()

    all_current: List[Dict] = []
    duplicates_removed_rows: List[Dict] = []
    expired_rows: List[Dict] = []
    outside_scope_rows: List[Dict] = []
    scraper_failed_rows: List[Dict] = []

    for r in runs:
        # Apply final dedupe per competitor (keeps QA-expanded outputs honest).
        deduped, dup_removed = final_dedupe(r.promotions)
        r.duplicate_rows_removed = dup_removed
        if dup_removed > 0:
            # We don't keep the removed rows around, but we record a count;
            # also synthesize sentinels for the Expired/Removed tab so they
            # have a visible audit trail.
            duplicates_removed_rows.extend([
                {
                    "business_name": r.competitor,
                    "city": "",
                    "page_url": "",
                    "raw_text": f"{dup_removed} duplicate rows collapsed in final-deduped view",
                }
            ])

        # Split kept rows on expiry.
        current, expired = classify_expired(deduped, today=today)
        r.current_rows_kept = len(current)
        r.expired_rows_removed = len(expired)
        for er in expired:
            er2 = dict(er)
            er2["business_name"] = er.get("business_name") or r.competitor
            expired_rows.append(er2)
        all_current.extend(current)

        # Outside-scope rows from excluded_rows.
        for x in r.excluded_rows:
            x2 = dict(x)
            x2["business_name"] = r.competitor
            outside_scope_rows.append(x2)

        # If the scraper failed outright, surface that row too.
        if r.status in ("failed", "parse_failed"):
            scraper_failed_rows.append({
                "business_name": r.competitor,
                "city": "",
                "page_url": str(r.json_path),
                "raw_text": r.error_message or "scraper failed; see logs",
            })

    # Stamp change status (new/updated/needs_review) using prior snapshot.
    all_current_stamped = compute_change_status(all_current, previous)

    # Detect rows that vanished since the previous run (true expirations).
    snapshot_expired = detect_expired_from_snapshot(all_current_stamped, previous)
    for er in snapshot_expired:
        expired_rows.append(er)

    by_city: Dict[str, List[Dict]] = {c: [] for c in TARGET_CITIES}
    for r in all_current_stamped:
        city = r.get("city") or ""
        by_city.setdefault(city, []).append(r)

    deltas = {
        "new": sum(1 for r in all_current_stamped if r.get("_change_status") == "new"),
        "updated": sum(1 for r in all_current_stamped if r.get("_change_status") == "updated"),
        "needs_review": sum(1 for r in all_current_stamped if r.get("_change_status") == "needs_review"),
    }

    duplicates_removed_count = sum(r.duplicate_rows_removed for r in runs)
    expired_removed_count = len(expired_rows)

    # Last-run timestamp = most recent scraped_at among all runs.
    timestamps = [r.scraped_at for r in runs if r.scraped_at]
    last_run_ts = max(timestamps) if timestamps else datetime.now().isoformat()

    # Build the snapshot for next run.
    new_snapshot: Dict[str, Dict] = {}
    for r in all_current_stamped:
        key = _row_key(r)
        new_snapshot[key] = {
            "row": {k: r.get(k) for k in SCHEMA_COLUMNS},
            "content_fingerprint": _content_fingerprint(r),
        }

    return BuildResult(
        runs=runs,
        all_current=all_current_stamped,
        by_city_current=by_city,
        expired_rows=expired_rows,
        duplicates_removed=duplicates_removed_rows,
        outside_scope=outside_scope_rows,
        scraper_failed_rows=scraper_failed_rows,
        deltas=deltas,
        coverage=coverage,
        duplicates_removed_count=duplicates_removed_count,
        expired_removed_count=expired_removed_count,
        last_run_ts=last_run_ts,
        snapshot=new_snapshot,
    )


def render_preview(build: BuildResult) -> Dict:
    """Render the per-tab payload as a JSON-serializable preview."""
    dash = build_dashboard_grid(
        runs=build.runs,
        all_current=build.all_current,
        deltas=build.deltas,
        duplicates_removed=build.duplicates_removed_count,
        expired_removed=build.expired_removed_count,
        last_run_ts=build.last_run_ts,
    )
    out: Dict = {"tabs": {}}
    out["tabs"][TAB_DASHBOARD] = {"rows": dash}

    for city in TARGET_CITIES:
        headers, body = build_data_tab_rows(build.by_city_current.get(city, []))
        out["tabs"][city] = {"headers": headers, "rows": body}

    headers, body = build_data_tab_rows(build.all_current)
    out["tabs"][TAB_ALL] = {"headers": headers, "rows": body}

    headers, body = build_run_summary_rows(build.runs)
    out["tabs"][TAB_RUN_SUMMARY] = {"headers": headers, "rows": body}

    headers, body = build_expired_removed_rows(
        expired=build.expired_rows,
        duplicates=build.duplicates_removed,
        scraper_failed=build.scraper_failed_rows,
        outside_scope=build.outside_scope,
    )
    out["tabs"][TAB_EXPIRED] = {"headers": headers, "rows": body}

    headers, body = build_url_coverage_rows(build.coverage)
    out["tabs"][TAB_URL_COVERAGE] = {"headers": headers, "rows": body}

    out["last_run_ts"] = build.last_run_ts
    out["deltas"] = build.deltas
    out["counts"] = {
        "current_rows": len(build.all_current),
        "expired_rows": build.expired_removed_count,
        "duplicates_removed": build.duplicates_removed_count,
        "needs_review": build.deltas.get("needs_review", 0),
    }
    return out


def upload(build: BuildResult, *, sheet_id: str = SHEET_ID) -> None:
    """Push all tabs to the Google Sheet identified by ``sheet_id``."""
    service = get_service()
    ensure_tabs(service, sheet_id, ALL_TABS_ORDER)
    tab_ids = fetch_tab_ids(service, sheet_id)

    # 1. Dashboard
    clear_tab(service, sheet_id, TAB_DASHBOARD)
    dash_grid = build_dashboard_grid(
        runs=build.runs,
        all_current=build.all_current,
        deltas=build.deltas,
        duplicates_removed=build.duplicates_removed_count,
        expired_removed=build.expired_removed_count,
        last_run_ts=build.last_run_ts,
    )
    write_grid(service, sheet_id, TAB_DASHBOARD, dash_grid)
    apply_dashboard_formatting(
        service, sheet_id, tab_ids[TAB_DASHBOARD], total_rows=len(dash_grid),
    )

    # 2-4. City tabs
    needs_review_idx = SCHEMA_COLUMNS.index("needs_review")
    helper_idx = len(SCHEMA_COLUMNS)  # _change_status is the first helper col
    wrap_cols = [
        SCHEMA_COLUMNS.index("promo_description"),
        SCHEMA_COLUMNS.index("offer_details"),
        SCHEMA_COLUMNS.index("ad_text"),
    ]
    for city in TARGET_CITIES:
        tab = CITY_TAB_NAMES[city]
        clear_tab(service, sheet_id, tab)
        clear_conditional_format_rules(service, sheet_id, tab_ids[tab])
        headers, body = build_data_tab_rows(build.by_city_current.get(city, []))
        write_grid(service, sheet_id, tab, [headers] + body)
        apply_data_tab_formatting(
            service, sheet_id, tab_ids[tab],
            header_cols=len(headers),
            data_row_count=len(body),
            wrap_cols=wrap_cols,
        )
        apply_city_tab_conditional_formatting(
            service, sheet_id, tab_ids[tab],
            header_cols=len(headers),
            data_row_count=len(body),
            helper_col_idx=helper_idx,
            needs_review_col_idx=needs_review_idx,
        )

    # 5. All Current Offers
    clear_tab(service, sheet_id, TAB_ALL)
    clear_conditional_format_rules(service, sheet_id, tab_ids[TAB_ALL])
    headers, body = build_data_tab_rows(build.all_current)
    write_grid(service, sheet_id, TAB_ALL, [headers] + body)
    apply_data_tab_formatting(
        service, sheet_id, tab_ids[TAB_ALL],
        header_cols=len(headers),
        data_row_count=len(body),
        wrap_cols=wrap_cols,
    )
    apply_city_tab_conditional_formatting(
        service, sheet_id, tab_ids[TAB_ALL],
        header_cols=len(headers),
        data_row_count=len(body),
        helper_col_idx=helper_idx,
        needs_review_col_idx=needs_review_idx,
    )

    # 6. Run Summary
    clear_tab(service, sheet_id, TAB_RUN_SUMMARY)
    clear_conditional_format_rules(service, sheet_id, tab_ids[TAB_RUN_SUMMARY])
    headers, body = build_run_summary_rows(build.runs)
    write_grid(service, sheet_id, TAB_RUN_SUMMARY, [headers] + body)
    apply_data_tab_formatting(
        service, sheet_id, tab_ids[TAB_RUN_SUMMARY],
        header_cols=len(headers),
        data_row_count=len(body),
        wrap_cols=[headers.index("error_message")] if "error_message" in headers else [],
    )
    apply_run_summary_conditional_formatting(
        service, sheet_id, tab_ids[TAB_RUN_SUMMARY],
        header_cols=len(headers),
        data_row_count=len(body),
        status_col_idx=headers.index("status"),
    )

    # 7. Expired / Removed
    clear_tab(service, sheet_id, TAB_EXPIRED)
    clear_conditional_format_rules(service, sheet_id, tab_ids[TAB_EXPIRED])
    headers, body = build_expired_removed_rows(
        expired=build.expired_rows,
        duplicates=build.duplicates_removed,
        scraper_failed=build.scraper_failed_rows,
        outside_scope=build.outside_scope,
    )
    write_grid(service, sheet_id, TAB_EXPIRED, [headers] + body)
    apply_data_tab_formatting(
        service, sheet_id, tab_ids[TAB_EXPIRED],
        header_cols=len(headers),
        data_row_count=len(body),
        wrap_cols=[
            headers.index("promo_description"),
            headers.index("raw_text_or_reason"),
        ],
    )

    # 8. URL Coverage
    clear_tab(service, sheet_id, TAB_URL_COVERAGE)
    clear_conditional_format_rules(service, sheet_id, tab_ids[TAB_URL_COVERAGE])
    headers, body = build_url_coverage_rows(build.coverage)
    write_grid(service, sheet_id, TAB_URL_COVERAGE, [headers] + body)
    apply_data_tab_formatting(
        service, sheet_id, tab_ids[TAB_URL_COVERAGE],
        header_cols=len(headers),
        data_row_count=len(body),
        wrap_cols=[headers.index("url")] if "url" in headers else [],
    )

    save_snapshot(build.snapshot)
    logger.info(
        f"Sheets upload complete: {len(build.all_current)} current rows across "
        f"{len(TARGET_CITIES)} cities."
    )


def write_preview(build: BuildResult) -> Path:
    PREVIEW_FILE.write_text(
        json.dumps(render_preview(build), indent=2, default=str)
    )
    return PREVIEW_FILE
