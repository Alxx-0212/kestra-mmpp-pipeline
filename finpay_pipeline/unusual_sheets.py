"""Google Sheets writer for unusual transaction rows."""
import gspread
import pandas as pd

from .sheets_common import (
    _add_protected_sheet_request,
    _delete_all_protected_range_requests,
    ensure_row_capacity,
)

def append_unusual_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
) -> bool:
    """
    Appends unusual transaction rows to a dedicated sheet using a readable,
    fixed report layout instead of dumping raw DataFrame columns.
    - Writes formatted column headers on the first run.
    - Duplicate guard: skips if the report date already exists.
    Returns True on success, False if skipped (duplicate).
    """
    if unusual_df.empty:
        print('No unusual transactions — nothing to write.')
        return True

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=None):
        ec = ec or len(HEADERS)
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    def _clean(value):
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return ''
        return value

    def _number(value):
        value = _clean(value)
        if value == '':
            return ''
        return int(value)

    def _datetime_display(value):
        value = _clean(value)
        if value == '':
            return ''
        return pd.to_datetime(value).strftime('%d/%m/%Y %H:%M:%S')

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

    sh = gspread_client.open(target_spreadsheet)
    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=5000, cols=20)

    def _protect_sheet() -> None:
        protection_request = _add_protected_sheet_request(
            gspread_client,
            ws,
            f"FinPay protected unusual sheet",
        )
        requests = [
            *_delete_all_protected_range_requests(sh, ws),
            protection_request,
        ]
        sh.batch_update({"requests": requests})

    report_date = pd.to_datetime(unusual_df['Transaction Date']).dt.strftime('%d/%m/%Y').max()
    existing = ws.get_all_values()
    meaningful_existing = [
        row for row in existing
        if any(str(cell).strip() for cell in row)
    ]

    if meaningful_existing:
        headers = meaningful_existing[0]
        if 'REPORT DATE' in headers:
            report_col = headers.index('REPORT DATE')
            existing_dates = [row[report_col] for row in meaningful_existing[1:] if len(row) > report_col]
            if report_date in existing_dates:
                _protect_sheet()
                print(f'⚠️  Unusual transactions for {report_date} already exist — skipping.')
                return False
        elif 'Transaction Date' in headers:
            td_col = headers.index('Transaction Date')
            existing_dates = [row[td_col] for row in meaningful_existing[1:] if len(row) > td_col]
            if any(report_date in d for d in existing_dates):
                _protect_sheet()
                print(f'⚠️  Unusual transactions for {report_date} already exist — skipping.')
                return False

    if not meaningful_existing:
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
            report_date,
            _number(row.get('No')),
            _datetime_display(row.get('Transaction Date')),
            str(_clean(row.get('Transaction ID'))),
            str(_clean(row.get('base_id'))),
            str(_clean(row.get('Transaction'))),
            _number(row.get('Kredit')),
            _number(row.get('Debet')),
            _number(row.get('Saldo Awal')),
            _number(row.get('Saldo Akhir')),
            str(_clean(row.get('Nomor RS'))),
            str(_clean(row.get('Remarks'))),
            str(_clean(row.get('unusual_reason'))),
        ])

    write_end = insert_row + len(rows_to_append) - 1
    ensure_row_capacity(sh, ws, write_end, buffer_rows=500, label=target_worksheet)
    ws.update(_range(insert_row, write_end), rows_to_append, value_input_option='USER_ENTERED')

    header_row = data_start - 1 if needs_header else 1
    data_end = data_start + len(unusual_df) - 1

    widths = {
        0: 110, 1: 70, 2: 165, 3: 220, 4: 220, 5: 320, 6: 120,
        7: 120, 8: 130, 9: 130, 10: 130, 11: 420, 12: 320,
    }
    protection_request = _add_protected_sheet_request(
        gspread_client,
        ws,
        f"FinPay protected unusual sheet {report_date}",
    )
    sh.batch_update({"requests": [
        *_delete_all_protected_range_requests(sh, ws),
        *([protection_request] if protection_request else []),
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
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
        "backgroundColor": COL_WHITE,
        "verticalAlignment": "TOP",
    })
    ws.format(f"A{data_start}:A{data_end}", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}})
    ws.format(f"C{data_start}:C{data_end}", {"numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm:ss"}})
    ws.format(f"G{data_start}:J{data_end}", {"numberFormat": IDR})
    ws.format(f"L{data_start}:M{data_end}", {"wrapStrategy": "WRAP"})
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })

    print(f'✓ Written {len(unusual_df)} unusual rows for {report_date}')
    return True


def process_unusual_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
    gspread_client,
) -> bool:
    """
    Coordinator for the unusual-transactions upload step.
    Mirrors the interface of process_daily_upload.
    """
    return append_unusual_to_gsheet(
        gspread_client, target_spreadsheet, target_worksheet, unusual_df
    )
