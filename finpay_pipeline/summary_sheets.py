"""Google Sheets writer for the daily summary worksheet."""
from datetime import datetime

import gspread
import pandas as pd

from .classification import (
    REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY,
)
from .sheets_common import (
    _add_protected_range_request,
    _add_protected_sheet_request,
    _delete_all_protected_range_requests,
    _mandiri_editor_emails,
    open_or_create_finpay_spreadsheet,
)

SUMMARY_ROW_BUFFER = 200


def _ensure_row_capacity(
    sh,
    ws,
    required_rows: int,
    buffer_rows: int = SUMMARY_ROW_BUFFER,
) -> None:
    """Expand worksheet rows before writing formulas that reference future rows."""
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
        "Expanded summary worksheet row capacity: "
        f"{current_rows} -> {target_rows} rows"
    )


def setup_initial_headers_and_saldo(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    starting_date_str: str,
    starting_balance: int,
) -> None:
    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)
    ws = sh.worksheet(target_worksheet)

    # Column widths
    widths = {0: 100, 1: 320, 2: 140, 3: 140, 4: 140}
    sh.batch_update({"requests": [
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

    date_display = pd.to_datetime(starting_date_str).strftime("%d/%m/%Y")
    ws.update("A1:E2", [
        ["TANGGAL", "KETERANGAN", "DEBET", "KREDIT", "SALDO"],
        [date_display, f"SALDO {date_display}", "", "", starting_balance],
    ], value_input_option="USER_ENTERED")

    ws.format("A1:E1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"})
    ws.format("E2", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}})
    protection_request = _add_protected_sheet_request(
        gspread_client,
        ws,
        "FinPay protected opening balance",
    )
    requests = [
        *_delete_all_protected_range_requests(sh, ws),
        *([protection_request] if protection_request else []),
    ]
    if requests:
        sh.batch_update({"requests": requests})


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4b  append daily block
# ─────────────────────────────────────────────────────────────────────────────

