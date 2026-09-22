"""Google Sheets writer for the daily summary worksheet."""
from datetime import datetime

import gspread
import pandas as pd

from .classification import (
    PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY,
    REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY,
    ST_RULE_CUTOFF_DATE,
)
from .sheets_common import (
    _add_protected_range_request,
    _add_protected_sheet_request,
    _contiguous_row_runs,
    _delete_all_protected_range_requests,
    _grid_range,
    _horizontal_border_requests,
    _mandiri_editor_emails,
    ensure_row_capacity,
    open_or_create_finpay_spreadsheet,
    uppercase_sheet_rows,
)


LINKAJA_FEE_SHEET_NAME = "LinkAja"
LINKAJA_FEE_COLUMNS_BY_WORKSHEET = {
    "MRT": ("A", "B", "C"),
    "TDR": ("F", "G", "H"),
    "PKY": ("K", "L", "M"),
    "BGI": ("P", "Q", "R"),
    "MRW": ("U", "V", "W"),
    "TNT": ("Z", "AA", "AB"),
}
LINKAJA_EXPECTED_FEE_LABEL = "LINKAJA EXPECTED RECHARGE OUT CLUSTER FEE"
LINKAJA_IN_CLUSTER_FEE_LABEL = "LINKAJA DIGIPOS B2B TRANSFER IN CLUSTER FEE"
SELLTHRU_SALES_FEE_INVOICE_LABEL = "BIAYA FEE BAR A ST (HOLD)"
LINKAJA_REFERENCE_CLUSTER_IDS = {
    "MRT": "421306",
    "TDR": "421307",
    "PKY": "411311",
    "BGI": "421315",
    "MRW": "421318",
    "TNT": "421320",
}
LINKAJA_REFERENCE_HEADERS = [
    "REPORT DATE",
    "EXPECTED RECHARGE OUT CLUSTER FEE",
    "DIGIPOS B2B TRANSFER IN CLUSTER FEE",
    "TOTAL FEE",
]
LINKAJA_REFERENCE_TABLE_WIDTH = len(LINKAJA_REFERENCE_HEADERS)
LINKAJA_REFERENCE_BLOCK_WIDTH = LINKAJA_REFERENCE_TABLE_WIDTH + 1
LINKAJA_REFERENCE_COLUMN_COUNT = (
    len(LINKAJA_REFERENCE_CLUSTER_IDS) * LINKAJA_REFERENCE_BLOCK_WIDTH - 1
)
LINKAJA_REFERENCE_ROW_COUNT = 2
LINKAJA_REFERENCE_WHITE_COLOR = {"red": 1, "green": 1, "blue": 1}
LINKAJA_REFERENCE_BORDER_COLOR = {"red": 0.650, "green": 0.700, "blue": 0.750}
LINKAJA_REFERENCE_CLUSTER_PALETTES = [
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


def _uses_legacy_st_sales_fee_layout(
    summary_df: pd.DataFrame,
    report_date: str | None = None,
) -> bool:
    """Keep the historical SLSFEE sheet rows for pre-cutover data."""
    target_date = report_date
    if target_date is None and "Transaction_Date" in summary_df.columns:
        target_date = summary_df["Transaction_Date"].max()
    report_date_value = pd.to_datetime(target_date, errors="coerce")
    include_legacy = (
        not pd.isna(report_date_value)
        and report_date_value.date() < ST_RULE_CUTOFF_DATE
    )
    if "Transaction" not in summary_df.columns:
        return include_legacy

    sales_fee_rows = summary_df[
        summary_df["Transaction"].astype(str).eq("SELLTHRUSALESFEE")
    ]
    if sales_fee_rows.empty or "Transaction_Date" not in sales_fee_rows.columns:
        return include_legacy
    sales_fee_dates = pd.to_datetime(
        sales_fee_rows["Transaction_Date"],
        errors="coerce",
    ).dropna()
    return include_legacy or any(
        value.date() < ST_RULE_CUTOFF_DATE for value in sales_fee_dates
    )


def _linkaja_reference_sheet_is_empty(values: list[list[str]]) -> bool:
    return not any(
        str(value).strip()
        for row in values
        for value in row
    )


def _linkaja_reference_header_values() -> list[list[str]]:
    values = [
        ["" for _ in range(LINKAJA_REFERENCE_COLUMN_COUNT)]
        for _ in range(LINKAJA_REFERENCE_ROW_COUNT)
    ]
    for index, (worksheet_name, cluster_id) in enumerate(
        LINKAJA_REFERENCE_CLUSTER_IDS.items()
    ):
        start_column = index * LINKAJA_REFERENCE_BLOCK_WIDTH
        values[0][start_column] = f"{worksheet_name} - {cluster_id}"
        for header_offset, header in enumerate(LINKAJA_REFERENCE_HEADERS):
            values[1][start_column + header_offset] = header
    return values


def _linkaja_reference_headers_match(values: list[list[str]]) -> bool:
    expected = uppercase_sheet_rows(_linkaja_reference_header_values())
    for row_index, expected_row in enumerate(expected):
        actual_row = values[row_index] if row_index < len(values) else []
        for column_index, expected_value in enumerate(expected_row):
            actual_value = (
                actual_row[column_index]
                if column_index < len(actual_row)
                else ""
            )
            if (
                str(actual_value).strip().upper()
                != str(expected_value).strip().upper()
            ):
                return False
    return True


def _linkaja_reference_sheet_setup_requests(ws) -> list[dict]:
    widths = {
        0: 110,
        1: 285,
        2: 300,
        3: 120,
        4: 24,
    }
    requests = [{
        "updateSheetProperties": {
            "properties": {
                "sheetId": ws.id,
                "gridProperties": {
                    "rowCount": max(int(getattr(ws, "row_count", 0) or 0), 1000),
                    "columnCount": max(
                        int(getattr(ws, "col_count", 0) or 0),
                        LINKAJA_REFERENCE_COLUMN_COUNT,
                    ),
                    "frozenRowCount": LINKAJA_REFERENCE_ROW_COUNT,
                },
            },
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
        }
    }]
    for block_index in range(len(LINKAJA_REFERENCE_CLUSTER_IDS)):
        block_start = block_index * LINKAJA_REFERENCE_BLOCK_WIDTH
        for offset, width in widths.items():
            column_index = block_start + offset
            if column_index >= LINKAJA_REFERENCE_COLUMN_COUNT:
                continue
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": column_index,
                        "endIndex": column_index + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            })
    return requests


