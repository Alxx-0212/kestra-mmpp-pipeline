"""No-invoice cell reporting matrix and local synthetic design export.

Rendering is pure. Google publication is a separate governed session operation;
writing an XLSX here never opens a spreadsheet or evaluates Excel formulas.
"""
from __future__ import annotations

from pathlib import Path
import re

from .cell_calculation import MAX_EXACT_RUPIAH, detail_categories
from .cell_contracts import business_date, fingerprint

REPORT_HEADERS = ("REPORT DATE", "SECTION", "KETERANGAN", "DEBET", "KREDIT", "SALDO")
BLOCK_OFFSETS = (0, 7, 14, 21)
DESCRIPTION_WIDTH_PX = 430
MIN_GRID_COLUMNS = 100
ROW_GROWTH_BUFFER = 200
REFERENCE_COLORS = {
    "header": "1F4E78", "section": "CCE0F2", "detail": "F6F9FC",
    "detail_alternate": "FFFFFF", "mandiri": "E5EFF9", "cash": "F6EDFC",
    "text": "000000",
}


def cell_grid_capacity(required_rows, required_columns, current=None):
    current = current or {}
    if any(type(value) is not int or value < 1 for value in (required_rows, required_columns)):
        raise ValueError("positive required grid dimensions expected")
    rows = int(current.get("rowCount", 0))
    return {"rowCount": rows if rows >= required_rows else required_rows + ROW_GROWTH_BUFFER,
            "columnCount": max(MIN_GRID_COLUMNS, int(current.get("columnCount", 0)), required_columns)}


def section_border_rows(matrix):
    """Reference PKY: strong section tops; Total/SELISIH on DEBET only."""
    sections, subtotals = [], []
    for index, key in enumerate(matrix["row_keys"][2:], 2):
        section = key.split(":", 1)[0]
        previous = matrix["row_keys"][index - 1].split(":", 1)[0]
        if section in ("DETAIL", "MANDIRI") and section != previous:
            sections.append(index + 1)
        if key in ("MANDIRI:net:Total", "CASH:cash:SELISIH"):
            subtotals.append(index + 1)
    return sections, subtotals


def column_letter(index: int) -> str:
    if type(index) is not int or index < 1:
        raise ValueError("column must be a positive integer")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def mandiri_reset_dates(report: dict, changed_dates: list[str]) -> set[str]:
    from datetime import timedelta

    coverage = {day["report_date"] for day in report["days"]}
    if not changed_dates or not set(changed_dates) <= coverage:
        raise ValueError("reset dates must belong to report coverage")
    result = set(changed_dates)
    for value in changed_dates:
        previous = (business_date(value) - timedelta(days=1)).isoformat()
        if previous in coverage:
            result.add(previous)
    return result


def _template(day: dict) -> list[tuple[str, str, str]]:
    rows = [("DETAIL", label.upper(), key) for label, key in detail_categories(day["report_date"])]
    rows.extend([
        ("DETAIL", "JUMLAH PEMBELIAN RECHARGE OUT CLUSTER", "purchase_count"),
        ("DETAIL", "EXPECTED BIAYA PEMBELIAN RECHARGE OUT CLUSTER FEE", "expected_fee"),
        ("DETAIL", "JUMLAH REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER", "reversal_count"),
    ])
    rows.extend(("MANDIRI", item["label"].upper(), "net:" + item["label"])
                for item in day["combined"]["mandiri"])
    rows.extend(("CASH", label, "cash:" + label) for label in ("RUNNING TOTAL", "MANDIRI", "SELISIH"))
    return rows