def append_daily_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    summary_df: pd.DataFrame,
) -> tuple[int | None, int | None]:
    """
    Returns (insert_row, footer_end) on success, (None, None) if duplicate.
    """
    target_date_str = summary_df["Transaction_Date"].max()
    formatted_date  = datetime.strptime(target_date_str, "%Y-%m-%d").strftime("%d/%m/%Y")

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=5):
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    COL_CASH_HEADER = {"red": 0.820, "green": 0.910, "blue": 0.800}
    COL_CASH_BODY = {"red": 0.925, "green": 0.973, "blue": 0.910}
    COL_ACCOUNTING_HEADER = {"red": 0.980, "green": 0.900, "blue": 0.700}
    COL_ACCOUNTING_BODY = {"red": 1.000, "green": 0.965, "blue": 0.840}
    COL_STATUS = {"red": 0.965, "green": 0.930, "blue": 0.990}
    COL_FOOTER_HEADER = {"red": 0.800, "green": 0.880, "blue": 0.950}
    COL_FOOTER_BODY = {"red": 0.900, "green": 0.940, "blue": 0.980}
    COL_INPUT = COL_STATUS

    sh = open_or_create_finpay_spreadsheet(gspread_client, target_spreadsheet)
    ws = sh.worksheet(target_worksheet)
    existing_values = ws.get_all_values()

    def _existing_mandiri_rows(values: list[list[str]]) -> list[int]:
        return [
            idx
            for idx, row in enumerate(values, start=1)
            if len(row) > 1 and str(row[1]).strip().upper() == "MANDIRI"
        ]

    def _summary_protection_requests(
        end_row: int,
        mandiri_rows: list[int],
    ) -> list[dict]:
        unique_mandiri_rows = [
            row for row in sorted(set(mandiri_rows))
            if row <= end_row
        ]
        protection_request = _add_protected_sheet_request(
            gspread_client,
            ws,
            "FinPay protected summary sheet",
            unprotected_ranges=[
                (row, row, 3, 3)
                for row in unique_mandiri_rows
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
                        3,
                        3,
                        f"FinPay protected MANDIRI input row {row}",
                        extra_editor_emails=mandiri_editor_emails,
                    )
                    for row in unique_mandiri_rows
                )
                if request
            ]
        return [
            *_delete_all_protected_range_requests(sh, ws),
            *([protection_request] if protection_request else []),
            *mandiri_protection_requests,
        ]

    # Duplicate guard — col A stores dates as DD/MM/YYYY, so compare using formatted_date
    if any(row and str(row[0]).strip() == formatted_date for row in existing_values):
        protection_requests = _summary_protection_requests(
            len(existing_values),
            _existing_mandiri_rows(existing_values),
        )
        if protection_requests:
            sh.batch_update({"requests": protection_requests})
        print(f"⚠️  {formatted_date} already exists in sheet — skipping.")
        return None, None

    # Build value map
    val_map = {
        str(row["Transaction"]).strip(): {
            "debet":  float(row.get("Sum_of_Debet",  0) or 0),
            "kredit": float(row.get("Sum_of_Kredit", 0) or 0),
        }
        for _, row in summary_df.iterrows()
    }

    insert_row = len(existing_values) + 1
    r = insert_row
    rows_to_append = []

    KETERANGAN = [
        ("TRANSFER MASUK DARI FINPAY", "CASHOUT APOLLO"),
        ("QRISDUWIT",                  "QRISDUWIT"),
        ("DISBURSEMENT",               "DISBURSEMENT"),
        ("PPOB",                       "FeeTransaksi"),
        ("NGRS",                       "RECHARGE"),
        ("BIAYA FEE NGRS",             "RECHARGEFEE"),
        ("RECHARGE OUT CLUSTER",       "RECHARGE OUT CLUSTER"),
        ("RECHARGE OUT CLUSTER FEE",   "RECHARGE OUT CLUSTER FEE"),
        ("REVERSAL NGRS",              REVERSAL_NGRS_CATEGORY),
        ("REVERSAL NGRS FEE",          REVERSAL_NGRS_FEE_CATEGORY),
        ("REVERSAL RECHARGE OUT CLUSTER", REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY),
        ("REVERSAL RECHARGE OUT CLUSTER FEE", REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY),
        ("ST",                         "SELLTHRU"),
        ("BIAYA FEE ST",               "SELLTHRUFEE"),
        ("BIAYA FEE BAR A. ST",        "SELLTHRUSALESFEE"),
    ]

    first = True
    for label, key in KETERANGAN:
        d = val_map.get(key, {})
        debet  = float(d.get("debet",  0))
        kredit = float(d.get("kredit", 0))
        if first:
            prev   = f"E$1:E{r - 1}"
            e_fmla = f"=IFERROR(INDEX({prev},MATCH(9.99E+307,{prev})),0)+C{r}-D{r}"
            rows_to_append.append([formatted_date, label, kredit, debet, e_fmla])
            first = False
        else:
            rows_to_append.append(["", label, kredit, debet, f"=E{r - 1}+C{r}-D{r}"])
        r += 1

    # Row offsets from insert_row (matches KETERANGAN order):
    #  +1 QRISDUWIT  +2 DISBURSEMENT  +3 FeeTransaksi
    #  +4 RECHARGE   +5 RECHARGEFEE   +6 RECHARGE OUT CLUSTER
    #  +7 RECHARGE OUT CLUSTER FEE    +8 Reversal NGRS
    #  +9 Reversal NGRS FEE  +10 Reversal Recharge Out Cluster
    #  +11 Reversal Recharge Out Cluster FEE
    #  +12 SELLTHRU  +13 SELLTHRUFEE  +14 SELLTHRUSALESFEE
    ir = insert_row

    def _split_net_formula(net_formula: str) -> tuple[str, str]:
        expression = net_formula[1:] if net_formula.startswith("=") else net_formula
        return f"=MAX(({expression}),0)", f"=MAX(-({expression}),0)"

    cash_report_rows = [
        ("NGRS", *_split_net_formula(f"=C{ir+4}-D{ir+4}")),
        ("Recharge Fee", *_split_net_formula(f"=C{ir+5}-D{ir+5}")),
        ("Reversal - NGRS", *_split_net_formula(f"=C{ir+8}-D{ir+8}")),
        ("Reversal - NGRS FEE", *_split_net_formula(f"=C{ir+9}-D{ir+9}")),
        ("QRISDUWIT", *_split_net_formula(f"=C{ir+1}-D{ir+1}")),
    ]

    accounting_report_rows = [
        ("PPOB", *_split_net_formula(f"=C{ir+3}-D{ir+3}")),
        ("DISBURSEMENT", *_split_net_formula(f"=C{ir+2}-D{ir+2}")),
        ("Recharge Out Cluster", *_split_net_formula(f"=C{ir+6}-D{ir+6}")),
        ("Recharge Out Cluster FEE", *_split_net_formula(f"=C{ir+7}-D{ir+7}")),
        (
            "Reversal - Recharge Out Cluster",
            *_split_net_formula(f"=C{ir+10}-D{ir+10}"),
        ),
        (
            "Reversal - Recharge Out Cluster FEE",
            *_split_net_formula(f"=C{ir+11}-D{ir+11}"),
        ),
        ("ST", *_split_net_formula(f"=C{ir+12}-D{ir+12}")),
        ("BIAYA FEE ST", *_split_net_formula(f"=C{ir+13}-D{ir+13}")),
        ("BIAYA FEE BAR A. ST", *_split_net_formula(f"=C{ir+14}-D{ir+14}")),
    ]

    def _append_summary_section(
        title: str,
        report_rows: list[tuple[str, str, str]],
    ) -> tuple[int, int, int]:
        nonlocal r
        header_row = r
        rows_to_append.append([formatted_date, title, "", "", ""])
        r += 1
        body_start = r
        for label, debet_formula, kredit_formula in report_rows:
            rows_to_append.append(["", label, debet_formula, kredit_formula, ""])
            r += 1
        body_end = r - 1
        return header_row, body_start, body_end

    cash_header, cash_start, cash_end = _append_summary_section(
        "CASH IN REPORT",
        cash_report_rows,
    )
    accounting_header, accounting_start, accounting_end = _append_summary_section(
        "ACCOUNTING REPORT",
        accounting_report_rows,
    )

    footer_header = r
    rows_to_append.append([formatted_date, "Transaction SUMMARY", "", "", ""])
    r += 1
    footer_start = r
    footer_formulas = [
        # NGRS = net(RECHARGE - RECHARGEFEE)
        f"=C{ir+4}-D{ir+4}+C{ir+5}-D{ir+5}",
        # Recharge Out Cluster = net(RECHARGE OUT CLUSTER - RECHARGE OUT CLUSTER FEE)
        f"=C{ir+6}-D{ir+6}+C{ir+7}-D{ir+7}",
        # Reversal - NGRS = net(Reversal NGRS - Reversal NGRS fee)
        f"=(C{ir+8}-D{ir+8})+(C{ir+9}-D{ir+9})",
        # Reversal - Recharge Out Cluster = net(out-cluster reversal - fee)
        f"=(C{ir+10}-D{ir+10})+(C{ir+11}-D{ir+11})",
        # PPOB  = net(FeeTransaksi)
        f"=C{ir+3}-D{ir+3}",
        # ST = net(SELLTHRU family only)
        f"=C{ir+12}-D{ir+12}+C{ir+13}-D{ir+13}+C{ir+14}-D{ir+14}",
        # DISBURSEMENT
        f"=C{ir+2}-D{ir+2}",
        # QRISDUWIT
        f"=C{ir+1}-D{ir+1}",
    ]
    FOOTER_LABELS = [
        "NGRS",
        "Recharge Out Cluster",
        "Reversal - NGRS",
        "Reversal - Recharge Out Cluster",
        "PPOB",
        "ST",
        "DISBURSEMENT",
        "QRISDUWIT",
        "Total",
    ]
    footer_formulas.append(
        "=" + "+".join(f"C{footer_start + j}" for j in range(len(footer_formulas)))
    )
    for i, f_label in enumerate(FOOTER_LABELS):
        rows_to_append.append(["", f_label, footer_formulas[i], "", ""])
        r += 1
    total_row = r - 1
    running_total_row = r
    previous_mandiri_value = (
        f'IFERROR(INDEX(FILTER(C$1:C{running_total_row - 1},'
        f'B$1:B{running_total_row - 1}="MANDIRI"),'
        f'COUNTIF(B$1:B{running_total_row - 1},"MANDIRI")),0)'
    )
    previous_running_total = (
        f'IFERROR(INDEX(FILTER(C$1:C{running_total_row - 1},'
        f'B$1:B{running_total_row - 1}="RUNNING TOTAL"),'
        f'COUNTIF(B$1:B{running_total_row - 1},"RUNNING TOTAL")),0)'
    )
    running_total_formula = (
        f'=IF({previous_mandiri_value}>0,'
        f'C{total_row},'
        f'{previous_running_total}+C{total_row})'
    )
    rows_to_append.append(["", "RUNNING TOTAL", running_total_formula, "", ""])
    r += 1
    footer_end = running_total_row
    mandiri_row = r
    next_transfer_range_start = mandiri_row + 2
    mandiri_formula = (
        f'=IFERROR(INDEX(FILTER(D{next_transfer_range_start}:D,'
        f'TRIM(B{next_transfer_range_start}:B)="TRANSFER MASUK DARI FINPAY",'
        f'D{next_transfer_range_start}:D<>""),1),0)'
    )
    rows_to_append.append(["", "MANDIRI", mandiri_formula, "", ""])
    r += 1
    selisih_row = r
    selisih_formula = (
        f'=IF(OR(C{mandiri_row}="",C{mandiri_row}=0),'
        f'"",C{mandiri_row}-C{running_total_row})'
    )
    selisih_status_formula = (
        f'=IF(OR(C{mandiri_row}="",C{mandiri_row}=0),"pending transfer",'
        f'IF(C{selisih_row}=0,"sesuai",'
        f'IF(C{selisih_row}>0,'
        f'"lebih bayar",'
        f'"kurang bayar")))'
    )
    rows_to_append.append(["", "SELISIH", selisih_formula, selisih_status_formula, ""])
    reconciliation_end = r

    required_rows = max(reconciliation_end, next_transfer_range_start)
    _ensure_row_capacity(sh, ws, required_rows)
    ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    # Formatting
    data_start = insert_row
    data_end = insert_row + len(KETERANGAN) - 1
    IDR = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    def _format_report_section(
        header_row: int,
        body_start: int,
        body_end: int,
        header_color: dict,
        body_color: dict,
    ) -> None:
        ws.format(_range(header_row, header_row), {
            "backgroundColor": header_color,
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
            "horizontalAlignment": "CENTER",
        })
        ws.format(_range(body_start, body_end), {"backgroundColor": body_color})
        ws.format(f"C{body_start}:D{body_end}", {"numberFormat": IDR})
        ws.format(_range(header_row, header_row), {
            "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
        })

    ws.format(_range(data_start, data_end), {"backgroundColor": COL_WHITE})
    ws.format(f"C{data_start}:E{data_end}", {"numberFormat": IDR})
    _format_report_section(
        cash_header, cash_start, cash_end, COL_CASH_HEADER, COL_CASH_BODY
    )
    _format_report_section(
        accounting_header,
        accounting_start,
        accounting_end,
        COL_ACCOUNTING_HEADER,
        COL_ACCOUNTING_BODY,
    )
    _format_report_section(
        footer_header, footer_start, footer_end, COL_FOOTER_HEADER, COL_FOOTER_BODY
    )
    ws.format(_range(footer_start, footer_end, ec=3), {
        "backgroundColor": COL_FOOTER_BODY,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"C{footer_start}:D{footer_end}", {"numberFormat": IDR})
    ws.format(_range(running_total_row, running_total_row), {
        "backgroundColor": COL_INPUT,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"C{running_total_row}", {
        "backgroundColor": COL_INPUT,
        "numberFormat": IDR,
    })
    ws.format(_range(mandiri_row, mandiri_row), {
        "backgroundColor": COL_INPUT,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"C{mandiri_row}", {
        "backgroundColor": COL_INPUT,
        "numberFormat": IDR,
    })
    ws.format(_range(selisih_row, selisih_row), {
        "backgroundColor": COL_STATUS,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"C{selisih_row}", {"numberFormat": IDR})
    ws.format(f"D{selisih_row}", {"wrapStrategy": "WRAP"})
    ws.format(f"C{selisih_row}", {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })
    ws.format(f"A{insert_row}", {
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER, "fontSize": 10}
    })
    ws.format(f"C{total_row}", {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })
    old_dashboard_range = {
        "sheetId": ws.id,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 6,
        "endColumnIndex": 15,
    }
    summary_widths = {0: 100, 1: 320, 2: 140, 3: 140, 4: 140}
    protection_requests = _summary_protection_requests(
        reconciliation_end,
        [*_existing_mandiri_rows(existing_values), mandiri_row],
    )
    sh.batch_update({"requests": [
        *protection_requests,
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "updateCells": {
                "range": old_dashboard_range,
                "rows": [{"values": [{} for _ in range(9)]}],
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
    return insert_row, reconciliation_end


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

    return append_daily_to_gsheet(
        gspread_client, target_spreadsheet, target_worksheet, summary_df
    )
