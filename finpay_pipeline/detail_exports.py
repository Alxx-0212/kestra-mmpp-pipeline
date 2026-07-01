"""QRISDUWIT and Reversal detail export helpers and sheet writer."""
import re

import gspread
import pandas as pd

from .classification import (
    REVERSAL_CATEGORY_TO_MAIN,
    REVERSAL_TRANSACTION,
    _is_reversal_transaction_label,
    relabel_reversal_transactions,
)
from .sheets_common import (
    _add_protected_sheet_request,
    _delete_all_protected_range_requests,
    ensure_row_capacity,
)

def extract_disbursement_date_from_remarks(remarks) -> str:
    """
    Extracts the QRIS Duwit disbursement date from remarks text.
    Expected phrase example:
      "... pembayaran QRIS pada tanggal 04-06-2026 sejumlah ..."
    Returns DD/MM/YYYY for Google Sheets readability, or an empty string.
    """
    if remarks is None or (not isinstance(remarks, str) and pd.isna(remarks)):
        return ""

    match = re.search(
        r"\btanggal\s+(\d{2}-\d{2}-\d{4})\b",
        str(remarks),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""

    parsed = pd.to_datetime(match.group(1), format="%d-%m-%Y", errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%d/%m/%Y")


def prepare_transaction_detail_export(
    df: pd.DataFrame,
    transaction: str,
    include_disbursement_date: bool = False,
) -> pd.DataFrame:
    """
    Filters detail rows by Transaction using case-insensitive exact matching.
    Optionally adds Disbursement Date extracted from Remarks for QRISDUWIT.
    """
    if "Transaction" not in df.columns:
        raise KeyError("Transaction column is required for detail export.")

    target = str(transaction).strip().upper()
    normalized = df["Transaction"].fillna("").astype(str).str.strip().str.upper()
    result = df.loc[normalized == target].copy()

    if include_disbursement_date:
        result["Disbursement Date"] = result["Remarks"].apply(
            extract_disbursement_date_from_remarks
        )

    sort_cols = [col for col in ["Transaction Date", "No"] if col in result.columns]
    if sort_cols:
        result = result.sort_values(sort_cols)

    return result.reset_index(drop=True)


def prepare_reversal_detail_export(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filters REVERSAL rows and relabels rows into reversal detail categories
    using the same remark rules as the summary transform.
    """
    if "Transaction" not in df.columns:
        raise KeyError("Transaction column is required for detail export.")

    prepared = relabel_reversal_transactions(df)
    reversal_mask = prepared['Transaction'].apply(_is_reversal_transaction_label)
    result = prepared.loc[reversal_mask].copy()

    if result.empty:
        print('No REVERSAL rows found for detail export.')
        return result

    categorized_counts = {
        str(value): int(count)
        for value, count in result['Transaction'].value_counts().items()
        if value in REVERSAL_CATEGORY_TO_MAIN
    }
    unclassified_rows = int(
        result['Transaction']
        .fillna('')
        .astype(str)
        .str.strip()
        .str.upper()
        .eq(REVERSAL_TRANSACTION)
        .sum()
    )

    sort_cols = [col for col in ["Transaction Date", "No"] if col in result.columns]
    if sort_cols:
        result = result.sort_values(sort_cols)

    print(f'Reversal detail rows categorized: {categorized_counts}')
    print(f'Reversal detail rows left as Reversal: {unclassified_rows}')
    return result.reset_index(drop=True)


def append_transaction_detail_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    detail_df: pd.DataFrame,
    include_disbursement_date: bool = False,
) -> bool:
    """
    Appends filtered transaction detail rows to a dedicated worksheet.
    Returns True on success/no rows, False if skipped by duplicate-date guard.
    """
    if detail_df.empty:
        print(f"No rows for {target_worksheet} — nothing to write.")
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
            return ""
        return value

    def _number(value):
        value = _clean(value)
        if value == "":
            return ""
        return int(value)

    def _sheet_text(value):
        value = _clean(value)
        if value == "":
            return ""
        return f"'{str(value)}"

    def _datetime_display(value):
        value = _clean(value)
        if value == "":
            return ""
        return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M:%S")

    def _date_display(value):
        value = _clean(value)
        if value == "":
            return ""
        return pd.to_datetime(value, dayfirst=True).strftime("%d/%m/%Y")

    HEADERS = ["REPORT DATE"]
    if include_disbursement_date:
        HEADERS.append("DISBURSEMENT DATE")
    HEADERS.extend([
        "NO",
        "TRANSACTION DATE",
        "TRANSACTION ID",
        "TRANSACTION TYPE",
        "TRANSACTION",
        "KREDIT",
        "DEBET",
        "SALDO AWAL",
        "SALDO AKHIR",
        "NOMOR RS",
        "REMARKS",
    ])

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    IDR        = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    sh = gspread_client.open(target_spreadsheet)
    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=5000, cols=max(20, len(HEADERS)))

    def _protect_sheet() -> None:
        protection_request = _add_protected_sheet_request(
            gspread_client,
            ws,
            f"FinPay protected {target_worksheet} sheet",
        )
        requests = [
            *_delete_all_protected_range_requests(sh, ws),
            protection_request,
        ]
        sh.batch_update({"requests": requests})

    report_date = pd.to_datetime(detail_df["Transaction Date"]).dt.strftime("%d/%m/%Y").max()
    existing = ws.get_all_values()
    meaningful_existing = [
        row for row in existing
        if any(str(cell).strip() for cell in row)
    ]

    if meaningful_existing:
        headers = meaningful_existing[0]
        if "REPORT DATE" in headers:
            report_col = headers.index("REPORT DATE")
            existing_dates = [
                row[report_col] for row in meaningful_existing[1:]
                if len(row) > report_col
            ]
            if report_date in existing_dates:
                _protect_sheet()
                print(f"⚠️  {target_worksheet} rows for {report_date} already exist — skipping.")
                return False

    insert_row = 1 if not meaningful_existing else len(existing) + 1
    needs_header = not meaningful_existing or meaningful_existing[0] != HEADERS
    rows_to_append = []

    if needs_header:
        rows_to_append.append(HEADERS)
        data_start = insert_row + 1
    else:
        data_start = insert_row

    for _, row in detail_df.iterrows():
        output_row = [report_date]
        if include_disbursement_date:
            output_row.append(_date_display(row.get("Disbursement Date")))
        output_row.extend([
            _number(row.get("No")),
            _datetime_display(row.get("Transaction Date")),
            str(_clean(row.get("Transaction ID"))),
            str(_clean(row.get("Transaction Type"))),
            str(_clean(row.get("Transaction"))),
            _number(row.get("Kredit")),
            _number(row.get("Debet")),
            _number(row.get("Saldo Awal")),
            _number(row.get("Saldo Akhir")),
            _sheet_text(row.get("Nomor RS")),
            str(_clean(row.get("Remarks"))),
        ])
        rows_to_append.append(output_row)

    write_end = insert_row + len(rows_to_append) - 1
    ensure_row_capacity(sh, ws, write_end, buffer_rows=500, label=target_worksheet)
    ws.update(_range(insert_row, write_end), rows_to_append, value_input_option="USER_ENTERED")

    header_row = data_start - 1 if needs_header else 1
    data_end = data_start + len(detail_df) - 1

    widths_by_header = {
        "REPORT DATE": 110,
        "DISBURSEMENT DATE": 145,
        "NO": 70,
        "TRANSACTION DATE": 165,
        "TRANSACTION ID": 220,
        "TRANSACTION TYPE": 150,
        "TRANSACTION": 320,
        "KREDIT": 120,
        "DEBET": 120,
        "SALDO AWAL": 130,
        "SALDO AKHIR": 130,
        "NOMOR RS": 130,
        "REMARKS": 480,
    }
    protection_request = _add_protected_sheet_request(
        gspread_client,
        ws,
        f"FinPay protected {target_worksheet} sheet {report_date}",
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
                    "properties": {"pixelSize": widths_by_header.get(header, 120)},
                    "fields": "pixelSize",
                }
            }
            for i, header in enumerate(HEADERS)
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

    for header in ["REPORT DATE", "DISBURSEMENT DATE"]:
        if header in HEADERS:
            col = HEADERS.index(header) + 1
            ws.format(f"{_a1(data_start, col)}:{_a1(data_end, col)}", {
                "numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}
            })

    date_col = HEADERS.index("TRANSACTION DATE") + 1
    ws.format(f"{_a1(data_start, date_col)}:{_a1(data_end, date_col)}", {
        "numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm:ss"}
    })

    for header in ["KREDIT", "DEBET", "SALDO AWAL", "SALDO AKHIR"]:
        col = HEADERS.index(header) + 1
        ws.format(f"{_a1(data_start, col)}:{_a1(data_end, col)}", {"numberFormat": IDR})

    nomor_rs_col = HEADERS.index("NOMOR RS") + 1
    ws.format(f"{_a1(data_start, nomor_rs_col)}:{_a1(data_end, nomor_rs_col)}", {
        "numberFormat": {"type": "TEXT"}
    })

    remarks_col = HEADERS.index("REMARKS") + 1
    ws.format(f"{_a1(data_start, remarks_col)}:{_a1(data_end, remarks_col)}", {
        "wrapStrategy": "WRAP"
    })
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })

    print(f"✓ Written {len(detail_df)} rows to {target_worksheet} for {report_date}")
    return True


def process_transaction_detail_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    detail_df: pd.DataFrame,
    gspread_client,
    include_disbursement_date: bool = False,
) -> bool:
    """
    Coordinator for QRISDUWIT and REVERSAL detail-row upload steps.
    """
    return append_transaction_detail_to_gsheet(
        gspread_client,
        target_spreadsheet,
        target_worksheet,
        detail_df,
        include_disbursement_date=include_disbursement_date,
    )