def _render_wide_matrix(report: dict, *, manual_values: dict | None = None,
                        reset_dates: set[str] | None = None, dummy: bool = False,
                        bank_from_credit: bool = False) -> dict:
    """Render aligned dates and relative cell formulas; only cell bank inputs unlock.

    manual_values keys are (report_date, cell_code), values integer rupiah.
    Only reset_dates lose those manual values; all other dates retain them.
    """
    manual_values = manual_values or {}
    reset_dates = reset_dates or set()
    config = report["configuration"]
    codes = [cell["cell_code"] for cell in config["cells"]]
    if not 1 <= len(codes) <= 3 or len(set(codes)) != len(codes):
        raise ValueError("matrix requires one to three distinct configured cells")
    days = report["days"]
    if not days:
        raise ValueError("matrix requires continuous report days")
    from datetime import timedelta
    expected_day = business_date(config["first_report_date"])
    for day in days:
        if business_date(day["report_date"]) != expected_day:
            raise ValueError("matrix report dates are not continuous and chronological")
        expected_day += timedelta(days=1)
    if days[-1]["report_date"] != report["through_date"]:
        raise ValueError("matrix coverage end differs from report through_date")
    covered = {day["report_date"] for day in days}
    if not reset_dates <= covered:
        raise ValueError("manual resets outside report coverage")
    if any(key[0] not in covered or key[1] not in codes or type(value) is not int
           for key, value in manual_values.items()):
        raise ValueError("manual MANDIRI values must be scoped integer inputs")
    offsets = BLOCK_OFFSETS[:len(codes) + 1]
    width = offsets[-1] + 6
    matrix = [[""] * width for _ in range(3)]
    preview = [[""] * width for _ in range(3)]
    row_keys = ["HEADERS", "TITLE", "OPENING"]
    edited_inputs, reset_audit, ranges = [], [], []
    positions = {}
    next_row = 4
    for day in days:
        template = _template(day)
        positions[day["report_date"]] = {
            key: next_row + index for index, (_, _, key) in enumerate(template)
        }
        next_row += len(template)

    for slot, offset in enumerate(offsets):
        title = ("TOTAL CLUSTER " + config["account"][:-2]) if slot == 0 else (
            f"C{slot} | CELL {codes[slot - 1]}"
        )
        if dummy:
            title = "DUMMY | " + title
        title_column = 3 if slot == 0 else offset
        matrix[1][title_column] = preview[1][title_column] = title
        matrix[0][offset:offset + 6] = preview[0][offset:offset + 6] = list(REPORT_HEADERS)
        opening = sum(config["opening_balances"].values()) if slot == 0 else config["opening_balances"][codes[slot - 1]]
        matrix[2][offset:offset + 6] = [config["first_report_date"], "OPENING", "OPENING BALANCE", 0, 0, opening]
        preview[2][offset:offset + 6] = matrix[2][offset:offset + 6]
    matrix[2][5] = "=SUM(" + ",".join(f"{column_letter(offset + 6)}3" for offset in offsets[1:]) + ")"
    prior_saldo_rows = {code: 3 for code in codes}
    prior_running = {code: 0 for code in codes}
    prior_bank = {code: 0 for code in codes}
    prior_running_rows = {}
    prior_bank_rows = {}

    for day_index, day in enumerate(days):
        date_string = day["report_date"]
        template = _template(day)
        day_positions = positions[date_string]
        cells = {cell["cell_code"]: cell for cell in day["cells"]}
        if set(cells) != set(codes):
            raise ValueError("matrix day has missing or unknown cells")
        states = {}
        for code in codes:
            cell = cells[code]
            if day_index == 0:
                expected_opening = config["opening_balances"][code]
            else:
                expected_opening = next(c for c in days[day_index - 1]["cells"] if c["cell_code"] == code)["closing_saldo"]
            if cell["opening_saldo"] != expected_opening:
                raise ValueError("matrix opening balance discontinuity")
            states[code] = {"saldo": expected_opening}
        start_row = len(matrix) + 1
        for section, label, key in template:
            row_number = len(matrix) + 1
            row, numerical = [""] * width, [""] * width
            for slot, offset in enumerate(offsets[1:], 1):
                code = codes[slot - 1]
                cell, state = cells[code], states[code]
                debit_col, credit_col, saldo_col = [column_letter(offset + col) for col in (4, 5, 6)]
                debit, credit, saldo = 0, 0, ""
                debit_formula = credit_formula = saldo_formula = None
                if key in {category for _, category in detail_categories(date_string)}:
                    measure = cell["measures"].get(key, {})
                    debit, credit = measure.get("debet", 0), measure.get("kredit", 0)
                    if abs(state["saldo"] + debit) > MAX_EXACT_RUPIAH:
                        raise ValueError("running saldo intermediate exceeds safe spreadsheet arithmetic")
                    state["saldo"] += debit - credit
                    saldo = state["saldo"]
                    saldo_formula = f"={saldo_col}{prior_saldo_rows[code]}+{debit_col}{row_number}-{credit_col}{row_number}"
                    prior_saldo_rows[code] = row_number
                elif key == "purchase_count":
                    debit, credit = cell["measures"].get("PEMBELIAN RECHARGE OUT CLUSTER", {}).get("count", 0), ""
                elif key == "expected_fee":
                    debit = ""
                    credit = cell["measures"].get("PEMBELIAN RECHARGE OUT CLUSTER", {}).get("count", 0) * 200
                    credit_formula = f"={debit_col}{day_positions['purchase_count']}*200"
                elif key == "reversal_count":
                    debit, credit = cell["measures"].get("REVERSAL - PEMBELIAN RECHARGE OUT CLUSTER", {}).get("count", 0), ""
                elif key.startswith("net:"):
                    measure = next(item for item in cell["mandiri"] if item["label"] == key[4:])
                    debit, credit = measure["signed"], ""
                    # Net rows remain signed, matching the existing daily footer.
                    if measure["label"] == "Total":
                        refs = [f"{debit_col}{day_positions['net:' + item['label']]}" for item in cell["mandiri"][:-1]]
                        debit_formula = "=" + "+".join(refs)
                    else:
                        from .cell_calculation import MANDIRI_GROUPS
                        group = next(keys for name, keys in MANDIRI_GROUPS if name == measure["label"])
                        if measure["label"] == "ST" and "SELLTHRUSALESFEE" in day_positions:
                            group = (*group, "SELLTHRUSALESFEE")
                        debit_formula = "=" + "+".join(
                            f"({debit_col}{day_positions[item]}-{credit_col}{day_positions[item]})" for item in group
                        )
                elif key == "cash:RUNNING TOTAL":
                    total = next(item["signed"] for item in cell["mandiri"] if item["label"] == "Total")
                    debit = total if isinstance(prior_bank[code], int) and prior_bank[code] > 0 else prior_running[code] + total
                    credit = ""
                    if code in prior_running_rows:
                        prev_bank = f"{debit_col}{prior_bank_rows[code]}"
                        debit_formula = (
                            f"=IF(AND(ISNUMBER({prev_bank}),{prev_bank}>0),"
                            f"{debit_col}{day_positions['net:Total']},"
                            f"{debit_col}{prior_running_rows[code]}+{debit_col}{day_positions['net:Total']})"
                        )
                    else:
                        debit_formula = f"={debit_col}{day_positions['net:Total']}"
                    state["running"] = debit
                elif key == "cash:MANDIRI":
                    credit = ""
                    default = "PENDING NEXT DAY"
                    if day_index + 1 < len(days):
                        following = days[day_index + 1]
                        next_cell = next(item for item in following["cells"] if item["cell_code"] == code)
                        source = "kredit" if bank_from_credit else "debet"
                        default = next_cell["measures"].get("CASHOUT APOLLO", {}).get(source, 0)
                        bank_source_col = credit_col if bank_from_credit else debit_col
                        debit_formula = f"={bank_source_col}{positions[following['report_date']]['CASHOUT APOLLO']}"
                    automatic_default = debit_formula or default
                    input_key = (date_string, code)
                    if input_key in manual_values and date_string not in reset_dates:
                        debit, debit_formula = manual_values[input_key], None
                    else:
                        debit = default
                        if input_key in manual_values:
                            reset_audit.append({"report_date": date_string, "cell_code": code,
                                                "previous_value": manual_values[input_key],
                                                "replacement_value": debit_formula or default})
                    state["bank"] = debit
                    edited_inputs.append({"report_date": date_string, "cell_code": code,
                                          "row": row_number, "column": offset + 4,
                                           "default": automatic_default if bank_from_credit else debit_formula or default})
                elif key == "cash:SELISIH":
                    bank = state["bank"]
                    debit = bank - state["running"] if isinstance(bank, int) and bank != 0 else ""
                    credit = ("SESUAI" if debit == 0 else "LEBIH BAYAR" if isinstance(debit, int) and debit > 0
                              else "KURANG BAYAR" if isinstance(debit, int) else "PENDING TRANSFER")
                    bank_ref, run_ref = (f"{debit_col}{day_positions['cash:MANDIRI']}", f"{debit_col}{day_positions['cash:RUNNING TOTAL']}")
                    debit_formula = f'=IF(AND(ISNUMBER({bank_ref}),{bank_ref}<>0),{bank_ref}-{run_ref},"")'
                    credit_formula = f'=IF({debit_col}{row_number}="","PENDING TRANSFER",IF({debit_col}{row_number}=0,"SESUAI",IF({debit_col}{row_number}>0,"LEBIH BAYAR","KURANG BAYAR")))'
                numerical[offset:offset + 6] = [date_string, section, label, debit, credit, saldo]
                row[offset:offset + 6] = [date_string, section, label,
                                        debit_formula or debit, credit_formula or credit, saldo_formula or saldo]

            # Combined additive and signed-net rows reference all district cells.
            row[:3] = numerical[:3] = [date_string, section, label]
            for column in (4, 5, 6):
                values = [numerical[offset + column - 1] for offset in offsets[1:]]
                refs = [f"{column_letter(offset + column)}{row_number}" for offset in offsets[1:]]
                numeric = all(type(value) is int for value in values)
                if numeric:
                    if sum(abs(value) for value in values) > MAX_EXACT_RUPIAH:
                        raise ValueError("combined sum exceeds safe spreadsheet arithmetic")
                    numerical[column - 1] = sum(values)
                    row[column - 1] = "=SUM(" + ",".join(refs) + ")"
                elif key == "cash:MANDIRI" and column == 4:
                    numerical[column - 1] = "PENDING NEXT DAY"
                    row[column - 1] = f'=IF(COUNT({",".join(refs)})={len(codes)},SUM({",".join(refs)}),"PENDING NEXT DAY")'
            if section == "MANDIRI":
                signed_values = [numerical[offset + 3] for offset in offsets[1:]]
                signed = sum(signed_values)
                references = ",".join(f"{column_letter(offset + 4)}{row_number}" for offset in offsets[1:])
                numerical[3], numerical[4] = max(signed, 0), max(-signed, 0)
                row[3] = f"=MAX(SUM({references}),0)"
                row[4] = f"=MAX(-SUM({references}),0)"
            if key == "cash:SELISIH":
                bank_row, run_row = day_positions["cash:MANDIRI"], day_positions["cash:RUNNING TOTAL"]
                actual_bank, actual_run = preview[bank_row - 1][3], preview[run_row - 1][3]
                difference = actual_bank - actual_run if type(actual_bank) is int and actual_bank != 0 else ""
                numerical[3] = difference
                numerical[4] = ("PENDING TRANSFER" if difference == "" else "SESUAI" if difference == 0
                                else "LEBIH BAYAR" if difference > 0 else "KURANG BAYAR")
                row[3] = f'=IF(AND(ISNUMBER(D{bank_row}),D{bank_row}<>0),D{bank_row}-D{run_row},"")'
                row[4] = f'=IF(D{row_number}="","PENDING TRANSFER",IF(D{row_number}=0,"SESUAI",IF(D{row_number}>0,"LEBIH BAYAR","KURANG BAYAR")))'
            matrix.append(row)
            preview.append(numerical)
            row_keys.append(section + ":" + key)
        for code in codes:
            if states[code]["saldo"] != cells[code]["closing_saldo"]:
                raise ValueError("rendered cell closing balance differs from calculated result")
            prior_running[code], prior_bank[code] = states[code]["running"], states[code]["bank"]
            prior_running_rows[code] = day_positions["cash:RUNNING TOTAL"]
            prior_bank_rows[code] = day_positions["cash:MANDIRI"]
        ranges.append({"report_date": date_string, "start_row": start_row, "end_row": len(matrix)})
    if any(type(value) is int and abs(value) > MAX_EXACT_RUPIAH for row in preview for value in row):
        raise ValueError("report amount exceeds exact spreadsheet integer precision")
    return {
        "account": config["account"], "cluster_id": config["account"][:-2],
        "report_fingerprint": fingerprint(report), "dummy": dummy,
        "layout_version": 2,
        "values": matrix, "numeric_preview": preview, "row_keys": row_keys,
        "blocks": [{"slot": "TOTAL" if index == 0 else f"C{index}",
                    "cell_code": None if index == 0 else codes[index - 1],
                    "start_column": offset + 1, "end_column": offset + 6}
                   for index, offset in enumerate(offsets)],
        "editable_inputs": edited_inputs, "manual_reset_audit": reset_audit,
        "date_ranges": ranges,
    }


