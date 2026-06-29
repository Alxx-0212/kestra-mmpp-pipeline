"""Shared Google Sheets authentication, formatting, and protection helpers."""
import os

import gspread
from google.oauth2.service_account import Credentials


def make_gspread_client(sa_key_path: str):
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    return gspread.authorize(creds)


def open_or_create_finpay_spreadsheet(gspread_client, title: str):
    """
    Open the report spreadsheet, or create it with the service account if it is
    missing. Access settings are synced on every call so recreated protections
    and Drive permissions stay stable across workflow runs.
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
    Keep the report private and prevent spreadsheet editors from sharing it.

    If the service account is not allowed to manage Drive permissions on an
    existing spreadsheet, these Drive-level settings can fail. The workflow can
    still write sheet values, but the durable fix is to let the service account
    create the workbook first.
    """
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
            f"This is expected if the service account is not the owner: {exc}"
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


def _service_account_email(gspread_client) -> str | None:
    auth = getattr(gspread_client, "auth", None)
    return (
        getattr(auth, "service_account_email", None)
        or getattr(auth, "signer_email", None)
    )


def _split_email_list(value: str | None) -> list[str]:
    if not value:
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
