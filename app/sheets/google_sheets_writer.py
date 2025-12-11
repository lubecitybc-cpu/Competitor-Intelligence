"""Google Sheets writer - Write merged promotions data to Google Sheets."""
import json
from pathlib import Path
from typing import List, Dict, Optional
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config.constants import ROOT
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "google_sheets_writer.log")

# Google Sheet ID from the URL
SHEET_ID = "15vOEjTo4bNSZsWmMA2ilPp44PbMie14P1hWFtIKO_B8"
SHEET_NAME = "Promotions & Offers"  # Tab name

# Column headers matching the sheet
COLUMN_HEADERS = [
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
    "date_scraped"
]


def get_sheets_service():
    """Initialize Google Sheets API service."""
    try:
        # Try to find service account JSON
        creds_path = ROOT / "service_account.json"
        
        if not creds_path.exists():
            # Try environment variable
            creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if creds_path:
                creds_path = Path(creds_path)
            else:
                raise FileNotFoundError(
                    "Service account credentials not found. "
                    "Place service_account.json in project root or set GOOGLE_APPLICATION_CREDENTIALS env var."
                )
        
        credentials = service_account.Credentials.from_service_account_file(
            str(creds_path),
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        
        service = build('sheets', 'v4', credentials=credentials)
        logger.info("Google Sheets API service initialized successfully")
        return service
        
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets service: {e}", exc_info=True)
        raise


def clear_sheet(service, sheet_id: str, sheet_name: str, start_row: int = 2):
    """Clear data from sheet (keep headers)."""
    try:
        range_name = f"{sheet_name}!A{start_row}:Z10000"  # Clear up to row 10000
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=range_name
        ).execute()
        logger.info(f"Cleared sheet data from row {start_row} onwards")
        return True
    except HttpError as e:
        logger.error(f"Error clearing sheet: {e}", exc_info=True)
        return False


def write_to_sheets(rows: List[Dict], sheet_id: str = SHEET_ID, sheet_name: str = SHEET_NAME) -> bool:
    """
    Write merged promotion rows to Google Sheets.
    
    Args:
        rows: List of row dictionaries matching COLUMN_HEADERS
        sheet_id: Google Sheet ID
        sheet_name: Sheet tab name
    
    Returns:
        True if successful, False otherwise
    """
    if not rows:
        logger.warning("No rows to write to Google Sheets")
        return False
    
    try:
        service = get_sheets_service()
        
        # Convert rows to list of lists (values only)
        values = []
        for row in rows:
            row_values = [row.get(header, "") for header in COLUMN_HEADERS]
            values.append(row_values)
        
        # Prepare the range (start from row 2, after headers)
        range_name = f"{sheet_name}!A2:N{len(values) + 1}"
        
        # Clear existing data first (keep headers)
        clear_sheet(service, sheet_id, sheet_name, start_row=2)
        
        # Write data
        body = {
            'values': values
        }
        
        result = service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',  # Allows formulas and proper formatting
            body=body
        ).execute()
        
        updated_cells = result.get('updatedCells', 0)
        logger.info(f"Successfully wrote {len(rows)} rows ({updated_cells} cells) to Google Sheets")
        logger.info(f"Sheet: {sheet_name}, Range: {range_name}")
        
        return True
        
    except HttpError as e:
        logger.error(f"Google Sheets API error: {e}", exc_info=True)
        if e.resp.status == 403:
            logger.error("Permission denied. Check service account has access to the sheet.")
        elif e.resp.status == 404:
            logger.error(f"Sheet not found. Check sheet ID and tab name: {sheet_name}")
        return False
    except Exception as e:
        logger.error(f"Error writing to Google Sheets: {e}", exc_info=True)
        return False


def write_merged_data_to_sheets(merged_data_file: Optional[Path] = None) -> bool:
    """
    Load merged data and write to Google Sheets.
    
    Args:
        merged_data_file: Path to merged JSON file. If None, uses default location.
    
    Returns:
        True if successful, False otherwise
    """
    if merged_data_file is None:
        merged_data_file = Path(__file__).parent.parent.parent / "data" / "sheets_ready" / "promotions_merged_for_sheets.json"
    
    if not merged_data_file.exists():
        logger.error(f"Merged data file not found: {merged_data_file}")
        return False
    
    try:
        data = json.loads(merged_data_file.read_text())
        rows = data.get("rows", [])
        
        if not rows:
            logger.warning("No rows found in merged data file")
            return False
        
        logger.info(f"Loading {len(rows)} rows from {merged_data_file}")
        return write_to_sheets(rows)
        
    except Exception as e:
        logger.error(f"Error loading merged data: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    import sys
    
    # Test Google Sheets connection
    print("Testing Google Sheets connection...")
    try:
        service = get_sheets_service()
        print("✅ Google Sheets API service initialized")
        
        # Try to read sheet info
        spreadsheet = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        print(f"✅ Successfully connected to sheet: {spreadsheet.get('properties', {}).get('title', 'Unknown')}")
        
        # List sheet tabs
        sheets = spreadsheet.get('sheets', [])
        print(f"\nAvailable tabs:")
        for sheet in sheets:
            print(f"  - {sheet.get('properties', {}).get('title', 'Unknown')}")
        
        print(f"\nTarget tab: {SHEET_NAME}")
        if any(s.get('properties', {}).get('title') == SHEET_NAME for s in sheets):
            print(f"✅ Tab '{SHEET_NAME}' found")
        else:
            print(f"⚠️  Tab '{SHEET_NAME}' not found. Will attempt to create or use first tab.")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