def render_cell_matrix(report: dict, *, manual_values=None, reset_dates=None, dummy=False,
                       presentation_version=3) -> dict:
    """Project the unchanged financial row template into compact numeric blocks.

    Version 2 remains available to verify/migrate the already published test
    baseline. No source, category, date, or numeric calculation is changed here.
    """
    wide = _render_wide_matrix(report, manual_values=manual_values, reset_dates=reset_dates, dummy=dummy,
                               bank_from_credit=presentation_version == 5)
    if presentation_version == 2:
        return wide
    if presentation_version not in (3, 4, 5):
        raise ValueError("unsupported cell presentation version")
    columns = {1: 1, 2: 2, 3: 3}
    blocks = []
    source_blocks = wide["blocks"] if presentation_version == 3 else wide["blocks"][1:]
    for index, old in enumerate(source_blocks):
        start = 4 + index * 4
        old_start = old["start_column"] + 3
        for offset in range(3):
            columns[old_start + offset] = start + offset
        blocks.append({**old, "start_column": start, "end_column": start + 2})
    width = blocks[-1]["end_column"]
    letters = {column_letter(old): column_letter(new) for old, new in columns.items()}

    def remap(value):
        if not isinstance(value, str) or not value.startswith("="):
            return value
        def reference(match):
            if match[2] not in letters:
                raise ValueError("formula references a removed detail/spacer column")
            return match[1] + letters[match[2]] + match[3] + match[4]
        return re.sub(r"(?<![A-Za-z0-9_])(\$?)([A-Z]{1,3})(\$?)(\d+)", reference, value)

    projected = {}
    for key in ("values", "numeric_preview"):
        rows = []
        for old_row in wide[key]:
            row = [""] * width
            for old, new in columns.items():
                row[new - 1] = remap(old_row[old - 1]) if key == "values" else old_row[old - 1]
            rows.append(row)
        rows[0] = [""] * width
        rows[1] = [""] * width
        rows[0][:3] = list(REPORT_HEADERS[:3])
        for old, block in zip(source_blocks, blocks):
            old_start = 3 if old["cell_code"] is None else old["start_column"] - 1
            rows[0][block["start_column"] - 1] = wide[key][1][old_start]
            rows[1][block["start_column"] - 1:block["end_column"]] = list(REPORT_HEADERS[3:])
        projected[key] = rows
    return {
        **wide, **projected, "layout_version": presentation_version, "row_keys": ["TITLE", "HEADERS", *wide["row_keys"][2:]],
        "blocks": blocks, "shared_detail_columns": 3,
        "format_palette": REFERENCE_COLORS.copy(),
        "editable_inputs": [{**entry, "column": columns[entry["column"]], "default": remap(entry["default"])}
                            for entry in wide["editable_inputs"]],
        "manual_reset_audit": [{**entry, "replacement_value": remap(entry["replacement_value"])}
                               for entry in wide["manual_reset_audit"]],
    }


