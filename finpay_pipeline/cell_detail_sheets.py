"""Cell-tagged cluster/date detail projections for the versioned cell flow."""
from __future__ import annotations

from datetime import datetime

from .cell_calculation import CellReportConfig
from .cell_contracts import CellIdentity, business_date, fingerprint

UNUSUAL_HEADERS = [
    "REPORT DATE", "CELL", "NO", "TRANSACTION DATE", "TRANSACTION ID", "BASE ID",
    "TRANSACTION", "KREDIT", "DEBET", "SALDO AWAL", "SALDO AKHIR", "NOMOR RS",
    "REMARKS", "UNUSUAL REASON",
]
REVERSAL_HEADERS = [
    "REPORT DATE", "CELL", "NO", "TRANSACTION DATE", "TRANSACTION ID",
    "TRANSACTION TYPE", "TRANSACTION", "KREDIT", "DEBET", "SALDO AWAL",
    "SALDO AKHIR", "NOMOR RS", "REMARKS",
]


def _date_time(value, report_date):
    if not isinstance(value, str):
        raise ValueError("detail transaction date must be a string")
    parsed = None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern)
            break
        except ValueError:
            continue
    if parsed is None or parsed.date() != business_date(report_date):
        raise ValueError("detail row date differs from target report date")
    return parsed.strftime("%d/%m/%Y %H:%M:%S"), parsed


def _financial_value(row, key):
    value = row.get(key)
    if type(value) is not int or abs(value) > 2 ** 53 - 1:
        raise ValueError(f"detail {key} must be a safe integer")
    return value


def _cell_rows(config: CellReportConfig, cell_days: list[dict], report_date: str) -> dict[str, list[dict]]:
    expected = {cell.cell_code: cell for cell in config.cells}
    config_id = fingerprint(config.to_dict())
    found = {}
    for day in cell_days:
        identity = day.get("identity", {})
        parsed = CellIdentity(**identity)
        if parsed.cell_code not in expected or parsed != expected[parsed.cell_code]:
            raise ValueError("detail evidence has unknown cell identity")
        if day.get("report_date") != report_date or day.get("config_id") != config_id:
            raise ValueError("detail evidence date/configuration mismatch")
        if parsed.cell_code in found:
            raise ValueError("duplicate detail cell/date evidence")
        calculation = day.get("calculation")
        if not isinstance(calculation, dict) or calculation.get("report_date") != report_date:
            raise ValueError("detail evidence has no validated calculation")
        expected_id = fingerprint({
            "generation_id": day.get("generation_id"), "config_id": config_id,
            "rules_version": config.rules_version, "calculation": calculation,
        })
        if day.get("calculation_id") != expected_id:
            raise ValueError("detail calculation fingerprint mismatch")
        found[parsed.cell_code] = calculation
    if set(found) != set(expected):
        raise ValueError("detail date is missing required cell coverage")
    return found


