"""Shared Google Sheets authentication, formatting, and protection helpers."""
import os

import gspread
import pandas as pd
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_ROW_BUFFER = 200
DEFAULT_BORDER_COLOR = {"red": 0.850, "green": 0.870, "blue": 0.890}
DEFAULT_ZEBRA_STRIPE_COLOR = {"red": 0.900, "green": 0.945, "blue": 1.000}
GOOGLE_DISCOVERY_URL = "https://sheets.googleapis.com/$discovery/rest?version=v4"


def _google_timeout() -> tuple[float, float]:
    connect = float(os.environ.get("FINPAY_GOOGLE_CONNECT_TIMEOUT", "10"))
    read = float(os.environ.get("FINPAY_GOOGLE_READ_TIMEOUT", "45"))
    if connect <= 0 or read <= 0:
        raise ValueError("Google timeout values must be positive")
    return connect, read


def _google_retry_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class _GoogleAuthorizedSession(AuthorizedSession):
    """AuthorizedSession with a default timeout for OAuth and API calls."""

    def __init__(self, credentials: Credentials):
        super().__init__(credentials)
        self._default_timeout = _google_timeout()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, **kwargs)


def google_api_preflight() -> dict:
    """Fail fast when the Google API network path is unavailable."""
    connect_timeout, read_timeout = _google_timeout()
    try:
        response = _google_retry_session().get(
            GOOGLE_DISCOVERY_URL,
            timeout=(connect_timeout, read_timeout),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            "Google API preflight failed; check outbound HTTPS/proxy access "
            f"to {GOOGLE_DISCOVERY_URL}: {exc}"
        ) from exc
    print(f"Google API preflight passed: HTTP {response.status_code}")
    return {"url": GOOGLE_DISCOVERY_URL, "status": response.status_code}
DEFAULT_WHITE_COLOR = {"red": 1, "green": 1, "blue": 1}


def uppercase_sheet_value(value):
    """Uppercase literal text before writing it to Google Sheets."""
    if not isinstance(value, str):
        return value
    if value.startswith("="):
        return value
    if value.startswith("'"):
        return "'" + value[1:].upper()
    return value.upper()


def uppercase_sheet_rows(rows: list[list]) -> list[list]:
    """Uppercase literal text cells while preserving formulas and numbers."""
    return [
        [uppercase_sheet_value(value) for value in row]
        for row in rows
    ]


def a1_cell(row: int, col: int) -> str:
    """Return an A1-style cell reference for 1-based row/column numbers."""
    col_letter = ""
    while col:
        col, rem = divmod(col - 1, 26)
        col_letter = chr(65 + rem) + col_letter
    return f"{col_letter}{row}"


def sheet_range(
    start_row: int,
    end_row: int,
    start_col: int = 1,
    end_col: int | None = None,
) -> str:
    """Return an A1-style range for 1-based row/column numbers."""
    end_col = end_col or start_col
    return f"{a1_cell(start_row, start_col)}:{a1_cell(end_row, end_col)}"