def section_fill(matrix, row_index):
    if row_index <= 2:
        return REFERENCE_COLORS["header"]
    key = matrix["row_keys"][row_index - 1]
    if key.startswith("MANDIRI:"):
        return REFERENCE_COLORS["mandiri"]
    if key.startswith("CASH:"):
        return REFERENCE_COLORS["cash"]
    return REFERENCE_COLORS["detail"] if row_index % 2 == 0 else REFERENCE_COLORS["detail_alternate"]


def write_cell_design_workbook(path: str | Path, matrices: list[dict]) -> None:
    """Write formulas and a separately calculated preview, not formula-cache proof."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
    from openpyxl.workbook.properties import CalcProperties

    if not matrices or not all(matrix["dummy"] for matrix in matrices):
        raise ValueError("design workbook export requires explicitly marked dummy matrices")
    workbook = Workbook()
    index = workbook.active
    index.title = "DUMMY DATA - READ FIRST"
    index.append(["SYNTHETIC DESIGN ONLY - NOT A LIVE REPORT"])
    index.append(["Formula tabs need Excel/Sheets recalculation. Numeric tabs are Python previews."])
    index.append(["No real balances, credentials, invoices, database or Google writes are used."])
    index.append(["Compact editable inputs retain the Cash-section fill. Protection here is a design aid, not access control."])
    index.append(["CLUSTER", "CELLS", "FORMULA TAB", "NUMERIC PREVIEW TAB"])
    index.column_dimensions["A"].width = 100
    for matrix in matrices:
        cluster = matrix["cluster_id"]
        index.append([cluster, sum(block["cell_code"] is not None for block in matrix["blocks"]),
                      cluster, cluster + " VALUES"])
        for numerical, title in ((False, cluster), (True, cluster + " VALUES")):
            sheet = workbook.create_sheet(title)
            values = matrix["numeric_preview"] if numerical else matrix["values"]
            editable = {(entry["row"], entry["column"]) for entry in matrix["editable_inputs"]}
            compact = matrix.get("layout_version") in (3, 4, 5)
            spacers = {block["start_column"] - 1 for block in matrix["blocks"][1:]} if compact else {7, 14, 21}
            numeric_columns = {column for block in matrix["blocks"]
                               for column in range(block["start_column"], block["end_column"] + 1)} if compact else set()
            for row_index, values_row in enumerate(values, 1):
                for column, value in enumerate(values_row, 1):
                    cell = sheet.cell(row=row_index, column=column, value=value)
                    if isinstance(value, str) and not value.startswith("="):
                        cell.data_type = "s"
                    cell.font = Font(name="Arial", size=10, color=REFERENCE_COLORS["text"] if compact else "24333E")
                    cell.alignment = Alignment(vertical="center", wrap_text=False,
                        horizontal="center" if compact and row_index <= 2 else "right" if column in numeric_columns else "left")
                    cell.number_format = '#,##0;[Red](#,##0);"-"'
                    cell.protection = Protection(locked=numerical or (row_index, column) not in editable)
                    if column in spacers:
                        cell.fill = PatternFill("solid", fgColor="FFFFFF")
                    elif compact:
                        cell.fill = PatternFill("solid", fgColor=section_fill(matrix, row_index))
                        if row_index <= 2:
                            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
                        elif column == 2 and matrix["row_keys"][row_index - 1].split(":", 1)[0] in ("DETAIL", "MANDIRI"):
                            section = matrix["row_keys"][row_index - 1].split(":", 1)[0]
                            previous = matrix["row_keys"][row_index - 2].split(":", 1)[0]
                            if section != previous:
                                cell.fill = PatternFill("solid", fgColor=REFERENCE_COLORS["section"])
                                cell.font = Font(name="Arial", size=10, bold=True, color=REFERENCE_COLORS["text"])
                    elif row_index <= 2:
                        cell.fill = PatternFill("solid", fgColor="243D4D")
                        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
                    elif (row_index, column) in editable:
                        cell.fill = PatternFill("solid", fgColor="FFF1BE")
                    else:
                        section = matrix["row_keys"][row_index - 1].split(":", 1)[0]
                        color = "E8EFF3" if section == "MANDIRI" else "E7F1E9" if section == "CASH" else ("F3F6F8" if row_index % 2 == 0 else "FFFFFF")
                        cell.fill = PatternFill("solid", fgColor=color)
                    light = Side(style="thin", color="A5B2BF")
                    cell.border = Border() if column in spacers else Border(
                        top=light if compact else None, bottom=light,
                        left=light if compact else None, right=light if compact else None)
            if compact:
                for column, width in enumerate((14, 12, DESCRIPTION_WIDTH_PX / 7), 1):
                    sheet.column_dimensions[column_letter(column)].width = width
                    sheet.merge_cells(start_row=1, start_column=column, end_row=2, end_column=column)
            for block in matrix["blocks"]:
                first = block["start_column"]
                widths = (17, 19, 19) if compact else (14, 12, 47, 17, 19, 19)
                for offset, width in enumerate(widths):
                    sheet.column_dimensions[column_letter(first + offset)].width = width
                title_start = first if compact else 4 if first == 1 else first
                title_row, end = (1, block["end_column"]) if compact else (2, first + 5)
                sheet.merge_cells(start_row=title_row, start_column=title_start, end_row=title_row, end_column=end)
                for column in range(title_start, end + 1):
                    sheet.cell(title_row, column).fill = PatternFill("solid", fgColor=REFERENCE_COLORS["header"] if compact else "243D4D")
                edge = Side(style="medium", color=REFERENCE_COLORS["header"] if compact else "243D4D")
                sections, subtotals = section_border_rows(matrix)
                starts = set(sections) if compact else {3, *(item["start_row"] for item in matrix["date_ranges"])}
                ends = set() if compact else {item["end_row"] for item in matrix["date_ranges"]}
                for row_index in range(1, len(values) + 1):
                    for column in range(first, end + 1):
                        cell = sheet.cell(row=row_index, column=column)
                        old = cell.border
                        cell.border = Border(
                            left=edge if column == first else old.left,
                            right=edge if column == end else old.right,
                            top=edge if row_index in starts or row_index == 1 else old.top,
                            bottom=edge if row_index in ends or row_index == 2 else old.bottom,
                        )
                if compact:
                    for row_index in subtotals:
                        cell = sheet.cell(row_index, first)
                        old = cell.border
                        cell.border = Border(top=edge, bottom=old.bottom, left=old.left, right=old.right)
            if compact:
                edge = Side(style="medium", color=REFERENCE_COLORS["header"])
                for row_index in section_border_rows(matrix)[0]:
                    for column in range(1, 4):
                        cell = sheet.cell(row_index, column)
                        old = cell.border
                        cell.border = Border(top=edge, bottom=old.bottom, left=old.left, right=old.right)
            for spacer in spacers:
                if spacer < len(values[0]):
                    sheet.column_dimensions[column_letter(spacer)].width = 3
            sheet.freeze_panes = "D3" if compact else "D2"
            sheet.sheet_view.showGridLines = False
            sheet.protection.sheet = True
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
    workbook.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