def _linkaja_reference_cluster_format_requests(
    ws,
    start_column_index: int,
    palette: dict[str, dict[str, float]],
) -> list[dict]:
    end_column_index = start_column_index + LINKAJA_REFERENCE_TABLE_WIDTH
    start_column = start_column_index + 1
    end_column = end_column_index
    body_end_row = max(LINKAJA_REFERENCE_ROW_COUNT, 3)

    return [
        {
            "unmergeCells": {
                "range": _grid_range(ws, 1, 1, start_column, end_column),
            }
        },
        {
            "mergeCells": {
                "range": _grid_range(ws, 1, 1, start_column, end_column),
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(ws, 1, 1, start_column, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["title"],
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "foregroundColor": LINKAJA_REFERENCE_WHITE_COLOR,
                            "bold": True,
                        },
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,horizontalAlignment,"
                    "verticalAlignment,textFormat)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(ws, 2, 2, start_column, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["header"],
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,horizontalAlignment,"
                    "verticalAlignment,textFormat,wrapStrategy)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(ws, 3, body_end_row, start_column, end_column),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": palette["body"],
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,horizontalAlignment,"
                    "verticalAlignment)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(
                    ws,
                    3,
                    body_end_row,
                    start_column + 1,
                    end_column,
                ),
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
                "range": _grid_range(
                    ws,
                    1,
                    LINKAJA_REFERENCE_ROW_COUNT,
                    start_column,
                    end_column,
                ),
                "top": {
                    "style": "SOLID_MEDIUM",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
                "bottom": {
                    "style": "SOLID_MEDIUM",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
                "left": {
                    "style": "SOLID_MEDIUM",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
                "right": {
                    "style": "SOLID_MEDIUM",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
                "innerHorizontal": {
                    "style": "SOLID",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
                "innerVertical": {
                    "style": "SOLID",
                    "color": LINKAJA_REFERENCE_BORDER_COLOR,
                },
            }
        },
    ]


def _linkaja_reference_sheet_format_requests(ws) -> list[dict]:
    requests = []
    for index in range(len(LINKAJA_REFERENCE_CLUSTER_IDS)):
        start_column = index * LINKAJA_REFERENCE_BLOCK_WIDTH
        palette = LINKAJA_REFERENCE_CLUSTER_PALETTES[
            index % len(LINKAJA_REFERENCE_CLUSTER_PALETTES)
        ]
        requests.extend(
            _linkaja_reference_cluster_format_requests(
                ws,
                start_column,
                palette,
            )
        )
    return requests


def ensure_linkaja_fee_reference_sheet(gspread_client, spreadsheet) -> bool:
    created = False
    try:
        worksheet = spreadsheet.worksheet(LINKAJA_FEE_SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=LINKAJA_FEE_SHEET_NAME,
            rows=1000,
            cols=LINKAJA_REFERENCE_COLUMN_COUNT,
        )
        created = True

    existing_values = (
        []
        if created
        else worksheet.get("A1:AC3", value_render_option="FORMATTED_VALUE")
    )
    existing_header_values = existing_values[:LINKAJA_REFERENCE_ROW_COUNT]
    if not _linkaja_reference_sheet_is_empty(existing_header_values):
        existing_data_preview = existing_values[LINKAJA_REFERENCE_ROW_COUNT:]
        if (
            _linkaja_reference_headers_match(existing_header_values)
            and _linkaja_reference_sheet_is_empty(existing_data_preview)
        ):
            spreadsheet.batch_update({
                "requests": [
                    *_linkaja_reference_sheet_setup_requests(worksheet),
                    *_linkaja_reference_sheet_format_requests(worksheet),
                ]
            })
        return False

    header_rows = uppercase_sheet_rows(_linkaja_reference_header_values())
    row_values = []
    for row in header_rows:
        row_values.append({
            "values": [
                {"userEnteredValue": {"stringValue": str(value)}}
                if str(value) else {}
                for value in row
            ]
        })

    spreadsheet.batch_update({
        "requests": [
            *_linkaja_reference_sheet_setup_requests(worksheet),
            {
                "updateCells": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": 0,
                        "endRowIndex": LINKAJA_REFERENCE_ROW_COUNT,
                        "startColumnIndex": 0,
                        "endColumnIndex": LINKAJA_REFERENCE_COLUMN_COUNT,
                    },
                    "rows": row_values,
                    "fields": "userEnteredValue",
                }
            },
            *_linkaja_reference_sheet_format_requests(worksheet),
            _add_protected_sheet_request(
                gspread_client,
                worksheet,
                "FinPay initialized LinkAja reference sheet",
            ),
        ]
    })
    return True


def setup_initial_headers_and_saldo(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    starting_date_str: str,
    starting_balance: int,
) -> None:
    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)
    ws = sh.worksheet(target_worksheet)

    col_header = {"red": 0.122, "green": 0.306, "blue": 0.471}

    # Column widths
    widths = {
        0: 110,  # REPORT DATE
        1: 130,  # SECTION
        2: 410,  # KETERANGAN
        3: 140,  # DEBET
        4: 190,  # KREDIT
        5: 140,  # SALDO
        6: 56,   # spacer
        7: 110,  # INVOICE REPORT DATE
        8: 410,  # INVOICE REPORT
        9: 140,  # INVOICE DEBET
        10: 140, # INVOICE KREDIT
    }
    sh.batch_update({"requests": [
        {
            "clearBasicFilter": {
                "sheetId": ws.id,
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        *_horizontal_border_requests(
            ws, 1, 1, 1, 11, style="SOLID_MEDIUM", color=col_header
        ),
        *_horizontal_border_requests(ws, 2, 2, 1, 11),
        *[
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            }
            for i, px in widths.items()
        ],
    ]})

    date_display = pd.to_datetime(starting_date_str).strftime("%d/%m/%Y")
    ws.update("A1:K2", uppercase_sheet_rows([
        [
            "REPORT DATE", "SECTION", "KETERANGAN", "DEBET", "KREDIT", "SALDO",
            "", "REPORT DATE", "INVOICE REPORT", "DEBET", "KREDIT",
        ],
        [date_display, "", f"SALDO {date_display}", "", "", starting_balance,
         "", "", "", "", ""],
    ]), value_input_option="USER_ENTERED")

    protection_request = _add_protected_sheet_request(
        gspread_client,
        ws,
        "FinPay protected opening balance",
    )
    requests = [
        *_delete_all_protected_range_requests(sh, ws),
        {
            "repeatCell": {
                "range": _grid_range(ws, 1, 2, 1, 11),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(ws, 1, 1, 1, 11),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid_range(ws, 2, 2, 6, 6),
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {
                            "type": "NUMBER",
                            "pattern": "#,##0;(#,##0);-",
                        }
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        *([protection_request] if protection_request else []),
    ]
    if requests:
        sh.batch_update({"requests": requests})


def _build_qrisduwit_invoice_rows(
    qrisduwit_df: pd.DataFrame | None,
) -> list[tuple[str, int | float, str]]:
    if qrisduwit_df is None or qrisduwit_df.empty:
        return []
    if "Kredit" not in qrisduwit_df.columns:
        raise KeyError("Kredit column is required for QRISDUWIT invoice rows.")

    if "Disbursement Date" in qrisduwit_df.columns:
        disbursement_values = qrisduwit_df["Disbursement Date"]
    else:
        disbursement_values = pd.Series(
            [""] * len(qrisduwit_df),
            index=qrisduwit_df.index,
        )

    labels = []
    label_sort_keys = {}
    missing_label = "MISSING DISBURSEMENT DATE"
    for value in disbursement_values:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            label = missing_label
            sort_key = (2, "")
        else:
            text = str(value).strip()
            parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
            if not text or pd.isna(parsed):
                label = missing_label if not text else text
                sort_key = (2, label) if label == missing_label else (1, label)
            else:
                label = parsed.strftime("%d/%m/%Y")
                sort_key = (0, parsed.date())
        labels.append(label)
        label_sort_keys.setdefault(label, sort_key)

    amounts = pd.to_numeric(qrisduwit_df["Kredit"], errors="coerce").fillna(0)
    grouped = amounts.groupby(pd.Series(labels, index=qrisduwit_df.index)).sum()
    rows = []
    for label in sorted(grouped.index, key=lambda item: label_sort_keys[item]):
        amount = float(grouped[label])
        amount_cell = int(amount) if amount.is_integer() else amount
        rows.append((f"QRISDUWIT - {label}", amount_cell, ""))
    return rows


def _cash_invoice_rows_with_qrisduwit(
    cash_report_rows: list[tuple],
    qrisduwit_report_rows: list[tuple[str, int | float, str]],
) -> list[tuple]:
    rows = list(cash_report_rows)
    if qrisduwit_report_rows:
        rows.append(("QRISDUWIT", "", ""))
        rows.extend(qrisduwit_report_rows)
    return rows


def _quoted_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _linkaja_fee_lookup_formula(
    target_worksheet: str,
    formatted_date: str,
    fee_type: str,
) -> str:
    worksheet_key = target_worksheet.strip().upper()
    if worksheet_key not in LINKAJA_FEE_COLUMNS_BY_WORKSHEET:
        known = ", ".join(LINKAJA_FEE_COLUMNS_BY_WORKSHEET)
        raise KeyError(
            f"No static LinkAja fee columns configured for worksheet "
            f"{target_worksheet!r}. Expected one of: {known}"
        )

    date_column, expected_fee_column, in_cluster_fee_column = (
        LINKAJA_FEE_COLUMNS_BY_WORKSHEET[worksheet_key]
    )
    if fee_type == "expected":
        value_column = expected_fee_column
    elif fee_type == "in_cluster":
        value_column = in_cluster_fee_column
    else:
        raise ValueError(f"Unknown LinkAja fee type: {fee_type!r}")

    report_date = datetime.strptime(formatted_date, "%d/%m/%Y")
    date_criterion = (
        f"DATE({report_date.year},{report_date.month},{report_date.day})"
    )
    sheet_name = _quoted_sheet_name(LINKAJA_FEE_SHEET_NAME)
    return (
        f"=IFERROR(SUMIF("
        f"{sheet_name}!{date_column}:{date_column},"
        f"{date_criterion},"
        f"{sheet_name}!{value_column}:{value_column}"
        "),0)"
    )


def _build_linkaja_fee_invoice_rows(
    target_worksheet: str,
    formatted_date: str,
) -> list[tuple[str, str, str]]:
    return [
        (
            LINKAJA_EXPECTED_FEE_LABEL,
            "",
            _linkaja_fee_lookup_formula(target_worksheet, formatted_date, "expected"),
        ),
        (
            LINKAJA_IN_CLUSTER_FEE_LABEL,
            "",
            _linkaja_fee_lookup_formula(target_worksheet, formatted_date, "in_cluster"),
        ),
    ]


def _next_transfer_mandiri_formula() -> str:
    next_debet = 'INDIRECT("D"&ROW()+2&":D")'
    next_kredit = 'INDIRECT("E"&ROW()+2&":E")'
    next_saldo = 'INDIRECT("F"&ROW()+2&":F")'
    next_section = 'INDIRECT("B"&ROW()+2&":B")'
    next_label = 'INDIRECT("C"&ROW()+2&":C")'
    return (
        f'=IFERROR(INDEX(FILTER({next_kredit},'
        f'TRIM({next_label})="TRANSFER MASUK DARI FINPAY",'
        f'{next_debet}<>"CASHOUT APOLLO",'
        f'{next_kredit}<>""),1),'
        f'IFERROR(INDEX(FILTER({next_saldo},'
        f'TRIM({next_label})="TRANSFER MASUK DARI FINPAY",'
        f'{next_debet}="CASHOUT APOLLO",'
        f'{next_saldo}<>""),1),'
        f'IFERROR(INDEX(FILTER({next_debet},'
        f'TRIM({next_section})="TRANSFER MASUK DARI FINPAY",'
        f'{next_debet}<>""),1),0)))'
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4b  append daily block
# ─────────────────────────────────────────────────────────────────────────────

def append_daily_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    summary_df: pd.DataFrame,
    qrisduwit_df: pd.DataFrame | None = None,
    report_date: str | None = None,
) -> tuple[int | None, int | None]:
    """
    Returns (insert_row, block_end) on success.
    Replaces the existing daily block for the same date on reruns.
    """
    target_date_str = report_date or summary_df["Transaction_Date"].max()
    formatted_date  = datetime.strptime(target_date_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    include_legacy_st_sales_fee = _uses_legacy_st_sales_fee_layout(
        summary_df,
        report_date=report_date,
    )

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=6):
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    COL_DETAIL_ALT = {"red": 0.965, "green": 0.980, "blue": 0.992}
    COL_CASH_HEADER = {"red": 0.820, "green": 0.910, "blue": 0.800}
    COL_CASH_BODY = {"red": 0.925, "green": 0.973, "blue": 0.910}
    COL_SELLTHRU_HEADER = {"red": 0.860, "green": 0.895, "blue": 1.000}
    COL_SELLTHRU_BODY = {"red": 0.940, "green": 0.955, "blue": 1.000}
    COL_ACCOUNTING_HEADER = {"red": 0.980, "green": 0.900, "blue": 0.700}
    COL_ACCOUNTING_BODY = {"red": 1.000, "green": 0.965, "blue": 0.840}
    COL_TABLE_BORDER = {"red": 0.650, "green": 0.700, "blue": 0.750}
    COL_STATUS = {"red": 0.965, "green": 0.930, "blue": 0.990}
    COL_FOOTER_HEADER = {"red": 0.800, "green": 0.880, "blue": 0.950}
    COL_FOOTER_BODY = {"red": 0.900, "green": 0.940, "blue": 0.980}
    COL_INPUT = COL_STATUS
    COL_DEFAULT_TEXT = {"red": 0, "green": 0, "blue": 0}
    COL_MUTED_TEXT = {"red": 0.420, "green": 0.420, "blue": 0.420}
    INVOICE_DETAIL_START_OFFSET = 0

    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)
    ws = sh.worksheet(target_worksheet)
    existing_values = ws.get_all_values()

    def _grid_range(sr: int, er: int, sc: int, ec: int) -> dict:
        return {
            "sheetId": ws.id,
            "startRowIndex": sr - 1,
            "endRowIndex": er,
            "startColumnIndex": sc - 1,
            "endColumnIndex": ec,
        }

    def _format_cell_request(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
        cell_format: dict,
        fields: str,
    ) -> dict | None:
        if start_row > end_row or start_col > end_col:
            return None
        return {
            "repeatCell": {
                "range": _grid_range(start_row, end_row, start_col, end_col),
                "cell": {"userEnteredFormat": cell_format},
                "fields": fields,
            }
        }

    def _header_value_request(headers: list[str]) -> dict:
        return {
            "updateCells": {
                "range": _grid_range(1, 1, 1, len(headers)),
                "rows": [{
                    "values": [
                        {"userEnteredValue": {"stringValue": str(value)}}
                        if str(value)
                        else {}
                        for value in uppercase_sheet_rows([headers])[0]
                    ]
                }],
                "fields": "userEnteredValue",
            }
        }

    def _invoice_table_border_requests(
        start_row: int,
        end_row: int,
    ) -> list[dict]:
        if start_row > end_row:
            return []

        outer = {"style": "SOLID_MEDIUM", "color": COL_HEADER}
        inner = {"style": "SOLID", "color": COL_TABLE_BORDER}
        return [{
            "updateBorders": {
                "range": _grid_range(start_row, end_row, 8, 11),
                "top": outer,
                "bottom": outer,
                "left": outer,
                "right": outer,
                "innerHorizontal": inner,
                "innerVertical": inner,
            }
        }]

    def _cell_grid_border_requests(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
    ) -> list[dict]:
        if start_row > end_row or start_col > end_col:
            return []

        border = {"style": "SOLID", "color": COL_TABLE_BORDER}
        return [{
            "updateBorders": {
                "range": _grid_range(start_row, end_row, start_col, end_col),
                "top": border,
                "bottom": border,
                "left": border,
                "right": border,
                "innerHorizontal": border,
                "innerVertical": border,
            }
        }]

    def _clear_cell_grid_border_requests(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
    ) -> list[dict]:
        if start_row > end_row or start_col > end_col:
            return []

        border = {"style": "NONE"}
        return [{
            "updateBorders": {
                "range": _grid_range(start_row, end_row, start_col, end_col),
                "top": border,
                "bottom": border,
                "left": border,
                "right": border,
                "innerHorizontal": border,
                "innerVertical": border,
            }
        }]

    def _merge_range_requests(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
    ) -> list[dict]:
        merge_range = _grid_range(start_row, end_row, start_col, end_col)
        return [
            {"unmergeCells": {"range": merge_range}},
            {"mergeCells": {"range": merge_range, "mergeType": "MERGE_ALL"}},
        ]

    def _unmerge_range_requests(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
    ) -> list[dict]:
        return [{
            "unmergeCells": {
                "range": _grid_range(start_row, end_row, start_col, end_col),
            }
        }]

    def _delete_row_requests(row_numbers: list[int]) -> list[dict]:
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
        return requests

    def _insert_row_request(start_row: int, row_count: int) -> dict | None:
        if row_count <= 0:
            return None
        return {
            "insertDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": start_row - 1,
                    "endIndex": start_row - 1 + row_count,
                },
                "inheritFromBefore": start_row > 1,
            }
        }

    def _ranges_intersect(
        first_start: int,
        first_end: int,
        second_start: int,
        second_end: int,
    ) -> bool:
        return first_start < second_end and first_end > second_start

    def _merged_invoice_unmerge_requests(row_numbers: list[int]) -> list[dict]:
        if not row_numbers:
            return []

        row_start_index = min(row_numbers) - 1
        row_end_index = max(row_numbers)
        invoice_start_index = 7
        invoice_end_index = 11
        metadata = sh.fetch_sheet_metadata({
            "fields": "sheets(properties(sheetId),merges)",
        })
        requests = []
        for sheet in metadata.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != ws.id:
                continue
            for merged_range in sheet.get("merges", []):
                merge_start_row = merged_range.get("startRowIndex", 0)
                merge_end_row = merged_range.get(
                    "endRowIndex",
                    merge_start_row + 1,
                )
                merge_start_col = merged_range.get("startColumnIndex", 0)
                merge_end_col = merged_range.get(
                    "endColumnIndex",
                    merge_start_col + 1,
                )
                if not _ranges_intersect(
                    merge_start_row,
                    merge_end_row,
                    row_start_index,
                    row_end_index,
                ):
                    continue
                if not _ranges_intersect(
                    merge_start_col,
                    merge_end_col,
                    invoice_start_index,
                    invoice_end_index,
                ):
                    continue
                exact_range = dict(merged_range)
                exact_range["sheetId"] = ws.id
                requests.append({"unmergeCells": {"range": exact_range}})
        return requests

    def _has_blank_invoice_cells(row: list) -> bool:
        return all(
            str(row[idx]).strip() == "" if idx < len(row) else True
            for idx in range(7, 11)
        )

    def _blank_invoice_block_ranges(
        start_row: int,
        rows: list[list],
    ) -> list[tuple[int, int]]:
        blocks = []
        block_start = None
        for offset, row in enumerate(rows):
            row_number = start_row + offset
            if _has_blank_invoice_cells(row):
                if block_start is None:
                    block_start = row_number
                continue

            if block_start is not None:
                blocks.append((block_start, row_number - 1))
                block_start = None

        if block_start is not None:
            blocks.append((block_start, start_row + len(rows) - 1))

        return blocks

    def _blank_invoice_block_merge_requests(
        blocks: list[tuple[int, int]],
    ) -> list[dict]:
        requests = []
        for start_row, end_row in blocks:
            requests.extend(_merge_range_requests(start_row, end_row, 8, 11))
        return requests

    def _blank_invoice_block_border_requests(
        blocks: list[tuple[int, int]],
    ) -> list[dict]:
        clear_border = {"style": "NONE"}
        side_border = {"style": "SOLID_MEDIUM", "color": COL_HEADER}
        requests = []
        for block_index, (start_row, end_row) in enumerate(blocks):
            requests.append({
                "updateBorders": {
                    "range": _grid_range(start_row, end_row, 8, 11),
                    "top": side_border if block_index == 0 else clear_border,
                    "bottom": clear_border,
                    "left": side_border,
                    "right": side_border,
                    "innerHorizontal": clear_border,
                    "innerVertical": clear_border,
                }
            })
        return requests

    def _daily_detail_start_rows(values: list[list[str]]) -> list[int]:
        starts = []
        previous_date = ""
        previous_section = ""
        for idx, row in enumerate(values, start=1):
            date_text = str(row[0]).strip() if row else ""
            section = str(row[1]).strip().upper() if len(row) > 1 else ""
            if (
                date_text
                and section == "DETAIL"
                and (previous_date != date_text or previous_section != "DETAIL")
            ):
                starts.append(idx)
            previous_date = date_text
            previous_section = section
        return starts

    def _summary_block_top_border_requests(
        detail_start_rows: list[int],
        max_row: int,
    ) -> list[dict]:
        requests = []
        for row in sorted(set(detail_start_rows)):
            if row > max_row:
                continue
            requests.extend(_horizontal_border_requests(
                ws,
                row,
                row,
                1,
                6,
                top=True,
                bottom=False,
                style="SOLID_MEDIUM",
                color=COL_HEADER,
            ))
            requests.extend(_horizontal_border_requests(
                ws,
                row,
                row,
                8,
                11,
                top=True,
                bottom=False,
                style="SOLID_MEDIUM",
                color=COL_HEADER,
            ))
        return requests

    def _existing_mandiri_cells(values: list[list[str]]) -> list[tuple[int, int]]:
        cells = []
        for idx, row in enumerate(values, start=1):
            if (
                len(row) > 2
                and str(row[1]).strip().upper() == "CASH"
                and str(row[2]).strip().upper() == "MANDIRI"
            ):
                # Current layout: KETERANGAN is column C and MANDIRI input is D.
                # Do not expose column C from MANDIRI invoice/report rows.
                cells.append((idx, 4))
        return cells

    def _matching_summary_date_block_rows(
        values: list[list[str]],
        date_text: str,
    ) -> list[int]:
        markers = [
            (idx, str(row[0]).strip())
            for idx, row in enumerate(values, start=1)
            if row and str(row[0]).strip()
        ]
        rows_to_delete = []
        i = 0
        while i < len(markers):
            row_number, marker_value = markers[i]
            if marker_value != date_text:
                i += 1
                continue

            start = row_number
            j = i + 1
            while j < len(markers) and markers[j][1] == date_text:
                j += 1

            end = markers[j][0] - 1 if j < len(markers) else len(values)
            rows_to_delete.extend(range(start, end + 1))
            i = j

        return rows_to_delete

    def _summary_protection_requests(
        end_row: int,
        mandiri_cells: list[tuple[int, int]],
    ) -> list[dict]:
        unique_mandiri_cells = [
            (row, col) for row, col in sorted(set(mandiri_cells))
            if row <= end_row
        ]
        protection_request = _add_protected_sheet_request(
            gspread_client,
            ws,
            "FinPay protected summary sheet",
            unprotected_ranges=[
                (row, row, col, col)
                for row, col in unique_mandiri_cells
            ],
        )
        mandiri_editor_emails = _mandiri_editor_emails()
        mandiri_protection_requests = []
        if mandiri_editor_emails:
            mandiri_protection_requests = [
                request
                for request in (
                    _add_protected_range_request(
                        gspread_client,
                        ws,
                        row,
                        row,
                        col,
                        col,
                        f"FinPay protected MANDIRI input row {row}",
                        extra_editor_emails=mandiri_editor_emails,
                    )
                    for row, col in unique_mandiri_cells
                )
                if request
            ]
        return [
            *_delete_all_protected_range_requests(sh, ws),
            *([protection_request] if protection_request else []),
            *mandiri_protection_requests,
        ]

    replacement_rows = _matching_summary_date_block_rows(
        existing_values,
        formatted_date,
    )
    if replacement_rows:
        replacement_start = min(replacement_rows)
        replacement_row_numbers = set(replacement_rows)
        existing_values = [
            row
            for row_number, row in enumerate(existing_values, start=1)
            if row_number not in replacement_row_numbers
        ]
        print(
            f"↻ Replacing existing summary block for {formatted_date} "
            f"({len(replacement_rows)} rows)."
        )
    else:
        replacement_start = None

    # Build value map
    val_map = {
        str(row["Transaction"]).strip(): {
            "debet":  float(row.get("Sum_of_Debet",  0) or 0),
            "kredit": float(row.get("Sum_of_Kredit", 0) or 0),
            "count":  int(row.get("Transaction_Count", 0) or 0),
        }
        for _, row in summary_df.iterrows()
    }

    summary_headers = [
        "REPORT DATE",
        "SECTION",
        "KETERANGAN",
        "DEBET",
        "KREDIT",
        "SALDO",
        "",
        "REPORT DATE",
        "INVOICE REPORT",
        "DEBET",
        "KREDIT",
    ]

    insert_row = replacement_start or len(existing_values) + 1
    r = insert_row
    rows_to_append = []

    pembelian_count = val_map.get(
        PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
        {},
    ).get("count", 0)
    reversal_pembelian_count = val_map.get(
        REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
        {},
    ).get("count", 0)

    detail_rows = [
        {"label": "TRANSFER MASUK DARI FINPAY", "key": "CASHOUT APOLLO"},
        {"label": "QRISDUWIT",                  "key": "QRISDUWIT"},
        {"label": "DISBURSEMENT",               "key": "DISBURSEMENT"},
        {"label": "PPOB",                       "key": "FeeTransaksi"},
        {"label": "NGRS",                       "key": "RECHARGE"},
        {"label": "BIAYA FEE NGRS",             "key": "RECHARGEFEE"},
        {
            "label": "Pembelian Recharge Out Cluster",
            "key": PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
        },
        {"label": "RECHARGE OUT CLUSTER",       "key": "RECHARGE OUT CLUSTER"},
        {"label": "RECHARGE OUT CLUSTER FEE",   "key": "RECHARGE OUT CLUSTER FEE"},
        {"label": "REVERSAL NGRS",              "key": REVERSAL_NGRS_CATEGORY},
        {"label": "REVERSAL NGRS FEE",          "key": REVERSAL_NGRS_FEE_CATEGORY},
        {
            "label": "Reversal - PEMBELIAN RECHARGE OUT CLUSTER",
            "key": REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
        },
        {
            "label": "REVERSAL RECHARGE OUT CLUSTER",
            "key": REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
        },
        {
            "label": "REVERSAL RECHARGE OUT CLUSTER FEE",
            "key": REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY,
        },
        {"label": "ST",                         "key": "SELLTHRU"},
        {"label": "BIAYA FEE ST",               "key": "SELLTHRUFEE"},
        {
            "label": "Jumlah Pembelian Recharge Out Cluster",
            "debet": pembelian_count,
            "kredit": "",
            "balance": "empty",
        },
        {
            "label": "Expected Biaya Pembelian Recharge Out Cluster FEE",
            "debet": "",
            "kredit": lambda row_by_label: (
                f"=D{row_by_label['Jumlah Pembelian Recharge Out Cluster']}*200"
            ),
            "balance": "empty",
        },
        {
            "label": "Jumlah Reversal - PEMBELIAN RECHARGE OUT CLUSTER",
            "debet": reversal_pembelian_count,
            "kredit": "",
            "balance": "empty",
        },
    ]
    if include_legacy_st_sales_fee:
        detail_rows.insert(
            16,
            {
                "label": SELLTHRU_SALES_FEE_INVOICE_LABEL,
                "key": "SELLTHRUSALESFEE",
            },
        )

    def _summary_row(
        section: str,
        label: str,
        debet="",
        kredit="",
        saldo="",
    ) -> list:
        return [formatted_date, section, label, debet, kredit, saldo]

    detail_start = r
    first = True
    detail_row_by_key = {}
    detail_row_by_label = {
        row["label"] if isinstance(row, dict) else row[0]: insert_row + idx
        for idx, row in enumerate(detail_rows)
    }

    def _resolve_detail_cell(value):
        return value(detail_row_by_label) if callable(value) else value

    for row in detail_rows:
        if isinstance(row, dict):
            label = str(row["label"])
            key = row.get("key")
            if key is not None:
                detail_row_by_key[str(key)] = r
                d = val_map.get(str(key), {})
                debet = float(d.get("debet", 0))
                kredit = float(d.get("kredit", 0))
                debet_cell = kredit
                kredit_cell = debet
            else:
                debet_cell = _resolve_detail_cell(row.get("debet", ""))
                kredit_cell = _resolve_detail_cell(row.get("kredit", ""))
        else:
            label, debet_cell, kredit_cell = row
            debet_cell = _resolve_detail_cell(debet_cell)
            kredit_cell = _resolve_detail_cell(kredit_cell)
        if first:
            prev_new = f"F$1:F{r - 1}"
            prev_wide = f"G$1:G{r - 1}"
            prev_legacy = f"E$1:E{r - 1}"
            saldo_formula = (
                f"=IFERROR(INDEX({prev_new},MATCH(9.99E+307,{prev_new})),"
                f"IFERROR(INDEX({prev_wide},MATCH(9.99E+307,{prev_wide})),"
                f"IFERROR(INDEX({prev_legacy},MATCH(9.99E+307,{prev_legacy})),0)))"
                f"+D{r}-E{r}"
            )
            first = False
        elif isinstance(row, dict) and row.get("balance") == "empty":
            saldo_formula = ""
        else:
            saldo_formula = f"=F{r - 1}+D{r}-E{r}"
        rows_to_append.append(
            _summary_row("DETAIL", label, debet_cell, kredit_cell, saldo_formula)
        )
        r += 1
    detail_end = r - 1

    def _split_net_formula(net_formula: str) -> tuple[str, str]:
        expression = net_formula[1:] if net_formula.startswith("=") else net_formula
        return f"=MAX(({expression}),0)", f"=MAX(-({expression}),0)"

    def _detail_net_formula(key: str) -> str:
        row = detail_row_by_key[key]
        return f"=D{row}-E{row}"

    def _detail_label_net_formula(label: str) -> str:
        row = detail_row_by_label[label]
        return f"=D{row}-E{row}"

    cash_report_rows = [
        ("NGRS", *_split_net_formula(_detail_net_formula("RECHARGE"))),
        ("Recharge Fee", *_split_net_formula(_detail_net_formula("RECHARGEFEE"))),
        (
            "Pembelian Recharge Out Cluster",
            *_split_net_formula(
                _detail_net_formula(PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY)
            ),
        ),
        (
            "Expected Biaya Pembelian Recharge Out Cluster FEE",
            *_split_net_formula(
                _detail_label_net_formula(
                    "Expected Biaya Pembelian Recharge Out Cluster FEE"
                )
            ),
        ),
    ]
    cash_report_rows.extend(
        _build_linkaja_fee_invoice_rows(target_worksheet, formatted_date)
    )
    qrisduwit_report_rows = _build_qrisduwit_invoice_rows(qrisduwit_df)
    cash_report_rows = _cash_invoice_rows_with_qrisduwit(
        cash_report_rows,
        qrisduwit_report_rows,
    )

    sellthru_report_rows = [
        ("ST", *_split_net_formula(_detail_net_formula("SELLTHRU"))),
        ("BIAYA FEE ST", *_split_net_formula(_detail_net_formula("SELLTHRUFEE"))),
    ]
    if include_legacy_st_sales_fee:
        sellthru_report_rows.append(
            (
                SELLTHRU_SALES_FEE_INVOICE_LABEL,
                *_split_net_formula(_detail_net_formula("SELLTHRUSALESFEE")),
            )
        )

    accounting_report_rows = [
        ("PPOB", *_split_net_formula(_detail_net_formula("FeeTransaksi"))),
        ("DISBURSEMENT", *_split_net_formula(_detail_net_formula("DISBURSEMENT"))),
        (
            "Recharge Out Cluster",
            *_split_net_formula(_detail_net_formula("RECHARGE OUT CLUSTER")),
        ),
        (
            "Recharge Out Cluster FEE",
            *_split_net_formula(_detail_net_formula("RECHARGE OUT CLUSTER FEE")),
        ),
        (
            "Reversal - Recharge Out Cluster",
            *_split_net_formula(
                _detail_net_formula(REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY)
            ),
        ),
        (
            "Reversal - Recharge Out Cluster FEE",
            *_split_net_formula(
                _detail_net_formula(REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY)
            ),
        ),
    ]

    invoice_sections = [
        {
            "title": "CASH IN - NGRS",
            "rows": cash_report_rows,
            "header_color": COL_CASH_HEADER,
            "body_color": COL_CASH_BODY,
        },
    ]
    invoice_sections.extend([
        {
            "title": "SELLTHRU",
            "rows": sellthru_report_rows,
            "header_color": COL_SELLTHRU_HEADER,
            "body_color": COL_SELLTHRU_BODY,
        },
        {
            "title": "ACCOUNTING",
            "rows": accounting_report_rows,
            "header_color": COL_ACCOUNTING_HEADER,
            "body_color": COL_ACCOUNTING_BODY,
        },
    ])
    invoice_rows = []
    for section_index, section in enumerate(invoice_sections):
        invoice_rows.append([formatted_date, section["title"], "", ""])
        invoice_rows.extend(
            [formatted_date, *row] for row in section["rows"]
        )
        if section_index < len(invoice_sections) - 1:
            invoice_rows.append(["", "", "", ""])

    footer_start = r
    st_footer_formula = (
        _detail_net_formula("SELLTHRU")
        + _detail_net_formula("SELLTHRUFEE").replace("=", "+")
    )
    if include_legacy_st_sales_fee:
        st_footer_formula += _detail_net_formula("SELLTHRUSALESFEE").replace("=", "+")

    footer_formulas = [
        # NGRS = net(RECHARGE - RECHARGEFEE + pembelian recharge out-cluster)
        (
            _detail_net_formula("RECHARGE")
            + _detail_net_formula("RECHARGEFEE").replace("=", "+")
            + _detail_net_formula(
                PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY
            ).replace("=", "+")
        ),
        # Recharge Out Cluster = net(RECHARGE OUT CLUSTER - RECHARGE OUT CLUSTER FEE)
        (
            _detail_net_formula("RECHARGE OUT CLUSTER")
            + _detail_net_formula("RECHARGE OUT CLUSTER FEE").replace("=", "+")
        ),
        # Reversal - NGRS = net(Reversal NGRS - Reversal NGRS fee)
        (
            _detail_net_formula(REVERSAL_NGRS_CATEGORY)
            + _detail_net_formula(REVERSAL_NGRS_FEE_CATEGORY).replace("=", "+")
        ),
        # Reversal - Pembelian Recharge Out Cluster
        _detail_net_formula(REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY),
        # Reversal - Recharge Out Cluster = net(out-cluster reversal - fee)
        (
            _detail_net_formula(REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY)
            + _detail_net_formula(
                REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY
            ).replace("=", "+")
        ),
        # PPOB  = net(FeeTransaksi)
        _detail_net_formula("FeeTransaksi"),
        # ST = net(SELLTHRU family, with the legacy SLSFEE only before cutoff)
        st_footer_formula,
        # DISBURSEMENT
        _detail_net_formula("DISBURSEMENT"),
        # QRISDUWIT
        _detail_net_formula("QRISDUWIT"),
    ]
    footer_rows = [
        "NGRS",
        "Recharge Out Cluster",
        "Reversal - NGRS",
        "Reversal - Pembelian Recharge Out Cluster",
        "Reversal - Recharge Out Cluster",
        "PPOB",
        "ST",
        "DISBURSEMENT",
        "QRISDUWIT",
        "Total",
    ]
    footer_formulas.append(
        "=" + "+".join(f"D{footer_start + j}" for j in range(len(footer_formulas)))
    )
    for i, f_label in enumerate(footer_rows):
        rows_to_append.append(
            _summary_row("MANDIRI", f_label, footer_formulas[i])
        )
        r += 1
    footer_end = r - 1
    total_row = r - 1
    running_total_row = r
    previous_mandiri_value = (
        f'IFERROR(INDEX(FILTER(D$1:D{running_total_row - 1},'
        f'C$1:C{running_total_row - 1}="MANDIRI",'
        f'D$1:D{running_total_row - 1}<>"MANDIRI"),'
        f'COUNTIFS(C$1:C{running_total_row - 1},"MANDIRI",'
        f'D$1:D{running_total_row - 1},"<>MANDIRI")),'
        f'IFERROR(INDEX(FILTER(E$1:E{running_total_row - 1},'
        f'C$1:C{running_total_row - 1}="MANDIRI",'
        f'D$1:D{running_total_row - 1}="MANDIRI"),'
        f'COUNTIFS(C$1:C{running_total_row - 1},"MANDIRI",'
        f'D$1:D{running_total_row - 1},"MANDIRI")),'
        f'IFERROR(LOOKUP(2,1/('
        f'B$1:B{running_total_row - 1}="MANDIRI")/'
        f'ISNUMBER(C$1:C{running_total_row - 1}),'
        f'C$1:C{running_total_row - 1}),0)))'
    )
    previous_running_total = (
        f'IFERROR(INDEX(FILTER(D$1:D{running_total_row - 1},'
        f'C$1:C{running_total_row - 1}="RUNNING TOTAL",'
        f'D$1:D{running_total_row - 1}<>"RUNNING_TOTAL"),'
        f'COUNTIFS(C$1:C{running_total_row - 1},"RUNNING TOTAL",'
        f'D$1:D{running_total_row - 1},"<>RUNNING_TOTAL")),'
        f'IFERROR(INDEX(FILTER(E$1:E{running_total_row - 1},'
        f'C$1:C{running_total_row - 1}="RUNNING TOTAL",'
        f'D$1:D{running_total_row - 1}="RUNNING_TOTAL"),'
        f'COUNTIFS(C$1:C{running_total_row - 1},"RUNNING TOTAL",'
        f'D$1:D{running_total_row - 1},"RUNNING_TOTAL")),'
        f'IFERROR(INDEX(FILTER(C$1:C{running_total_row - 1},'
        f'B$1:B{running_total_row - 1}="RUNNING TOTAL"),'
        f'COUNTIF(B$1:B{running_total_row - 1},"RUNNING TOTAL")),0)))'
    )
    running_total_formula = (
        f'=IF({previous_mandiri_value}>0,'
        f'D{total_row},'
        f'{previous_running_total}+D{total_row})'
    )
    rows_to_append.append(
        _summary_row("Cash", "RUNNING TOTAL", running_total_formula)
    )
    r += 1
    mandiri_row = r
    mandiri_formula = _next_transfer_mandiri_formula()
    rows_to_append.append(
        _summary_row("Cash", "MANDIRI", mandiri_formula)
    )
    r += 1
    selisih_row = r
    selisih_formula = (
        f'=IF(OR(D{mandiri_row}="",D{mandiri_row}=0),'
        f'"",D{mandiri_row}-D{running_total_row})'
    )
    selisih_status_formula = (
        f'=IF(OR(D{mandiri_row}="",D{mandiri_row}=0),"PENDING TRANSFER",'
        f'IF(D{selisih_row}=0,"SESUAI",'
        f'IF(D{selisih_row}>0,'
        f'"LEBIH BAYAR",'
        f'"KURANG BAYAR")))'
    )
    rows_to_append.append(
        _summary_row("Cash", "SELISIH", selisih_formula, selisih_status_formula)
    )
    reconciliation_end = r
    cash_flow_rows = rows_to_append
    invoice_start = detail_start + INVOICE_DETAIL_START_OFFSET
    qrisduwit_parent_invoice_row = None
    if qrisduwit_report_rows:
        qrisduwit_parent_invoice_row = (
            invoice_start + 1 + cash_report_rows.index(("QRISDUWIT", "", ""))
        )
    invoice_offset = invoice_start - insert_row
    block_row_count = max(len(cash_flow_rows), invoice_offset + len(invoice_rows))
    rows_to_append = []
    for idx in range(block_row_count):
        left = (
            cash_flow_rows[idx]
            if idx < len(cash_flow_rows)
            else [formatted_date, "", "", "", "", ""]
        )
        invoice_idx = idx - invoice_offset
        right = (
            invoice_rows[invoice_idx]
            if 0 <= invoice_idx < len(invoice_rows)
            else ["", "", "", ""]
        )
        rows_to_append.append([*left, "", *right])
    block_end = insert_row + len(rows_to_append) - 1
    invoice_table_ranges = []
    invoice_separator_rows = []
    row_cursor = invoice_start
    for section_index, section in enumerate(invoice_sections):
        header_row = row_cursor
        end_row = header_row + len(section["rows"])
        invoice_table_ranges.append({
            "header_row": header_row,
            "end_row": end_row,
            "header_color": section["header_color"],
            "body_color": section["body_color"],
        })
        row_cursor = end_row + 1
        if section_index < len(invoice_sections) - 1:
            invoice_separator_rows.append(row_cursor)
            row_cursor += 1
    invoice_end = row_cursor - 1
    blank_invoice_blocks = _blank_invoice_block_ranges(
        insert_row,
        rows_to_append,
    )
    blank_invoice_merge_requests = _blank_invoice_block_merge_requests(
        blank_invoice_blocks,
    )
    blank_invoice_border_requests = _blank_invoice_block_border_requests(
        blank_invoice_blocks,
    )
    invoice_header_unmerge_requests = []
    invoice_table_border_requests = []
    for table in invoice_table_ranges:
        header_row = table["header_row"]
        end_row = table["end_row"]
        invoice_header_unmerge_requests.extend(
            _unmerge_range_requests(header_row, header_row, 9, 11)
        )
        invoice_table_border_requests.extend(
            _invoice_table_border_requests(header_row, end_row)
        )

    required_rows = max(block_end, mandiri_row + 2)
    if replacement_start:
        structure_requests = []
        structure_requests.extend(
            _merged_invoice_unmerge_requests(replacement_rows)
        )
        structure_requests.extend(_delete_row_requests(replacement_rows))
        insert_request = _insert_row_request(
            replacement_start,
            len(rows_to_append),
        )
        if insert_request:
            structure_requests.append(insert_request)
        if structure_requests:
            sh.batch_update({"requests": structure_requests})
    else:
        ensure_row_capacity(sh, ws, required_rows, label="summary worksheet")
    ws.update(
        _range(insert_row, block_end, 1, 11),
        uppercase_sheet_rows(rows_to_append),
        value_input_option="USER_ENTERED",
    )

    # Formatting
    data_start = insert_row
    data_end = block_end
    IDR = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}
    format_requests = []

    def _add_format_request(
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
        cell_format: dict,
        fields: str,
    ) -> None:
        request = _format_cell_request(
            start_row,
            end_row,
            start_col,
            end_col,
            cell_format,
            fields,
        )
        if request:
            format_requests.append(request)

    def _format_report_section(
        start_row: int,
        end_row: int,
        first_row_color: dict,
        body_color: dict,
    ) -> None:
        _add_format_request(
            start_row,
            end_row,
            1,
            6,
            {"backgroundColor": body_color},
            "userEnteredFormat.backgroundColor",
        )
        _add_format_request(
            start_row,
            end_row,
            4,
            6,
            {"numberFormat": IDR},
            "userEnteredFormat.numberFormat",
        )
        if end_row > start_row:
            _add_format_request(
                start_row + 1,
                end_row,
                1,
                2,
                {"textFormat": {"foregroundColor": COL_MUTED_TEXT}},
                "userEnteredFormat.textFormat.foregroundColor",
            )
        _add_format_request(
            start_row,
            start_row,
            1,
            2,
            {
                "backgroundColor": first_row_color,
                "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
            },
            "userEnteredFormat(backgroundColor,textFormat)",
        )

    def _format_detail_stripes(start_row: int, end_row: int) -> None:
        for row_number in range(start_row, end_row + 1, 2):
            stripe_start_col = 3 if row_number == start_row else 1
            _add_format_request(
                row_number,
                row_number,
                stripe_start_col,
                6,
                {"backgroundColor": COL_DETAIL_ALT},
                "userEnteredFormat.backgroundColor",
            )

    _add_format_request(
        data_start,
        data_end,
        1,
        11,
        {"backgroundColor": COL_WHITE},
        "userEnteredFormat.backgroundColor",
    )
    _add_format_request(
        data_start,
        data_end,
        1,
        11,
        {
            "textFormat": {
                "bold": True,
                "foregroundColor": COL_DEFAULT_TEXT,
            }
        },
        (
            "userEnteredFormat.textFormat.bold,"
            "userEnteredFormat.textFormat.foregroundColor"
        ),
    )
    _add_format_request(
        data_start,
        data_end,
        4,
        6,
        {"numberFormat": IDR},
        "userEnteredFormat.numberFormat",
    )
    _add_format_request(
        data_start,
        data_end,
        8,
        8,
        {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}},
        "userEnteredFormat.numberFormat",
    )
    _add_format_request(
        data_start,
        data_end,
        10,
        11,
        {"numberFormat": IDR},
        "userEnteredFormat.numberFormat",
    )
    _format_report_section(
        detail_start, detail_end, COL_FOOTER_HEADER, COL_WHITE
    )
    _format_detail_stripes(detail_start, detail_end)
    _format_report_section(
        footer_start, footer_end, COL_FOOTER_HEADER, COL_FOOTER_BODY
    )
    _format_report_section(
        running_total_row, selisih_row, COL_INPUT, COL_STATUS
    )
    _add_format_request(
        running_total_row,
        selisih_row,
        4,
        5,
        {"numberFormat": IDR},
        "userEnteredFormat.numberFormat",
    )
    _add_format_request(
        mandiri_row,
        mandiri_row,
        1,
        6,
        {
            "backgroundColor": COL_INPUT,
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
        },
        "userEnteredFormat(backgroundColor,textFormat)",
    )
    _add_format_request(
        mandiri_row,
        mandiri_row,
        4,
        4,
        {"backgroundColor": COL_INPUT, "numberFormat": IDR},
        "userEnteredFormat(backgroundColor,numberFormat)",
    )
    _add_format_request(
        selisih_row,
        selisih_row,
        1,
        6,
        {
            "backgroundColor": COL_STATUS,
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
        },
        "userEnteredFormat(backgroundColor,textFormat)",
    )
    _add_format_request(
        selisih_row,
        selisih_row,
        4,
        4,
        {"numberFormat": IDR},
        "userEnteredFormat.numberFormat",
    )
    _add_format_request(
        selisih_row,
        selisih_row,
        5,
        5,
        {"wrapStrategy": "WRAP"},
        "userEnteredFormat.wrapStrategy",
    )
    if invoice_rows:
        for table in invoice_table_ranges:
            header_row = table["header_row"]
            end_row = table["end_row"]
            _add_format_request(
                header_row,
                header_row,
                8,
                11,
                {
                    "backgroundColor": table["header_color"],
                    "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
                    "horizontalAlignment": "CENTER",
                },
                "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            )
            if end_row > header_row:
                _add_format_request(
                    header_row + 1,
                    end_row,
                    8,
                    11,
                    {"backgroundColor": table["body_color"]},
                    "userEnteredFormat.backgroundColor",
                )
        for separator_row in invoice_separator_rows:
            _add_format_request(
                separator_row,
                separator_row,
                8,
                11,
                {"backgroundColor": COL_WHITE},
                "userEnteredFormat.backgroundColor",
            )
        _add_format_request(
            invoice_start,
            invoice_end,
            9,
            9,
            {
                "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
                "horizontalAlignment": "LEFT",
                "wrapStrategy": "CLIP",
            },
            "userEnteredFormat(textFormat,horizontalAlignment,wrapStrategy)",
        )
        _add_format_request(
            invoice_start,
            invoice_end,
            8,
            8,
            {"horizontalAlignment": "CENTER", "wrapStrategy": "CLIP"},
            "userEnteredFormat(horizontalAlignment,wrapStrategy)",
        )
        _add_format_request(
            invoice_start,
            invoice_end,
            10,
            11,
            {"horizontalAlignment": "RIGHT"},
            "userEnteredFormat.horizontalAlignment",
        )
        for table in invoice_table_ranges:
            _add_format_request(
                table["header_row"],
                table["header_row"],
                9,
                9,
                {"horizontalAlignment": "CENTER"},
                "userEnteredFormat.horizontalAlignment",
            )
        if qrisduwit_parent_invoice_row:
            _add_format_request(
                qrisduwit_parent_invoice_row,
                qrisduwit_parent_invoice_row,
                8,
                11,
                {
                    "backgroundColor": COL_CASH_HEADER,
                    "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
                    "horizontalAlignment": "CENTER",
                },
                "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            )
    _add_format_request(
        1,
        1,
        1,
        6,
        {
            "backgroundColor": COL_HEADER,
            "horizontalAlignment": "CENTER",
            "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
        },
        "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
    )
    _add_format_request(
        1,
        1,
        7,
        7,
        {"backgroundColor": COL_WHITE},
        "userEnteredFormat.backgroundColor",
    )
    _add_format_request(
        1,
        1,
        8,
        11,
        {
            "backgroundColor": COL_HEADER,
            "horizontalAlignment": "CENTER",
            "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
        },
        "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
    )
    old_dashboard_range = {
        "sheetId": ws.id,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 11,
        "endColumnIndex": 15,
    }
    summary_widths = {
        0: 110,
        1: 130,
        2: 410,
        3: 140,
        4: 190,
        5: 140,
        6: 56,
        7: 110,
        8: 410,
        9: 140,
        10: 140,
    }
    existing_mandiri_cells = _existing_mandiri_cells(existing_values)
    existing_detail_start_rows = _daily_detail_start_rows(existing_values)
    if replacement_start:
        inserted_row_count = len(rows_to_append)
        existing_mandiri_cells = [
            (row + inserted_row_count if row >= replacement_start else row, col)
            for row, col in existing_mandiri_cells
        ]
        existing_detail_start_rows = [
            row + inserted_row_count if row >= replacement_start else row
            for row in existing_detail_start_rows
        ]
    final_sheet_end = max(block_end, len(existing_values) + len(rows_to_append))
    detail_start_rows = [*existing_detail_start_rows, detail_start]
    protection_requests = _summary_protection_requests(
        final_sheet_end,
        [*existing_mandiri_cells, (mandiri_row, 4)],
    )
    sh.batch_update({"requests": [
        *protection_requests,
        {
            "clearBasicFilter": {
                "sheetId": ws.id,
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        _header_value_request(summary_headers),
        *invoice_header_unmerge_requests,
        *_unmerge_range_requests(2, 2, 8, 11),
        *blank_invoice_merge_requests,
        *format_requests,
        *_clear_cell_grid_border_requests(data_start, data_end, 1, 11),
        *_cell_grid_border_requests(1, 1, 1, 11),
        *_cell_grid_border_requests(2, 2, 1, 11),
        *_cell_grid_border_requests(data_start, data_end, 1, 11),
        *_horizontal_border_requests(ws, 1, 1, 1, 11),
        *_horizontal_border_requests(ws, data_start, data_end, 1, 11),
        *blank_invoice_border_requests,
        *[
            request
            for row in [
                detail_start,
                footer_start,
            ]
            for request in _horizontal_border_requests(
                ws,
                row,
                row,
                1,
                6,
                top=True,
                bottom=False,
                style="SOLID_MEDIUM",
                color=COL_HEADER,
            )
        ],
        *invoice_table_border_requests,
        *_summary_block_top_border_requests(detail_start_rows, final_sheet_end),
        *_horizontal_border_requests(
            ws,
            total_row,
            total_row,
            4,
            4,
            top=True,
            bottom=False,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        *_horizontal_border_requests(
            ws,
            selisih_row,
            selisih_row,
            4,
            4,
            top=True,
            bottom=False,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        {
            "updateCells": {
                "range": old_dashboard_range,
                "rows": [{"values": [{} for _ in range(4)]}],
                "fields": "userEnteredValue",
            }
        },
        {
            "repeatCell": {
                "range": old_dashboard_range,
                "cell": {"userEnteredFormat": {}},
                "fields": "userEnteredFormat",
            }
        },
        *[
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            }
            for i, px in summary_widths.items()
        ],
    ]})
    return insert_row, block_end


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4  coordinator  (used by the upload task)
# ─────────────────────────────────────────────────────────────────────────────

def process_daily_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    summary_df: pd.DataFrame,
    starting_balance_date: str,
    default_starting_balance: int,
    gspread_client,
    qrisduwit_df: pd.DataFrame | None = None,
    report_date: str | None = None,
) -> tuple[int | None, int | None]:
    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)

    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=1000, cols=50)

    if not ws.acell("A1").value:
        setup_initial_headers_and_saldo(
            gspread_client, target_spreadsheet, target_worksheet,
            starting_date_str=starting_balance_date,
            starting_balance=default_starting_balance,
        )

    ensure_linkaja_fee_reference_sheet(gspread_client, sh)

    return append_daily_to_gsheet(
        gspread_client,
        target_spreadsheet,
        target_worksheet,
        summary_df,
        qrisduwit_df=qrisduwit_df,
        report_date=report_date,
    )