def clean_sheet_value(value):
    """Normalize pandas/None missing values before writing to Sheets."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return value


def number_sheet_value(value):
    """Return an integer-like value for Sheets, or blank for missing input."""
    value = clean_sheet_value(value)
    if value == "":
        return ""
    return int(value)


def sheet_text_value(value):
    """Force a value to be interpreted as text by Google Sheets."""
    value = clean_sheet_value(value)
    if value == "":
        return ""
    return f"'{str(value)}"


def datetime_sheet_display(value) -> str:
    """Format a timestamp-like value for display in report sheets."""
    value = clean_sheet_value(value)
    if value == "":
        return ""
    return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M:%S")


def date_sheet_display(value) -> str:
    """Format a date-like value for display in report sheets."""
    value = clean_sheet_value(value)
    if value == "":
        return ""
    return pd.to_datetime(value, dayfirst=True).strftime("%d/%m/%Y")


def report_date_display(
    df: pd.DataFrame,
    report_date: str | None = None,
    date_column: str = "Transaction Date",
) -> str:
    """Resolve the report date used to replace rerun rows in detail sheets."""
    if not df.empty:
        return pd.to_datetime(df[date_column]).dt.strftime("%d/%m/%Y").max()
    if report_date:
        parsed = pd.to_datetime(report_date, format="%Y-%m-%d", errors="coerce")
        if pd.isna(parsed):
            parsed = pd.to_datetime(report_date, dayfirst=True)
        return parsed.strftime("%d/%m/%Y")
    return ""


def make_gspread_client(sa_key_path: str):
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    google_api_preflight()
    creds = Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    session = _GoogleAuthorizedSession(creds)
    client = gspread.Client(auth=None, session=session)
    client.auth = creds
    client.http_client.set_timeout(_google_timeout())
    return client


def open_or_create_finpay_spreadsheet(gspread_client, title: str):
    """
    Open the report spreadsheet, or create it with the service account if it is
    missing. Drive-level access settings are only synced for spreadsheets the
    service account creates; existing owner-managed spreadsheets are opened as-is.
    """
    try:
        sh = gspread_client.open(title)
        created = False
    except gspread.SpreadsheetNotFound:
        sh = gspread_client.create(title)
        created = True
        print(f"Created spreadsheet '{title}' with the GCP service account.")

    _configure_spreadsheet_access(gspread_client, sh, created=created)
    return sh


def _spreadsheet_writer_emails() -> list[str]:
    emails = set(_split_email_list(os.environ.get("FINPAY_SPREADSHEET_WRITER_EMAILS")))
    emails.update(_split_email_list(os.environ.get("FINPAY_MANDIRI_EDITOR_EMAILS")))
    return sorted(emails)


def _configure_spreadsheet_access(gspread_client, sh, created: bool = False) -> None:
    """
    Keep service-account-created reports private and prevent spreadsheet
    editors from sharing them.
    """
    if not created:
        return

    try:
        locale = os.environ.get("FINPAY_SPREADSHEET_LOCALE", "en_GB").strip()
        timezone = os.environ.get("FINPAY_SPREADSHEET_TIMEZONE", "Asia/Makassar").strip()
        if locale:
            sh.update_locale(locale)
        if timezone:
            sh.update_timezone(timezone)

        # Equivalent to disabling "Editors can change permissions and share".
        gspread_client.http_client.request(
            "patch",
            f"https://www.googleapis.com/drive/v3/files/{sh.id}",
            json={"writersCanShare": False},
            params={"fields": "id,writersCanShare", "supportsAllDrives": True},
        )

        allowed_users = set(_spreadsheet_writer_emails())
        allowed_users.update(_protection_editor_emails(gspread_client))
        allowed_users.discard("")

        for email in sorted(allowed_users):
            if email == _service_account_email(gspread_client):
                continue
            sh.share(email, perm_type="user", role="writer", notify=created)

        for permission in sh.list_permissions():
            permission_type = permission.get("type")
            permission_id = permission.get("id")
            if permission_type in {"anyone", "domain"} and permission_id:
                gspread_client.remove_permission(sh.id, permission_id)

    except Exception as exc:
        print(
            "Warning: could not fully sync spreadsheet Drive permissions. "
            f"Check service account ownership and Drive API permissions: {exc}"
        )


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


def _delete_all_banded_range_requests(sh, ws) -> list[dict]:
    """Build requests that remove every banded range on one worksheet."""
    metadata = sh.fetch_sheet_metadata({
        "fields": "sheets(properties(sheetId),bandedRanges(bandedRangeId))",
    })
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != ws.id:
            continue
        for banded_range in sheet.get("bandedRanges", []):
            requests.append({
                "deleteBanding": {
                    "bandedRangeId": banded_range["bandedRangeId"],
                }
            })
    return requests


def ensure_row_capacity(
    sh,
    ws,
    required_rows: int,
    buffer_rows: int = DEFAULT_ROW_BUFFER,
    label: str = "worksheet",
) -> None:
    """Expand worksheet rows before writing ranges near the grid boundary."""
    current_rows = int(getattr(ws, "row_count", 0) or 0)
    if current_rows >= required_rows:
        return

    target_rows = required_rows + buffer_rows
    sh.batch_update({"requests": [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"rowCount": target_rows},
                },
                "fields": "gridProperties.rowCount",
            }
        },
    ]})
    print(
        f"Expanded {label} row capacity: "
        f"{current_rows} -> {target_rows} rows"
    )


def _contiguous_row_runs(row_numbers: list[int]) -> list[tuple[int, int]]:
    if not row_numbers:
        return []

    runs = []
    start = previous = row_numbers[0]
    for row_number in row_numbers[1:]:
        if row_number == previous + 1:
            previous = row_number
            continue
        runs.append((start, previous))
        start = previous = row_number
    runs.append((start, previous))
    return runs


def _delete_sheet_rows(sh, ws, row_numbers: list[int]) -> None:
    """Delete 1-based worksheet rows, grouping adjacent rows per request."""
    if not row_numbers:
        return

    requests = []
    for start, end in reversed(_contiguous_row_runs(sorted(row_numbers))):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": start - 1,
                    "endIndex": end,
                }
            }
        })

    sh.batch_update({"requests": requests})


def _insert_blank_sheet_rows(sh, ws, start_row: int, row_count: int) -> None:
    """Insert blank rows before a 1-based worksheet row."""
    if row_count <= 0:
        return

    sh.batch_update({"requests": [{
        "insertDimension": {
            "range": {
                "sheetId": ws.id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": start_row - 1 + row_count,
            },
            "inheritFromBefore": start_row > 1,
        }
    }]})


def _matching_report_date_rows(
    existing_rows: list[list[str]],
    headers: list[str],
    date_text: str,
    primary_header: str = "REPORT DATE",
    fallback_header: str | None = None,
) -> list[int]:
    """Return 1-based rows whose date column matches the report date."""
    if primary_header in headers:
        report_col = headers.index(primary_header)
        return [
            row_number
            for row_number, row in enumerate(existing_rows[1:], start=2)
            if len(row) > report_col and str(row[report_col]).strip() == date_text
        ]

    if fallback_header and fallback_header in headers:
        fallback_col = headers.index(fallback_header)
        return [
            row_number
            for row_number, row in enumerate(existing_rows[1:], start=2)
            if len(row) > fallback_col and date_text in str(row[fallback_col])
        ]

    return []


def _service_account_email(gspread_client) -> str | None:
    auth = getattr(gspread_client, "auth", None)
    return (
        getattr(auth, "service_account_email", None)
        or getattr(auth, "signer_email", None)
    )


def _split_email_list(value: str | None) -> list[str]:
    if not value:
        return []
    if value.strip().upper() == "__EMPTY__":
        return []
    return [
        email.strip()
        for email in value.split(",")
        if email.strip()
    ]


def _protection_editor_emails(
    gspread_client,
    extra_editor_emails: list[str] | None = None,
) -> list[str]:
    emails = set(_split_email_list(os.environ.get("FINPAY_PROTECTION_EDITOR_EMAILS")))
    emails.update(extra_editor_emails or [])

    service_account_email = _service_account_email(gspread_client)
    if service_account_email:
        emails.add(service_account_email)

    return sorted(emails)


def _mandiri_editor_emails() -> list[str]:
    return _split_email_list(os.environ.get("FINPAY_MANDIRI_EDITOR_EMAILS"))


def _grid_range(
    ws,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
) -> dict:
    return {
        "sheetId": ws.id,
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,
        "startColumnIndex": start_col - 1,
        "endColumnIndex": end_col,
    }


def _horizontal_border_requests(
    ws,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    *,
    top: bool = False,
    bottom: bool = True,
    style: str = "SOLID",
    color: dict | None = None,
) -> list[dict]:
    """Build repeatCell requests for horizontal borders only."""
    if start_row > end_row or start_col > end_col:
        return []

    border = {
        "style": style,
        "color": color or DEFAULT_BORDER_COLOR,
    }
    requests = []
    for side, enabled in (("top", top), ("bottom", bottom)):
        if not enabled:
            continue
        requests.append({
            "repeatCell": {
                "range": _grid_range(ws, start_row, end_row, start_col, end_col),
                "cell": {
                    "userEnteredFormat": {
                        "borders": {
                            side: border,
                        }
                    }
                },
                "fields": f"userEnteredFormat.borders.{side}",
            }
        })
    return requests


def _clear_background_color_request(
    ws,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
) -> dict | None:
    """Build a request that clears explicit background colors from a range."""
    if start_row > end_row or start_col > end_col:
        return None

    return {
        "repeatCell": {
            "range": _grid_range(ws, start_row, end_row, start_col, end_col),
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _row_banding_request(
    ws,
    header_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    header_color: dict,
    first_band_color: dict | None = None,
    second_band_color: dict | None = None,
) -> dict | None:
    """Build an addBanding request for zebra-striped report rows."""
    if header_row > end_row or start_col > end_col:
        return None

    return {
        "addBanding": {
            "bandedRange": {
                "range": _grid_range(ws, header_row, end_row, start_col, end_col),
                "rowProperties": {
                    "headerColor": header_color,
                    "firstBandColor": first_band_color or DEFAULT_WHITE_COLOR,
                    "secondBandColor": (
                        second_band_color or DEFAULT_ZEBRA_STRIPE_COLOR
                    ),
                },
            }
        }
    }


def _add_protected_sheet_request(
    gspread_client,
    ws,
    description: str,
    unprotected_ranges: list[tuple[int, int, int, int]] | None = None,
) -> dict:
    protected_range = {
        "range": {"sheetId": ws.id},
        "description": description,
        "warningOnly": False,
    }
    protected_range["editors"] = {
        "users": _protection_editor_emails(gspread_client),
        "domainUsersCanEdit": False,
    }
    if unprotected_ranges:
        protected_range["unprotectedRanges"] = [
            _grid_range(ws, sr, er, sc, ec)
            for sr, er, sc, ec in unprotected_ranges
            if sr <= er and sc <= ec
        ]

    return {"addProtectedRange": {"protectedRange": protected_range}}


def _add_protected_range_request(
    gspread_client,
    ws,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    description: str,
    extra_editor_emails: list[str] | None = None,
) -> dict | None:
    if start_row > end_row or start_col > end_col:
        return None

    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": _grid_range(ws, start_row, end_row, start_col, end_col),
                "description": description,
                "warningOnly": False,
                "editors": {
                    "users": _protection_editor_emails(
                        gspread_client,
                        extra_editor_emails=extra_editor_emails,
                    ),
                    "domainUsersCanEdit": False,
                },
            }
        }
    }