def build_cell_detail_matrix(config: CellReportConfig, cell_days: list[dict],
                             report_date: str, kind: str) -> dict:
    """Merge a complete cell cohort while retaining a truthful CELL value."""
    if kind not in ("unusual", "reversal"):
        raise ValueError("detail kind must be unusual or reversal")
    business_date(report_date)
    by_cell = _cell_rows(config, cell_days, report_date)
    display = {code: int(code) - int(config.account[:-2]) for code in by_cell}
    if any(number not in (1, 2, 3) for number in display.values()) or len(set(display.values())) != len(display):
        raise ValueError("unsupported or ambiguous cluster-relative cell number")
    headers = UNUSUAL_HEADERS if kind == "unusual" else REVERSAL_HEADERS
    rows = []
    for code in sorted(by_cell):
        source_rows = by_cell[code].get(kind) or []
        for source in source_rows:
            timestamp, sort_time = _date_time(source.get("Transaction Date"), report_date)
            transaction_id = source.get("Transaction ID")
            remarks = source.get("Remarks")
            label = source.get("processed_transaction_label")
            if not isinstance(transaction_id, str) or not transaction_id.strip():
                raise ValueError("detail transaction ID must remain nonblank exact text")
            if not isinstance(remarks, str) or not isinstance(label, str):
                raise ValueError("detail source text is missing")
            common = {
                "transaction_date_sort": sort_time,
                "cell_code": code,
                "transaction_id": transaction_id,
                "report_date": business_date(report_date).strftime("%d/%m/%Y"),
                "transaction_date": timestamp,
                "transaction": label.upper(),
                "kredit": _financial_value(source, "Kredit"),
                "debet": _financial_value(source, "Debet"),
                "saldo_awal": _financial_value(source, "Saldo Awal"),
                "saldo_akhir": _financial_value(source, "Saldo Akhir"),
                "nomor_rs": source.get("Nomor RS", ""),
                "remarks": remarks,
            }
            if not isinstance(common["nomor_rs"], str):
                raise ValueError("detail Nomor RS must remain text")
            if kind == "unusual":
                reason = source.get("unusual_reason")
                base_id = source.get("base_id")
                if not isinstance(reason, str) or not isinstance(base_id, str):
                    raise ValueError("unusual detail lacks base ID or reason")
                values = [common["report_date"], display[code], 0, common["transaction_date"],
                          transaction_id, base_id, common["transaction"], common["kredit"],
                          common["debet"], common["saldo_awal"], common["saldo_akhir"],
                          common["nomor_rs"], common["remarks"], reason]
            else:
                transaction_type = source.get("Transaction Type")
                if not isinstance(transaction_type, str):
                    raise ValueError("reversal detail lacks transaction type")
                values = [common["report_date"], display[code], 0, common["transaction_date"], transaction_id,
                          transaction_type.upper(), common["transaction"], common["kredit"],
                          common["debet"], common["saldo_awal"], common["saldo_akhir"],
                          common["nomor_rs"], common["remarks"]]
            rows.append((sort_time, code, transaction_id, values))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    values = [list(headers)]
    for index, (_, _, _, row) in enumerate(rows, 1):
        row[2] = index  # New display counter; never claim to preserve source No.
        values.append(row)
    return {"cluster_id": config.account[:-2], "account": config.account,
            "report_date": report_date, "kind": kind, "headers": list(headers),
            "cell_codes": sorted(by_cell), "cell_display_map": display, "layout_version": 3, "rows": values,
            "day_separator_rows": [2] if rows else [],
            "row_count": len(rows), "empty": not rows}


def build_cell_detail_history(config: CellReportConfig, cell_days: list[dict], kind: str) -> dict:
    """Build one whole-cluster output for a contiguous persisted history range."""
    if not cell_days:
        raise ValueError("cell detail history cannot be empty")
    dates = sorted({item.get("report_date") for item in cell_days})
    first = business_date(dates[0])
    from datetime import timedelta
    for offset, value in enumerate(dates):
        if business_date(value) != first + timedelta(days=offset):
            raise ValueError("cell detail history has a missing calendar date")
    by_date = []
    for report_date in dates:
        current = [item for item in cell_days if item.get("report_date") == report_date]
        matrix = build_cell_detail_matrix(config, current, report_date, kind)
        by_date.extend(matrix["rows"][1:])
    headers = UNUSUAL_HEADERS if kind == "unusual" else REVERSAL_HEADERS
    return {"cluster_id": config.account[:-2], "account": config.account,
            "start_date": dates[0], "end_date": dates[-1], "kind": kind,
            "cell_codes": sorted(cell.cell_code for cell in config.cells),
            "cell_display_map": matrix["cell_display_map"], "layout_version": 3,
            "day_separator_rows": [index + 2 for index, row in enumerate(by_date)
                                   if index == 0 or row[0] != by_date[index - 1][0]],
            "headers": list(headers), "rows": [list(headers), *by_date], "row_count": len(by_date)}


