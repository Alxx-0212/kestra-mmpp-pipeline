"""FinPay-only per-cell invoice panels; LinkAja remains a separate workflow."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .cell_calculation import MAX_EXACT_RUPIAH, CellReportConfig
from .cell_contracts import CellIdentity, business_date, fingerprint

PANEL_HEADERS = ("REPORT DATE", "INVOICE REPORT", "DEBET", "KREDIT")
SALES_FEE_CUTOFF = business_date("2026-09-01")
SALES_FEE_RESTORED = business_date("2026-10-01")
SALES_FEE_LABEL = "BIAYA FEE BAR A ST (HOLD)"
INVOICE_COLORS = {
    name: {channel: int(value[index:index + 2], 16) / 255
           for channel, index in (("red", 0), ("green", 2), ("blue", 4))}
    for name, value in {
        "header": "1F4E78", "cash_header": "D1E8CC", "cash_body": "EBF8E8",
        "sellthru_header": "DBE4FF", "sellthru_body": "EFF3FF",
        "accounting_header": "F9E5B2", "accounting_body": "FFF6D6",
        "white": "FFFFFF", "text": "000000", "border": "A5B2BF",
    }.items()
}


def _amount(value):
    if type(value) is not int:
        raise ValueError("invoice source amount must be an exact integer rupiah value")
    if abs(value) > MAX_EXACT_RUPIAH:
        raise ValueError("invoice amount exceeds exact spreadsheet precision")
    return value


def _split(measures: dict, key: str) -> tuple[int, int]:
    measure = measures.get(key, {"debet": 0, "kredit": 0})
    net = _amount(_amount(measure["debet"]) - _amount(measure["kredit"]))
    return max(net, 0), max(-net, 0)


def _qris_groups(rows: list[dict]) -> dict[str, int]:
    amounts: dict[str, int] = defaultdict(int)
    sort_keys = {}
    for row in rows:
        value = row.get("Disbursement Date")
        text = "" if value is None else str(value).strip()
        if not text:
            label, sort = "MISSING DISBURSEMENT DATE", (2, "")
        else:
            parsed = None
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed:
                label, sort = parsed.strftime("%d/%m/%Y"), (0, parsed.date())
            else:
                label, sort = text, (1, text)
        amount = _amount(row.get("Kredit"))
        amounts[label] += amount
        if abs(amounts[label]) > MAX_EXACT_RUPIAH:
            raise ValueError("QRIS invoice group exceeds exact spreadsheet precision")
        sort_keys.setdefault(label, sort)
    return {label: amounts[label] for label in sorted(amounts, key=lambda item: sort_keys[item])}


def _cell_facts(cell_days: list[dict], config: CellReportConfig) -> dict:
    identities = {cell.cell_code: cell for cell in config.cells}
    expected = set(identities)
    expected_config = fingerprint(config.to_dict())
    result = {}
    for day in cell_days:
        identity = day.get("identity", {})
        try:
            parsed_identity = CellIdentity(**identity)
        except (TypeError, ValueError):
            raise ValueError("invoice facts have an invalid cell identity") from None
        if parsed_identity.cell_code not in identities or parsed_identity != identities[parsed_identity.cell_code]:
            raise ValueError("invoice facts belong to another account/cluster")
        code, report_date = identity.get("cell_code"), day.get("report_date")
        if code not in expected or not report_date:
            raise ValueError("invoice facts have an unknown cell/date")
        key = (report_date, code)
        if key in result:
            raise ValueError("duplicate invoice cell/date facts")
        calculation = day.get("calculation")
        if not isinstance(calculation, dict) or calculation.get("report_date") != report_date:
            raise ValueError("invoice facts lack their validated cell calculation")
        if day.get("config_id") != expected_config or day.get("rules_version") != config.rules_version:
            raise ValueError("invoice facts use a stale calculation configuration")
        expected_calculation_id = fingerprint({
            "generation_id": day.get("generation_id"), "config_id": expected_config,
            "rules_version": config.rules_version, "calculation": calculation,
        })
        if day.get("calculation_id") != expected_calculation_id:
            raise ValueError("invoice calculation fingerprint mismatch")
        result[key] = calculation
    if not result:
        raise ValueError("invoice requires per-cell calculation facts")
    dates = sorted({day for day, _ in result})
    first = business_date(dates[0])
    for index, value in enumerate(dates):
        from datetime import timedelta
        if business_date(value) != first + timedelta(days=index):
            raise ValueError("invoice report dates are not contiguous")
        if any((value, code) not in result for code in expected):
            raise ValueError(f"invoice date {value} has incomplete cell coverage")
    return result


def _summary_measures(calculation: dict) -> dict[str, dict]:
    measures = {}
    for row in calculation.get("summary", []):
        label = str(row["Transaction"]).strip().upper()
        if label in measures:
            raise ValueError("duplicate cell invoice summary label")
        values = (row["Sum_of_Kredit"], row["Sum_of_Debet"], row["Transaction_Count"])
        if any(type(item) is not int for item in values):
            raise ValueError("invoice summary values must be exact integer rupiah/counts")
        measures[label] = {"debet": values[0], "kredit": values[1], "count": values[2]}
    return measures


def _records_for_day(day, cells):
    """Make one shared category/date row template for all invoice panels."""
    codes = sorted(cells)
    measures = {code: _summary_measures(cells[code]) for code in codes}
    qrises = {code: _qris_groups(cells[code].get("qris", [])) for code in codes}
    qris_labels = sorted({label for groups in qrises.values() for label in groups}, key=lambda label: (
        2 if label == "MISSING DISBURSEMENT DATE" else 1 if not _is_invoice_date(label) else 0,
        _invoice_date_key(label), label,
    ))
    include_sales_fee = (
        day < SALES_FEE_CUTOFF
        or (day >= SALES_FEE_RESTORED and any(
            "SELLTHRUSALESFEE" in measures[code] for code in codes
        ))
    )
    records: list[tuple[str, str, dict[str, tuple[int | str, int | str]]]] = []

    def add(section, label, values):
        records.append((section, label, values))

    for section, rows in (
        ("CASH IN - NGRS", (
            ("NGRS", "RECHARGE"), ("Recharge Fee", "RECHARGEFEE"),
            ("Pembelian Recharge Out Cluster", "PEMBELIAN RECHARGE OUT CLUSTER"),
        )),
        ("SELLTHRU", (("ST", "SELLTHRU"), ("BIAYA FEE ST", "SELLTHRUFEE"))),
        ("ACCOUNTING", (
            ("PPOB", "FEETRANSAKSI"), ("DISBURSEMENT", "DISBURSEMENT"),
            ("Recharge Out Cluster", "RECHARGE OUT CLUSTER"),
            ("Recharge Out Cluster FEE", "RECHARGE OUT CLUSTER FEE"),
            ("Reversal - Recharge Out Cluster", "REVERSAL - RECHARGE OUT CLUSTER"),
            ("Reversal - Recharge Out Cluster FEE", "REVERSAL - RECHARGE OUT CLUSTER FEE"),
        )),
    ):
        add(section, "SECTION", {code: ("", "") for code in codes})
        for label, category in rows:
            values = {code: _split(measures[code], category) for code in codes}
            add(section, label, values)
        if section == "CASH IN - NGRS":
            purchases = {code: measures[code].get("PEMBELIAN RECHARGE OUT CLUSTER", {}).get("count", 0)
                         for code in codes}
            add(section, "Expected Biaya Pembelian Recharge Out Cluster FEE",
                {code: (0, _amount(count * 200)) for code, count in purchases.items()})
            if qris_labels:
                add(section, "QRISDUWIT", {code: ("", "") for code in codes})
                for label in qris_labels:
                    add(section, "QRISDUWIT - " + label,
                        {code: (qrises[code].get(label, 0), "") for code in codes})
        if section == "SELLTHRU" and include_sales_fee:
            add(section, SALES_FEE_LABEL, {
                code: _split(measures[code], "SELLTHRUSALESFEE") for code in codes
            })
    return records


def _is_invoice_date(value):
    try:
        datetime.strptime(value, "%d/%m/%Y")
        return True
    except ValueError:
        return False


def _invoice_date_key(value):
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return value


def build_cell_invoice_matrix(config: CellReportConfig, cell_days: list[dict], *, dummy=False) -> dict:
    facts = _cell_facts(cell_days, config)
    codes = sorted(cell.cell_code for cell in config.cells)
    widths = 4 * len(codes) + 3 * max(0, len(codes) - 1)
    matrix = [[""] * widths for _ in range(2)]
    row_sections = [None, None]
    section_ranges = []
    qris_subtitle_rows, qris_child_rows = [], []
    panels = []
    for index, code in enumerate(codes):
        start = 1 + index * 7
        panels.append({"cell_code": code, "start_column": start, "end_column": start + 3})
        matrix[0][start - 1:start + 3] = [f"C{index + 1} | CELL {code}", "", "", ""]
        matrix[1][start - 1:start + 3] = ["REPORT DATE", "INVOICE REPORT", "DEBET", "KREDIT"]
    date_strings = sorted({day for day, _ in facts})
    for date_index, date_string in enumerate(date_strings):
        day = business_date(date_string)
        per_code = {code: facts[(date_string, code)] for code in codes}
        for section, label, values in _records_for_day(day, per_code):
            if label == "QRISDUWIT":
                qris_subtitle_rows.append(len(matrix) + 1)
            elif label.startswith("QRISDUWIT - "):
                qris_child_rows.append(len(matrix) + 1)
            if label == "SECTION":
                section_ranges.append({"report_date": date_string, "section": section,
                    "start_row": len(matrix) + 1, "end_row": len(matrix) + 1})
            row = [""] * widths
            for code in codes:
                panel = next(item for item in panels if item["cell_code"] == code)
                start = panel["start_column"] - 1
                if not section and not label:
                    row[start:start + 4] = ["", "", "", ""]
                elif label == "SECTION":
                    row[start:start + 4] = [day.strftime("%d/%m/%Y"), section, "", ""]
                else:
                    debit, credit = values[code]
                    row[start:start + 4] = [day.strftime("%d/%m/%Y"), label, debit, credit]
            matrix.append(row)
            row_sections.append(section)
            section_ranges[-1]["end_row"] = len(matrix)
        if date_index < len(date_strings) - 1:
            matrix.extend([[""] * widths for _ in range(2)])
            row_sections.extend([None, None])
    return {
        "account": config.account, "cluster_id": config.account[:-2],
        "dummy": dummy, "layout_version": 4, "panels": panels,
        "headers": list(PANEL_HEADERS), "values": matrix, "row_sections": row_sections,
        "dates": sorted({day for day, _ in facts}),
        "section_ranges": section_ranges,
        "qris_subtitle_rows": qris_subtitle_rows, "qris_child_rows": qris_child_rows,
        "scope_note": "FinPay-only invoice; LinkAja fees and separate LinkAja workflow are excluded.",
        "fingerprint": fingerprint(matrix),
    }


def invoice_format_requests(sheet_id: int, matrix: dict) -> list[dict]:
    if type(sheet_id) is not int or sheet_id < 0:
        raise ValueError("explicit invoice worksheet ID required")
    values = matrix.get("values")
    if not values or any(len(row) != len(values[0]) for row in values):
        raise ValueError("invoice matrix rows must have a fixed width")
    width, height = len(values[0]), len(values)

    def grid(sr, er, sc, ec):
        return {"sheetId": sheet_id, "startRowIndex": sr, "endRowIndex": er,
                "startColumnIndex": sc, "endColumnIndex": ec}

    requests = [
        {"repeatCell": {"range": grid(0, height, 0, width), "cell": {"userEnteredFormat": {
            "backgroundColor": INVOICE_COLORS["white"], "horizontalAlignment": "LEFT",
            "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP", "textFormat": {
                "fontFamily": "Arial", "fontSize": 10, "bold": False,
                "foregroundColor": INVOICE_COLORS["text"]}}}, "fields": "userEnteredFormat"}},
        {"updateBorders": {"range": grid(0, height, 0, width), **{side: {"style": "NONE"} for side in
            ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 0, "hideGridlines": True}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount,hideGridlines)"}},
        {"repeatCell": {"range": grid(0, 2, 0, width), "cell": {"userEnteredFormat": {
            "backgroundColor": INVOICE_COLORS["header"], "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
            "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True,
                           "foregroundColor": INVOICE_COLORS["white"]}}}, "fields": "userEnteredFormat"}},
    ]
    titles = {"CASH IN - NGRS": ("cash_header", "cash_body"),
              "SELLTHRU": ("sellthru_header", "sellthru_body"),
              "ACCOUNTING": ("accounting_header", "accounting_body")}
    blocks = matrix["panels"]
    edge = {"style": "SOLID_MEDIUM", "color": INVOICE_COLORS["header"]}
    light = {"style": "SOLID", "color": INVOICE_COLORS["border"]}
    for block in blocks:
        start, end = block["start_column"] - 1, block["end_column"]
        sizes = (140 if matrix.get("layout_version") in (3, 4) else 110, 410, 140, 140)
        contract_widths = matrix.get("style_contract", {}).get("widths")
        if contract_widths:
            sizes = contract_widths[start:end]
        for offset, pixels in enumerate(sizes):
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id,
                "dimension": "COLUMNS", "startIndex": start + offset, "endIndex": start + offset + 1},
                "properties": {"pixelSize": pixels}, "fields": "pixelSize"}})
        if matrix.get("layout_version") in (3, 4):
            requests.append({"mergeCells": {"range": grid(0, 1, start, end), "mergeType": "MERGE_ALL"}})
        requests.append({"repeatCell": {"range": grid(2, height, start + 2, start + 4),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}}},
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}})
        requests.append({"repeatCell": {"range": grid(2, height, start, start + 1),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"}})
        requests.append({"updateBorders": {"range": grid(0, 2, start, end),
            "top": edge, "bottom": edge, "left": edge, "right": edge,
            "innerVertical": light, "innerHorizontal": light}})
        for section in matrix["section_ranges"]:
            first, last = section["start_row"] - 1, section["end_row"]
            heading, body = titles[section["section"]]
            requests.extend([
                {"repeatCell": {"range": grid(first, last, start, end), "cell": {
                    "userEnteredFormat": {"backgroundColor": INVOICE_COLORS[body]}},
                    "fields": "userEnteredFormat.backgroundColor"}},
                {"repeatCell": {"range": grid(first, first + 1, start, end), "cell": {
                    "userEnteredFormat": {"backgroundColor": INVOICE_COLORS[heading],
                        "textFormat": {"bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat.bold)"}},
                {"repeatCell": {"range": grid(first, first + 1, start + 1, start + 2),
                    "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                    "fields": "userEnteredFormat.horizontalAlignment"}},
                {"updateBorders": {"range": grid(first, last, start, end), "top": edge,
                    "bottom": edge, "left": edge, "right": edge,
                    "innerVertical": light, "innerHorizontal": light}},
            ])
        if matrix.get("layout_version") == 4:
            for row in matrix.get("qris_subtitle_rows", []):
                requests.append({"repeatCell": {"range": grid(row - 1, row, start, end),
                    "cell": {"userEnteredFormat": {"backgroundColor": INVOICE_COLORS["cash_header"],
                        "horizontalAlignment": "CENTER", "verticalAlignment": "BOTTOM",
                        "textFormat": {"bold": True, "foregroundColor": INVOICE_COLORS["header"]}}},
                    "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat.bold,textFormat.foregroundColor)"}})
            for row in matrix.get("qris_child_rows", []):
                requests.extend([
                    {"repeatCell": {"range": grid(row - 1, row, start, end), "cell": {"userEnteredFormat": {
                        "verticalAlignment": "BOTTOM", "textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat(verticalAlignment,textFormat.bold)"}},
                    {"repeatCell": {"range": grid(row - 1, row, start + 1, start + 2), "cell": {"userEnteredFormat": {
                        "textFormat": {"foregroundColor": INVOICE_COLORS["header"]}}},
                        "fields": "userEnteredFormat.textFormat.foregroundColor"}},
                ])
            for row in [*matrix.get("qris_subtitle_rows", []), *matrix.get("qris_child_rows", [])]:
                requests.append({"repeatCell": {"range": grid(row - 1, row, start + 2, end),
                    "cell": {"userEnteredFormat": {"wrapStrategy": "OVERFLOW_CELL"}},
                    "fields": "userEnteredFormat.wrapStrategy"}})

    for panel_index in range(len(blocks) - 1):
        spacer, next_start = blocks[panel_index]["end_column"], blocks[panel_index + 1]["start_column"] - 1
        area = grid(0, height, spacer, next_start)
        requests.append({"repeatCell": {"range": area, "cell": {"userEnteredFormat": {
            "backgroundColor": INVOICE_COLORS["white"]}}, "fields": "userEnteredFormat.backgroundColor"}})
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": spacer, "endIndex": next_start}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}})
        # The canvas was cleared before drawing panels. Clearing a gutter border
        # here also erases the adjacent panel's outline in the native Sheets API.
    requests.append({"setBasicFilter": {"filter": {"range": grid(1, height, 0, width)}}})
    if matrix.get("layout_version") == 4:
        for panel in blocks:
            requests.append({"updateBorders": {"range": grid(0, height, panel["start_column"] - 1, panel["end_column"]),
                "left": edge, "right": edge}})
    return requests
