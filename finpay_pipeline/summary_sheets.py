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
)
from .sheets_common import (
    _add_protected_range_request,
    _add_protected_sheet_request,
    _delete_all_protected_range_requests,
    _delete_sheet_rows,
    _horizontal_border_requests,
    _insert_blank_sheet_rows,
    _mandiri_editor_emails,
    ensure_row_capacity,
    open_or_create_finpay_spreadsheet,
    uppercase_sheet_rows,
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

    ws.format("A1:K1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"})
    ws.format("F2", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}})
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
    Returns (insert_row, block_end) on success.
    Replaces the existing daily block for the same date on reruns.
    """
    target_date_str = summary_df["Transaction_Date"].max()
    formatted_date  = datetime.strptime(target_date_str, "%Y-%m-%d").strftime("%d/%m/%Y")

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
    COL_ACCOUNTING_HEADER = {"red": 0.980, "green": 0.900, "blue": 0.700}
    COL_ACCOUNTING_BODY = {"red": 1.000, "green": 0.965, "blue": 0.840}
    COL_TABLE_BORDER = {"red": 0.650, "green": 0.700, "blue": 0.750}
    COL_STATUS = {"red": 0.965, "green": 0.930, "blue": 0.990}
    COL_FOOTER_HEADER = {"red": 0.800, "green": 0.880, "blue": 0.950}
    COL_FOOTER_BODY = {"red": 0.900, "green": 0.940, "blue": 0.980}
    COL_INPUT = COL_STATUS
    COL_MUTED_TEXT = {"red": 0.420, "green": 0.420, "blue": 0.420}

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

            invoice_table_row = row + 3
            if invoice_table_row <= max_row:
                requests.extend(_horizontal_border_requests(
                    ws,
                    invoice_table_row,
                    invoice_table_row,
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
            # New layout: KETERANGAN is column C and MANDIRI input is column D.
            if len(row) > 2 and str(row[2]).strip().upper() == "MANDIRI":
                if len(row) > 3 and str(row[3]).strip().upper() == "MANDIRI":
                    # Previous 7-column layout kept TRANSACTION KEY in column D.
                    cells.append((idx, 5))
                else:
                    cells.append((idx, 4))
                continue

            # Legacy layout: KETERANGAN was column B and MANDIRI input was column C.
            if len(row) > 1 and str(row[1]).strip().upper() == "MANDIRI":
                cells.append((idx, 3))
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
        sh.batch_update({"requests": [{
            "unmergeCells": {
                "range": _grid_range(
                    max(2, replacement_start - 1),
                    max(replacement_rows),
                    8,
                    11,
                )
            }
        }]})
        _delete_sheet_rows(sh, ws, replacement_rows)
        print(
            f"↻ Replacing existing summary block for {formatted_date} "
            f"({len(replacement_rows)} rows)."
        )
        existing_values = ws.get_all_values()
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
    ws.update("A1:K1", uppercase_sheet_rows([summary_headers]), value_input_option="USER_ENTERED")

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
        {"label": "BIAYA FEE BAR A. ST",        "key": "SELLTHRUSALESFEE"},
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
        ("QRISDUWIT", *_split_net_formula(_detail_net_formula("QRISDUWIT"))),
    ]

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
        ("ST", *_split_net_formula(_detail_net_formula("SELLTHRU"))),
        ("BIAYA FEE ST", *_split_net_formula(_detail_net_formula("SELLTHRUFEE"))),
        (
            "BIAYA FEE BAR A. ST",
            *_split_net_formula(_detail_net_formula("SELLTHRUSALESFEE")),
        ),
    ]

    invoice_rows = [
        [formatted_date, "CASH IN", "", ""],
        *[[formatted_date, *row] for row in cash_report_rows],
        ["", "", "", ""],
        [formatted_date, "ACCOUNTING", "", ""],
        *[[formatted_date, *row] for row in accounting_report_rows],
    ]

    footer_start = r
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
        # ST = net(SELLTHRU family only)
        (
            _detail_net_formula("SELLTHRU")
            + _detail_net_formula("SELLTHRUFEE").replace("=", "+")
            + _detail_net_formula("SELLTHRUSALESFEE").replace("=", "+")
        ),
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
    next_transfer_range_start = mandiri_row + 2
    mandiri_formula = (
        f'=IFERROR(INDEX(FILTER(E{next_transfer_range_start}:E,'
        f'TRIM(C{next_transfer_range_start}:C)="TRANSFER MASUK DARI FINPAY",'
        f'D{next_transfer_range_start}:D<>"CASHOUT APOLLO",'
        f'E{next_transfer_range_start}:E<>""),1),'
        f'IFERROR(INDEX(FILTER(F{next_transfer_range_start}:F,'
        f'TRIM(C{next_transfer_range_start}:C)="TRANSFER MASUK DARI FINPAY",'
        f'D{next_transfer_range_start}:D="CASHOUT APOLLO",'
        f'F{next_transfer_range_start}:F<>""),1),'
        f'IFERROR(INDEX(FILTER(D{next_transfer_range_start}:D,'
        f'TRIM(B{next_transfer_range_start}:B)="TRANSFER MASUK DARI FINPAY",'
        f'D{next_transfer_range_start}:D<>""),1),0)))'
    )
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
    invoice_start = detail_start + 3
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
    cash_invoice_header_row = invoice_start
    cash_invoice_end = cash_invoice_header_row + len(cash_report_rows)
    invoice_separator_row = cash_invoice_end + 1
    accounting_invoice_header_row = cash_invoice_end + 2
    invoice_end = accounting_invoice_header_row + len(accounting_report_rows)
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

    required_rows = max(block_end, next_transfer_range_start)
    if replacement_start:
        _insert_blank_sheet_rows(sh, ws, replacement_start, len(rows_to_append))
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

    def _format_report_section(
        start_row: int,
        end_row: int,
        first_row_color: dict,
        body_color: dict,
    ) -> None:
        ws.format(_range(start_row, end_row), {"backgroundColor": body_color})
        ws.format(f"D{start_row}:F{end_row}", {"numberFormat": IDR})
        if end_row > start_row:
            ws.format(f"A{start_row + 1}:B{end_row}", {
                "textFormat": {"foregroundColor": COL_MUTED_TEXT},
            })
        ws.format(f"A{start_row}:B{start_row}", {
            "backgroundColor": first_row_color,
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
        })

    def _format_detail_stripes(start_row: int, end_row: int) -> None:
        for row_number in range(start_row, end_row + 1, 2):
            stripe_start_col = 3 if row_number == start_row else 1
            ws.format(_range(row_number, row_number, stripe_start_col, 6), {
                "backgroundColor": COL_DETAIL_ALT,
            })

    ws.format(_range(data_start, data_end, 1, 11), {"backgroundColor": COL_WHITE})
    ws.format(f"D{data_start}:F{data_end}", {"numberFormat": IDR})
    ws.format(f"H{data_start}:H{data_end}", {
        "numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"},
    })
    ws.format(f"J{data_start}:K{data_end}", {"numberFormat": IDR})
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
    ws.format(f"D{running_total_row}:E{selisih_row}", {"numberFormat": IDR})
    ws.format(_range(mandiri_row, mandiri_row), {
        "backgroundColor": COL_INPUT,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"D{mandiri_row}", {
        "backgroundColor": COL_INPUT,
        "numberFormat": IDR,
    })
    ws.format(_range(selisih_row, selisih_row), {
        "backgroundColor": COL_STATUS,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"D{selisih_row}", {"numberFormat": IDR})
    ws.format(f"E{selisih_row}", {"wrapStrategy": "WRAP"})
    if invoice_rows:
        ws.format(_range(cash_invoice_header_row, cash_invoice_header_row, 8, 11), {
            "backgroundColor": COL_CASH_HEADER,
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
            "horizontalAlignment": "CENTER",
        })
        ws.format(_range(cash_invoice_header_row + 1, cash_invoice_end, 8, 11), {
            "backgroundColor": COL_CASH_BODY,
        })
        ws.format(_range(invoice_separator_row, invoice_separator_row, 8, 11), {
            "backgroundColor": COL_WHITE,
        })
        ws.format(
            _range(accounting_invoice_header_row, accounting_invoice_header_row, 8, 11),
            {
                "backgroundColor": COL_ACCOUNTING_HEADER,
                "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
                "horizontalAlignment": "CENTER",
            },
        )
        ws.format(_range(accounting_invoice_header_row + 1, invoice_end, 8, 11), {
            "backgroundColor": COL_ACCOUNTING_BODY,
        })
        ws.format(f"I{invoice_start}:I{invoice_end}", {
            "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
            "horizontalAlignment": "LEFT",
            "wrapStrategy": "CLIP",
        })
        ws.format(f"H{invoice_start}:H{invoice_end}", {
            "horizontalAlignment": "CENTER",
            "wrapStrategy": "CLIP",
        })
        ws.format(f"J{invoice_start}:K{invoice_end}", {
            "horizontalAlignment": "RIGHT",
        })
        ws.format(f"I{cash_invoice_header_row}", {"horizontalAlignment": "CENTER"})
        ws.format(f"I{accounting_invoice_header_row}", {"horizontalAlignment": "CENTER"})
    ws.format("A1:F1", {
        "backgroundColor": COL_HEADER,
        "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
    })
    ws.format("G1", {"backgroundColor": COL_WHITE})
    ws.format("H1:K1", {
        "backgroundColor": COL_HEADER,
        "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
    })
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
        *_unmerge_range_requests(cash_invoice_header_row, cash_invoice_header_row, 9, 11),
        *_unmerge_range_requests(
            accounting_invoice_header_row,
            accounting_invoice_header_row,
            9,
            11,
        ),
        *_unmerge_range_requests(2, 2, 8, 11),
        *blank_invoice_merge_requests,
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
        *_horizontal_border_requests(
            ws,
            invoice_start,
            invoice_start,
            8,
            11,
            top=True,
            bottom=False,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        *_horizontal_border_requests(
            ws,
            invoice_start,
            invoice_start,
            8,
            11,
            top=False,
            bottom=True,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        *_horizontal_border_requests(
            ws,
            accounting_invoice_header_row,
            accounting_invoice_header_row,
            8,
            11,
            top=True,
            bottom=False,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        *_horizontal_border_requests(
            ws,
            accounting_invoice_header_row,
            accounting_invoice_header_row,
            8,
            11,
            top=False,
            bottom=True,
            style="SOLID_MEDIUM",
            color=COL_HEADER,
        ),
        *_invoice_table_border_requests(
            invoice_start,
            cash_invoice_end,
        ),
        *_invoice_table_border_requests(
            accounting_invoice_header_row,
            invoice_end,
        ),
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
