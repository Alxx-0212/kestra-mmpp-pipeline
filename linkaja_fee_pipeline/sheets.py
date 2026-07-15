"""Google Sheets writer for LinkAja fee summaries."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any

from .processing import (
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    REPORT_DATE_HEADER,
    TOTAL_FEE_HEADER,
    date_sort_key,
)


DEFAULT_ROW_BUFFER = 100
DEFAULT_COL_BUFFER = 5
HEADER_ROW_COUNT = 2
DATA_START_ROW = HEADER_ROW_COUNT + 1
TABLE_HEADERS = [
    REPORT_DATE_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    TOTAL_FEE_HEADER,
]
TABLE_WIDTH = len(TABLE_HEADERS)
SPACER_WIDTH = 1
BLOCK_WIDTH = TABLE_WIDTH + SPACER_WIDTH
KNOWN_CLUSTER_ORDER = ["421306", "421307", "411311", "421315", "421318", "421320"]
INITIAL_CLUSTER_COUNT = len(KNOWN_CLUSTER_ORDER)
STATIC_SHEET_COL_COUNT = INITIAL_CLUSTER_COUNT * BLOCK_WIDTH - SPACER_WIDTH
CLUSTER_START_COLUMNS = {
    cluster_id: index * BLOCK_WIDTH
    for index, cluster_id in enumerate(KNOWN_CLUSTER_ORDER)
}
CLUSTER_DISPLAY_NAMES = {
    "421306": "MRT",
    "421307": "TDR",
    "411311": "PKY",
    "421315": "BGI",
    "421318": "MRW",
    "421320": "TNT",
}
REPORT_DATE_COLUMN_WIDTH = 110
EXPECTED_FEE_COLUMN_WIDTH = 285
IN_CLUSTER_FEE_COLUMN_WIDTH = 300
TOTAL_FEE_COLUMN_WIDTH = 120
SPACER_COLUMN_WIDTH = 24

WHITE_COLOR = {"red": 1, "green": 1, "blue": 1}
BORDER_COLOR = {"red": 0.650, "green": 0.700, "blue": 0.750}
CLUSTER_PALETTES = [
    {
        "title": {"red": 0.122, "green": 0.306, "blue": 0.471},
        "header": {"red": 0.733, "green": 0.835, "blue": 0.922},
        "body": {"red": 0.925, "green": 0.965, "blue": 0.988},
    },
    {
        "title": {"red": 0.220, "green": 0.424, "blue": 0.204},
        "header": {"red": 0.765, "green": 0.890, "blue": 0.741},
        "body": {"red": 0.925, "green": 0.973, "blue": 0.910},
    },
    {
        "title": {"red": 0.494, "green": 0.184, "blue": 0.556},
        "header": {"red": 0.850, "green": 0.765, "blue": 0.890},
        "body": {"red": 0.965, "green": 0.930, "blue": 0.990},
    },
    {
        "title": {"red": 0.749, "green": 0.341, "blue": 0.000},
        "header": {"red": 0.980, "green": 0.820, "blue": 0.604},
        "body": {"red": 1.000, "green": 0.948, "blue": 0.882},
    },
    {
        "title": {"red": 0.525, "green": 0.082, "blue": 0.082},
        "header": {"red": 0.925, "green": 0.690, "blue": 0.690},
        "body": {"red": 0.992, "green": 0.925, "blue": 0.925},
    },
    {
        "title": {"red": 0.000, "green": 0.376, "blue": 0.455},
        "header": {"red": 0.694, "green": 0.867, "blue": 0.902},
        "body": {"red": 0.910, "green": 0.973, "blue": 0.984},
    },
]


def make_gspread_client(sa_key_path: str):
    from google.oauth2.service_account import Credentials
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_file(sa_key_path, scopes=scopes)
    return gspread.authorize(credentials)


def uppercase_sheet_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("="):
        return value
    if value.startswith("'"):
        return "'" + value[1:].upper()
    return value.upper()


def uppercase_sheet_rows(rows: list[list[Any]]) -> list[list[Any]]:
    return [[uppercase_sheet_value(value) for value in row] for row in rows]


def a1_cell(row: int, column: int) -> str:
    column_letter = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        column_letter = chr(65 + remainder) + column_letter
    return f"{column_letter}{row}"


def sheet_range(start_row: int, end_row: int, start_col: int, end_col: int) -> str:
    return f"{a1_cell(start_row, start_col)}:{a1_cell(end_row, end_col)}"


def matching_date_cluster_rows(
    existing_rows: list[list[str]],
    headers: list[str],
    new_rows: list[dict[str, Any]],
) -> list[int]:
    """Return matching rows for the legacy single-table sheet layout."""
    if REPORT_DATE_HEADER not in headers or CLUSTER_ID_HEADER not in headers:
        return []

    report_date_column = headers.index(REPORT_DATE_HEADER)
    cluster_id_column = headers.index(CLUSTER_ID_HEADER)
    replacement_keys = {
        (
            str(row.get(REPORT_DATE_HEADER, "")).strip(),
            str(row.get(CLUSTER_ID_HEADER, "")).strip(),
        )
        for row in new_rows
    }

    return [
        row_number
        for row_number, row in enumerate(existing_rows[1:], start=2)
        if (
            len(row) > max(report_date_column, cluster_id_column)
            and (
                str(row[report_date_column]).strip(),
                str(row[cluster_id_column]).strip(),
            ) in replacement_keys
        )
    ]


def process_linkaja_fee_sheet_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    rows: list[dict[str, Any]],
    gspread_client,
) -> dict[str, Any]:
    spreadsheet = _open_or_create_spreadsheet(gspread_client, target_spreadsheet)
    worksheet = _open_or_create_worksheet(spreadsheet, target_worksheet)
    existing_values = worksheet.get_all_values()
    headers_initialized = setup_linkaja_fee_headers(
        gspread_client,
        spreadsheet,
        worksheet,
        existing_values=existing_values,
    )
    if headers_initialized:
        existing_values = worksheet.get_all_values()

    if not rows:
        return {
            "status": "no_rows",
            "worksheet": target_worksheet,
            "headers_initialized": headers_initialized,
            "replaced_rows": 0,
            "insert_row": None,
            "block_end": None,
            "rows": 0,
            "clusters": [],
        }

    existing_tables = _extract_cluster_tables(existing_values)
    merged_tables, replaced_rows = _merge_replacement_rows(existing_tables, rows)
    changed_clusters = _changed_clusters(rows)

    existing_row_count = len(existing_values)
    row_count = max(
        HEADER_ROW_COUNT,
        existing_row_count,
        max((HEADER_ROW_COUNT + len(merged_tables[cluster_id]) for cluster_id in changed_clusters), default=HEADER_ROW_COUNT),
    )
    _ensure_grid_capacity(spreadsheet, worksheet, row_count, STATIC_SHEET_COL_COUNT)
    _write_cluster_data_blocks(
        worksheet,
        merged_tables,
        changed_clusters,
        row_count,
    )
    _format_linkaja_sheet(
        gspread_client,
        spreadsheet,
        worksheet,
        list(KNOWN_CLUSTER_ORDER),
        row_count,
        STATIC_SHEET_COL_COUNT,
    )

    return {
        "status": "success",
        "worksheet": target_worksheet,
        "headers_initialized": headers_initialized,
        "replaced_rows": replaced_rows,
        "insert_row": DATA_START_ROW,
        "block_end": row_count,
        "rows": len(rows),
        "clusters": changed_clusters,
    }


def setup_linkaja_fee_headers(
    gspread_client,
    spreadsheet,
    worksheet,
    existing_values: list[list[str]] | None = None,
) -> bool:
    existing_values = worksheet.get_all_values() if existing_values is None else existing_values
    if not _worksheet_is_empty(existing_values):
        return False

    _unmerge_all_cells(spreadsheet, worksheet)
    _ensure_grid_capacity(
        spreadsheet,
        worksheet,
        HEADER_ROW_COUNT,
        STATIC_SHEET_COL_COUNT,
    )
    header_values = _build_static_header_values()
    worksheet.update(
        sheet_range(1, HEADER_ROW_COUNT, 1, STATIC_SHEET_COL_COUNT),
        uppercase_sheet_rows(header_values),
        value_input_option="USER_ENTERED",
    )
    _format_linkaja_sheet(
        gspread_client,
        spreadsheet,
        worksheet,
        list(KNOWN_CLUSTER_ORDER),
        HEADER_ROW_COUNT,
        STATIC_SHEET_COL_COUNT,
    )
    return True


def _open_or_create_spreadsheet(gspread_client, title: str):
    import gspread

    try:
        return gspread_client.open(title)
    except gspread.SpreadsheetNotFound:
        return gspread_client.create(title)


def _open_or_create_worksheet(spreadsheet, title: str):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=STATIC_SHEET_COL_COUNT)


def _extract_cluster_tables(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    if not existing_rows:
        return _empty_known_cluster_tables()

    legacy_rows = _extract_legacy_single_table(existing_rows)
    if legacy_rows:
        return _normalize_known_cluster_tables(legacy_rows)

    return _extract_static_side_by_side_tables(existing_rows)


def _extract_legacy_single_table(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    headers = [str(value).strip().upper() for value in existing_rows[0]]
    required_headers = [
        REPORT_DATE_HEADER,
        CLUSTER_ID_HEADER,
        EXPECTED_FEE_HEADER,
        IN_CLUSTER_FEE_HEADER,
        TOTAL_FEE_HEADER,
    ]
    if not all(header in headers for header in required_headers):
        return {}

    columns = {header: headers.index(header) for header in required_headers}
    tables: dict[str, list[dict[str, Any]]] = {}
    for row in existing_rows[1:]:
        report_date = _cell_value(row, columns[REPORT_DATE_HEADER])
        cluster_id = _cell_value(row, columns[CLUSTER_ID_HEADER])
        if not report_date or not cluster_id:
            continue
        tables.setdefault(cluster_id, []).append({
            REPORT_DATE_HEADER: report_date,
            CLUSTER_ID_HEADER: cluster_id,
            EXPECTED_FEE_HEADER: _cell_value(row, columns[EXPECTED_FEE_HEADER]),
            IN_CLUSTER_FEE_HEADER: _cell_value(row, columns[IN_CLUSTER_FEE_HEADER]),
            TOTAL_FEE_HEADER: _cell_value(row, columns[TOTAL_FEE_HEADER]),
        })

    return tables


def _extract_static_side_by_side_tables(existing_rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    if len(existing_rows) < 2:
        return _empty_known_cluster_tables()

    tables = _empty_known_cluster_tables()
    for cluster_id in KNOWN_CLUSTER_ORDER:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        cluster_rows = []
        for row in existing_rows[2:]:
            report_date = _cell_value(row, start_column)
            if not report_date:
                continue
            cluster_rows.append({
                REPORT_DATE_HEADER: report_date,
                CLUSTER_ID_HEADER: cluster_id,
                EXPECTED_FEE_HEADER: _cell_value(row, start_column + 1),
                IN_CLUSTER_FEE_HEADER: _cell_value(row, start_column + 2),
                TOTAL_FEE_HEADER: _cell_value(row, start_column + 3),
            })
        tables[cluster_id] = cluster_rows

    return _sort_cluster_table_rows(tables)


def _cluster_id_from_title(value: str) -> str | None:
    match = re.match(r"\s*(?:CLUSTER\s+|[A-Z]{2,10}\s+-\s+)?(\d+)\s*$", value, re.IGNORECASE)
    return match.group(1) if match else None


def _cluster_title(cluster_id: str) -> str:
    display_name = CLUSTER_DISPLAY_NAMES.get(cluster_id)
    if display_name:
        return f"{display_name} - {cluster_id}"
    return cluster_id


def _merge_replacement_rows(
    existing_tables: dict[str, list[dict[str, Any]]],
    new_rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    tables = _normalize_known_cluster_tables(deepcopy(existing_tables))
    replaced_rows = 0

    for row in new_rows:
        cluster_id = str(row.get(CLUSTER_ID_HEADER, "")).strip()
        report_date = str(row.get(REPORT_DATE_HEADER, "")).strip()
        if not cluster_id:
            raise ValueError(f"LinkAja fee row is missing {CLUSTER_ID_HEADER}")
        if not report_date:
            raise ValueError(f"LinkAja fee row is missing {REPORT_DATE_HEADER}")
        _validate_known_cluster_id(cluster_id)

        existing_rows = tables.get(cluster_id, [])
        kept_rows = [
            existing_row
            for existing_row in existing_rows
            if str(existing_row.get(REPORT_DATE_HEADER, "")).strip() != report_date
        ]
        replaced_rows += len(existing_rows) - len(kept_rows)
        kept_rows.append(_visible_cluster_row(row, cluster_id, report_date))
        tables[cluster_id] = sorted(kept_rows, key=lambda item: date_sort_key(item[REPORT_DATE_HEADER]))

    return _sort_cluster_table_rows(tables), replaced_rows


def _visible_cluster_row(
    row: dict[str, Any],
    cluster_id: str,
    report_date: str,
) -> dict[str, Any]:
    return {
        REPORT_DATE_HEADER: report_date,
        CLUSTER_ID_HEADER: cluster_id,
        EXPECTED_FEE_HEADER: row.get(EXPECTED_FEE_HEADER, ""),
        IN_CLUSTER_FEE_HEADER: row.get(IN_CLUSTER_FEE_HEADER, ""),
        TOTAL_FEE_HEADER: row.get(TOTAL_FEE_HEADER, ""),
    }


def _sort_cluster_table_rows(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        cluster_id: sorted(rows, key=lambda item: date_sort_key(str(item[REPORT_DATE_HEADER])))
        for cluster_id, rows in _normalize_known_cluster_tables(tables).items()
    }


def _empty_known_cluster_tables() -> dict[str, list[dict[str, Any]]]:
    return {cluster_id: [] for cluster_id in KNOWN_CLUSTER_ORDER}


def _normalize_known_cluster_tables(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    normalized = _empty_known_cluster_tables()
    for cluster_id, rows in tables.items():
        _validate_known_cluster_id(cluster_id)
        normalized[cluster_id] = rows
    return normalized


def _validate_known_cluster_id(cluster_id: str) -> None:
    if cluster_id not in KNOWN_CLUSTER_ORDER:
        known_clusters = ", ".join(KNOWN_CLUSTER_ORDER)
        raise ValueError(
            f"Unknown LinkAja cluster ID {cluster_id!r}; expected one of: {known_clusters}"
        )


def _changed_clusters(rows: list[dict[str, Any]]) -> list[str]:
    cluster_ids = {
        str(row.get(CLUSTER_ID_HEADER, "")).strip()
        for row in rows
    }
    for cluster_id in cluster_ids:
        _validate_known_cluster_id(cluster_id)
    return [
        cluster_id
        for cluster_id in KNOWN_CLUSTER_ORDER
        if cluster_id in cluster_ids
    ]


def _worksheet_is_empty(existing_values: list[list[str]]) -> bool:
    return not any(
        str(value).strip()
        for row in existing_values
        for value in row
    )


def _build_static_header_values() -> list[list[Any]]:
    values = [["" for _ in range(STATIC_SHEET_COL_COUNT)] for _ in range(HEADER_ROW_COUNT)]

    for cluster_id in KNOWN_CLUSTER_ORDER:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        values[0][start_column] = _cluster_title(cluster_id)
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[1][start_column + header_offset] = header

    return values


def _write_cluster_data_blocks(
    worksheet,
    tables: dict[str, list[dict[str, Any]]],
    cluster_ids: list[str],
    row_count: int,
) -> None:
    if row_count < DATA_START_ROW:
        return

    for cluster_id in cluster_ids:
        _validate_known_cluster_id(cluster_id)
        start_column = CLUSTER_START_COLUMNS[cluster_id] + 1
        end_column = start_column + TABLE_WIDTH - 1
        values = _cluster_data_block_values(tables[cluster_id], row_count)
        worksheet.update(
            sheet_range(DATA_START_ROW, row_count, start_column, end_column),
            uppercase_sheet_rows(values),
            value_input_option="USER_ENTERED",
        )


def _cluster_data_block_values(
    rows: list[dict[str, Any]],
    row_count: int,
) -> list[list[Any]]:
    data_row_count = row_count - HEADER_ROW_COUNT
    values = [["" for _ in range(TABLE_WIDTH)] for _ in range(data_row_count)]
    for row_index, row in enumerate(rows[:data_row_count]):
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[row_index][header_offset] = row.get(header, "")
    return values


def _build_side_by_side_values(
    tables: dict[str, list[dict[str, Any]]],
) -> tuple[list[list[Any]], list[str]]:
    tables = _normalize_known_cluster_tables(tables)
    clusters = list(KNOWN_CLUSTER_ORDER)
    max_data_rows = max((len(rows) for rows in tables.values()), default=0)
    col_count = INITIAL_CLUSTER_COUNT * BLOCK_WIDTH - SPACER_WIDTH
    row_count = max(2 + max_data_rows, 2)
    values = [["" for _ in range(col_count)] for _ in range(row_count)]

    for cluster_id in clusters:
        start_column = CLUSTER_START_COLUMNS[cluster_id]
        values[0][start_column] = _cluster_title(cluster_id)
        for header_offset, header in enumerate(TABLE_HEADERS):
            values[1][start_column + header_offset] = header

        for row_offset, row in enumerate(tables[cluster_id], start=2):
            for header_offset, header in enumerate(TABLE_HEADERS):
                values[row_offset][start_column + header_offset] = row.get(header, "")

    return values, clusters


def _cell_value(row: list[Any], column_index: int) -> str:
    if column_index >= len(row):
        return ""
    return str(row[column_index]).strip()


def _unmerge_all_cells(spreadsheet, worksheet) -> None:
    row_count = max(int(getattr(worksheet, "row_count", 1) or 1), 1)
    col_count = max(int(getattr(worksheet, "col_count", 1) or 1), 1)
    spreadsheet.batch_update({"requests": [{
        "unmergeCells": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": 0,
                "endRowIndex": row_count,
                "startColumnIndex": 0,
                "endColumnIndex": col_count,
            }
        }
    }]})


def _ensure_grid_capacity(
    spreadsheet,
    worksheet,
    required_rows: int,
    required_cols: int,
) -> None:
    current_rows = int(getattr(worksheet, "row_count", 0) or 0)
    current_cols = int(getattr(worksheet, "col_count", 0) or 0)
    target_rows = max(current_rows, required_rows + DEFAULT_ROW_BUFFER)
    target_cols = max(current_cols, required_cols + DEFAULT_COL_BUFFER)
    if current_rows >= required_rows and current_cols >= required_cols:
        return

    spreadsheet.batch_update({"requests": [{
        "updateSheetProperties": {
            "properties": {
                "sheetId": worksheet.id,
                "gridProperties": {
                    "rowCount": target_rows,
                    "columnCount": target_cols,
                },
            },
            "fields": "gridProperties(rowCount,columnCount)",
        }
    }]})


def _format_linkaja_sheet(
    gspread_client,
    spreadsheet,
    worksheet,
    clusters: list[str],
    row_count: int,
    col_count: int,
) -> None:
    requests = [
        *_delete_all_protected_range_requests(spreadsheet, worksheet),
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"frozenRowCount": 2},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    ]

    for cluster_index, _cluster_id in enumerate(clusters):
        start_column = cluster_index * BLOCK_WIDTH
        end_column = start_column + TABLE_WIDTH
        palette = CLUSTER_PALETTES[cluster_index % len(CLUSTER_PALETTES)]
        requests.extend(_cluster_format_requests(
            worksheet,
            start_column,
            end_column,
            row_count,
            palette,
        ))

        spacer_column = end_column
        if spacer_column < col_count:
            requests.append(
                _column_width_request(
                    worksheet,
                    spacer_column,
                    spacer_column + 1,
                    SPACER_COLUMN_WIDTH,
                )
            )

    protected_sheet_request = _add_protected_sheet_request(
        gspread_client,
        worksheet,
        "LinkAja protected fee summary sheet",
    )
    requests.append(protected_sheet_request)

    spreadsheet.batch_update({"requests": requests})


def _delete_all_protected_range_requests(spreadsheet, worksheet) -> list[dict]:
    metadata = spreadsheet.fetch_sheet_metadata({
        "fields": "sheets(properties(sheetId),protectedRanges(protectedRangeId))",
    })
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != worksheet.id:
            continue
        for protected_range in sheet.get("protectedRanges", []):
            requests.append({
                "deleteProtectedRange": {
                    "protectedRangeId": protected_range["protectedRangeId"],
                }
            })
    return requests


def _add_protected_sheet_request(
    gspread_client,
    worksheet,
    description: str,
) -> dict:
    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": {"sheetId": worksheet.id},
                "description": description,
                "warningOnly": False,
                "editors": {
                    "users": _protection_editor_emails(gspread_client),
                    "domainUsersCanEdit": False,
                },
            }
        }
    }


def _protection_editor_emails(gspread_client) -> list[str]:
    emails = set(_split_email_list(os.environ.get("FINPAY_PROTECTION_EDITOR_EMAILS")))
    emails.update(_split_email_list(os.environ.get("LINKAJA_PROTECTION_EDITOR_EMAILS")))

    service_account_email = _service_account_email(gspread_client)
    if service_account_email:
        emails.add(service_account_email)

    return sorted(emails)


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


def _cluster_format_requests(
    worksheet,
    start_column: int,
    end_column: int,
    row_count: int,
    palette: dict[str, dict[str, float]],
) -> list[dict]:
    body_end_row = max(row_count, 3)
    return [
        {
            "mergeCells": {
                "range": _grid_range(worksheet, 1, 1, start_column + 1, end_column),
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 1, 1, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["title"],
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"foregroundColor": WHITE_COLOR, "bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 2, 2, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["header"],
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 3, body_end_row, start_column + 1, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["body"],
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(worksheet, 3, body_end_row, start_column + 2, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "updateBorders": {
                "range": _grid_range(worksheet, 1, max(row_count, 2), start_column + 1, end_column),
                "top": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "bottom": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "left": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "right": {"style": "SOLID_MEDIUM", "color": BORDER_COLOR},
                "innerHorizontal": {"style": "SOLID", "color": BORDER_COLOR},
                "innerVertical": {"style": "SOLID", "color": BORDER_COLOR},
            }
        },
        _column_width_request(
            worksheet,
            start_column,
            start_column + 1,
            REPORT_DATE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 1,
            start_column + 2,
            EXPECTED_FEE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 2,
            start_column + 3,
            IN_CLUSTER_FEE_COLUMN_WIDTH,
        ),
        _column_width_request(
            worksheet,
            start_column + 3,
            start_column + 4,
            TOTAL_FEE_COLUMN_WIDTH,
        ),
    ]


def _grid_range(
    worksheet,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
) -> dict:
    return {
        "sheetId": worksheet.id,
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,
        "startColumnIndex": start_col - 1,
        "endColumnIndex": end_col,
    }


def _column_width_request(
    worksheet,
    start_column_index: int,
    end_column_index: int,
    pixel_size: int,
) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": worksheet.id,
                "dimension": "COLUMNS",
                "startIndex": start_column_index,
                "endIndex": end_column_index,
            },
            "properties": {"pixelSize": pixel_size},
            "fields": "pixelSize",
        }
    }