def detail_format_requests(sheet_id: int, matrix: dict) -> list[dict]:
    """Reproduce the existing detail-sheet table treatment with CELL inserted."""
    if type(sheet_id) is not int or sheet_id < 0:
        raise ValueError("explicit detail worksheet ID required")
    headers = matrix.get("headers")
    rows = matrix.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list) or not rows or rows[0] != headers:
        raise ValueError("validated detail matrix with header is required")
    width, height = len(headers), len(rows)
    if any(len(row) != width for row in rows):
        raise ValueError("detail matrix width mismatch")

    def grid(start_row, end_row, start_column, end_column):
        return {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row,
                "startColumnIndex": start_column, "endColumnIndex": end_column}

    dark_blue = {"red": 31 / 255, "green": 78 / 255, "blue": 120 / 255}
    light_blue = {"red": 229 / 255, "green": 240 / 255, "blue": 1}
    white = {"red": 1, "green": 1, "blue": 1}
    requests = [
        {"repeatCell": {"range": grid(0, height, 0, width), "cell": {"userEnteredFormat": {
            "backgroundColor": white, "textFormat": {"fontFamily": "Arial", "fontSize": 10,
                "bold": False, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
            "horizontalAlignment": "LEFT", "verticalAlignment": "TOP", "wrapStrategy": "CLIP"}},
            "fields": "userEnteredFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 0, "hideGridlines": True}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount,hideGridlines)"}},
        {"repeatCell": {"range": grid(0, 1, 0, width), "cell": {"userEnteredFormat": {
            "backgroundColor": dark_blue, "textFormat": {"fontFamily": "Arial", "fontSize": 10,
                "bold": True, "foregroundColor": white}, "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
        {"setBasicFilter": {"filter": {"range": grid(0, height, 0, width)}}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": 0, "endIndex": height}, "properties": {"pixelSize": 21}, "fields": "pixelSize"}},
        {"updateBorders": {"range": grid(0, height, 0, width), **{side: {"style": "NONE"} for side in
            ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}},
    ]
    # Explicit stripes are observable in effectiveFormat, unlike banding overlays.
    for row in range(1, height):
        requests.append({"repeatCell": {"range": grid(row, row + 1, 0, width),
            "cell": {"userEnteredFormat": {"backgroundColor": white if row % 2 else light_blue}},
            "fields": "userEnteredFormat.backgroundColor"}})
    widths = {
        "REPORT DATE": 110, "CELL": 70 if matrix.get("layout_version") == 3 else 50, "NO": 70, "TRANSACTION DATE": 165,
        "TRANSACTION ID": 300, "BASE ID": 220, "TRANSACTION TYPE": 150,
        "TRANSACTION": 410, "KREDIT": 120, "DEBET": 120, "SALDO AWAL": 130,
        "SALDO AKHIR": 130, "NOMOR RS": 130,
        "REMARKS": 480 if matrix["kind"] == "reversal" else 420, "UNUSUAL REASON": 480,
    }
    money = {"KREDIT", "DEBET", "SALDO AWAL", "SALDO AKHIR"}
    text = {"TRANSACTION ID", "BASE ID", "TRANSACTION TYPE", "TRANSACTION", "NOMOR RS"}
    for index, header in enumerate(headers):
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id,
            "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
                "properties": {"pixelSize": matrix.get("style_contract", {}).get("widths", [widths.get(name, 120) for name in headers])[index]},
                "fields": "pixelSize"}})
        if header in money and height > 1:
            requests.append({"repeatCell": {"range": grid(1, height, index, index + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER",
                    "pattern": "#,##0;(#,##0);-"}, "horizontalAlignment": "RIGHT"}},
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"}})
        elif header in {"CELL", "NO"} and height > 1:
            requests.append({"repeatCell": {"range": grid(1, height, index, index + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"},
                    "horizontalAlignment": "CENTER"}}, "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"}})
        elif header == "REPORT DATE" and height > 1:
            requests.append({"repeatCell": {"range": grid(1, height, index, index + 1),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
                "fields": "userEnteredFormat.horizontalAlignment"}})
        elif header in text and height > 1:
            requests.append({"repeatCell": {"range": grid(1, height, index, index + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat"}})
        elif header in {"REMARKS", "UNUSUAL REASON"} and height > 1:
            requests.append({"repeatCell": {"range": grid(1, height, index, index + 1),
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy"}})
    if height > 1:
        requests.append({"updateBorders": {"range": grid(1, height, 0, width),
            "innerHorizontal": {"style": "SOLID", "color": {"red": 216 / 255, "green": 221 / 255, "blue": 226 / 255}}}})
        for first in matrix.get("day_separator_rows", [2]):
            requests.append({"updateBorders": {"range": grid(first - 1, first, 0, width),
                "top": {"style": "SOLID_MEDIUM", "color": dark_blue}}})
        requests.append({"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id,
            "dimension": "ROWS", "startIndex": 1, "endIndex": height}}})
    return requests
