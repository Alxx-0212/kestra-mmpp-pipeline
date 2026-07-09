"""Google Sheets writer for unusual transaction rows."""
import gspread
import pandas as pd

from .sheets_common import (
    _add_protected_sheet_request,
    _clear_background_color_request,
    _delete_all_banded_range_requests,
    _delete_all_protected_range_requests,
    _delete_sheet_rows,
    _horizontal_border_requests,
    _insert_blank_sheet_rows,
    _matching_report_date_rows,
    _row_banding_request,
    clean_sheet_value,
    datetime_sheet_display,
    ensure_row_capacity,
    number_sheet_value,
    open_or_create_finpay_spreadsheet,
    report_date_display,
    sheet_range,
    uppercase_sheet_rows,
)

def append_unusual_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
    report_date: str | None = None,
) -> bool:
    """
    Appends unusual transaction rows to a dedicated sheet using a readable,
    fixed report layout instead of dumping raw DataFrame columns.
    - Writes formatted column headers on the first run.
    - Replaces existing rows for the same report date on reruns.
    Returns True on success/no rows.
    """
    def _range(sr, er, sc=1, ec=None):
        return sheet_range(sr, er, sc, ec or len(HEADERS))

    HEADERS = [
        'REPORT DATE',
        'NO',
        'TRANSACTION DATE',
        'TRANSACTION ID',
        'BASE ID',
        'TRANSACTION',
        'KREDIT',
        'DEBET',
        'SALDO AWAL',
        'SALDO AKHIR',
        'NOMOR RS',
        'REMARKS',
        'UNUSUAL REASON',
    ]

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    COL        = {"red": 0.741, "green": 0.843, "blue": 0.933}
    IDR        = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)
    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        if unusual_df.empty:
            print('No unusual transactions — nothing to write.')
            return True
        ws = sh.add_worksheet(title=target_worksheet, rows=5000, cols=20)

    report_date_text = report_date_display(unusual_df, report_date)
    if unusual_df.empty and not report_date_text:
        print('No unusual transactions — nothing to write.')
        return True

    existing = ws.get_all_values()
    meaningful_existing = [
        row for row in existing
        if any(str(cell).strip() for cell in row)
    ]

    if meaningful_existing:
        headers = meaningful_existing[0]
        matching_rows = _matching_report_date_rows(
            existing,
            headers,
            report_date_text,
            fallback_header="Transaction Date",
        )
        if matching_rows:
            replacement_start = min(matching_rows)
            _delete_sheet_rows(sh, ws, matching_rows)
            print(
                f'↻ Replacing {len(matching_rows)} existing unusual rows '
                f'for {report_date_text}.'
            )
            existing = ws.get_all_values()
            meaningful_existing = [
                row for row in existing
                if any(str(cell).strip() for cell in row)
            ]
        else:
            replacement_start = None
    else:
        replacement_start = None

    if unusual_df.empty:
        print(f'No unusual transactions for {report_date_text} — stale rows removed if present.')
        return True

    if replacement_start:
        insert_row = replacement_start
    elif not meaningful_existing:
        insert_row = 1
    else:
        insert_row = len(existing) + 1

    needs_header = not meaningful_existing or meaningful_existing[0] != HEADERS
    rows_to_append = []

    if needs_header:
        rows_to_append.append(HEADERS)
        data_start = insert_row + 1
    else:
        data_start = insert_row

    for _, row in unusual_df.iterrows():
        rows_to_append.append([
            report_date_text,
            number_sheet_value(row.get('No')),
            datetime_sheet_display(row.get('Transaction Date')),
            str(clean_sheet_value(row.get('Transaction ID'))),
            str(clean_sheet_value(row.get('base_id'))),
            str(clean_sheet_value(row.get('Transaction'))),
            number_sheet_value(row.get('Kredit')),
            number_sheet_value(row.get('Debet')),
            number_sheet_value(row.get('Saldo Awal')),
            number_sheet_value(row.get('Saldo Akhir')),
            str(clean_sheet_value(row.get('Nomor RS'))),
            str(clean_sheet_value(row.get('Remarks'))),
            str(clean_sheet_value(row.get('unusual_reason'))),
        ])

    write_end = insert_row + len(rows_to_append) - 1
    if replacement_start:
        _insert_blank_sheet_rows(sh, ws, replacement_start, len(rows_to_append))
    else:
        ensure_row_capacity(sh, ws, write_end, buffer_rows=500, label=target_worksheet)
    ws.update(
        _range(insert_row, write_end),
        uppercase_sheet_rows(rows_to_append),
        value_input_option='USER_ENTERED',
    )

    header_row = data_start - 1 if needs_header else 1
    data_end = data_start + len(unusual_df) - 1
    table_end = max(write_end, len(existing) + len(rows_to_append))

    widths = {
        0: 110, 1: 70, 2: 165, 3: 300, 4: 220, 5: 410, 6: 120,
        7: 120, 8: 130, 9: 130, 10: 130, 11: 420, 12: 480,
    }
    protection_request = _add_protected_sheet_request(
        gspread_client,
        ws,
        f"FinPay protected unusual sheet {report_date_text}",
    )
    banding_request = _row_banding_request(
        ws,
        header_row,
        table_end,
        1,
        len(HEADERS),
        COL_HEADER,
    )
    clear_data_background_request = _clear_background_color_request(
        ws,
        data_start,
        table_end,
        1,
        len(HEADERS),
    )
    sh.batch_update({"requests": [
        *_delete_all_protected_range_requests(sh, ws),
        *_delete_all_banded_range_requests(sh, ws),
        *(
            [clear_data_background_request]
            if clear_data_background_request
            else []
        ),
        *([protection_request] if protection_request else []),
        *([banding_request] if banding_request else []),
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
        *_horizontal_border_requests(ws, header_row, header_row, 1, len(HEADERS)),
        *_horizontal_border_requests(ws, data_start, data_end, 1, len(HEADERS)),
        *_horizontal_border_requests(
            ws,
            data_start,
            data_start,
            1,
            len(HEADERS),
            top=True,
            bottom=False,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
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

    ws.format(_range(header_row, header_row), {
        "backgroundColor": COL_HEADER,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
    })
    ws.format(_range(data_start, data_end), {
        "verticalAlignment": "TOP",
    })
    ws.format(f"A{data_start}:A{data_end}", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}})
    ws.format(f"C{data_start}:C{data_end}", {"numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm:ss"}})
    ws.format(f"G{data_start}:J{data_end}", {"numberFormat": IDR})
    ws.format(f"L{data_start}:M{data_end}", {"wrapStrategy": "WRAP"})
    print(f'✓ Written {len(unusual_df)} unusual rows for {report_date_text}')
    return True


def process_unusual_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
    gspread_client,
    report_date: str | None = None,
) -> bool:
    """
    Coordinator for the unusual-transactions upload step.
    Mirrors the interface of process_daily_upload.
    """
    return append_unusual_to_gsheet(
        gspread_client,
        target_spreadsheet,
        target_worksheet,
        unusual_df,
        report_date=report_date,
    )
