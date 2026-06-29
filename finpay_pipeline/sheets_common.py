"""Shared Google Sheets authentication and formatting helpers."""
import gspread
from google.oauth2.service_account import Credentials

def make_gspread_client(sa_key_path: str):
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _delete_all_protected_range_requests(sh, ws) -> list[dict]:
    """Build requests that remove every protected range on one worksheet."""
    metadata = sh.fetch_sheet_metadata({
        "fields": "sheets(properties(sheetId),protectedRanges(protectedRangeId))",
    })
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != ws.id:
            continue
        for protected_range in sheet.get("protectedRanges", []):
            requests.append({
                "deleteProtectedRange": {
                    "protectedRangeId": protected_range["protectedRangeId"],
                }
            })
    return requests
