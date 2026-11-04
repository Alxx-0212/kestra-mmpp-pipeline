"""Four FinPay projections, one cluster/workbook publication epoch, no retries."""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime

from .cell_calculation import build_cluster_report, recalculate_stale_history
from .cell_contracts import business_date, fingerprint
from .cell_database import validate_cluster_batch
from .cell_detail_sheets import build_cell_detail_history, detail_format_requests
from .cell_invoice_sheets import build_cell_invoice_matrix, invoice_format_requests
from .cell_observability import report
from .cell_publication import (
    GUARD_PREFIX, MANAGED_PREFIX, MAX_REQUEST_BYTES, _editors_match, _format_requests,
    _manual_values, _marker_entries, _marker_request, _padded, _protection_requests,
    _status_requests, _verify_final_protections, _verify_marker, matrix_write_requests,
    _validate_input_policy,
)
from .cell_sheets import cell_grid_capacity, mandiri_reset_dates, render_cell_matrix

ROLES = ("summary", "invoice", "unusual", "reversal")
CLUSTER_ALIASES = {"412110_B": "PKY", "422090_B": "MRT", "422010_B": "BGI", "422220_B": "TNT",
    "422100_B": "TDR", "422180_B": "MRW", "422140_B": "KOTAMOB", "422040_B": "BOLMONG"}


def cell_worksheet_title(account, role, naming_scheme="code"):
    if role not in ROLES:
        raise ValueError("unknown worksheet role")
    if naming_scheme == "code":
        return f"{account[:-2]} {role.title()}"
    if naming_scheme != "cluster_alias" or account not in CLUSTER_ALIASES:
        raise ValueError("unknown or unverified worksheet naming scheme/cluster")
    alias = CLUSTER_ALIASES[account]
    return alias if role == "summary" else f"{alias} {role.title()}"
STYLE_REVISION = "finpay-cell-invoice-bank-validation-20261018"
# Pinned from the original PKY invoice/detail formatting, not from writer requests.
STYLE_PALETTE = {
    "header": "1F4E78", "white": "FFFFFF", "black": "000000", "grid": "A5B2BF",
    "section": "CCE0F2", "detail": "F6F9FC", "mandiri": "E5EFF9", "cash": "F6EDFC",
    "cash_header": "D1E8CC", "cash_body": "EBF8E8", "sellthru_header": "DBE4FF",
    "sellthru_body": "EFF3FF", "accounting_header": "F9E5B2", "accounting_body": "FFF6D6",
    "stripe": "E5F0FF", "detail_grid": "D8DDE2",
}


def bind_cell_style_contract(role, matrix):
    """Declare expected presentation. Only verified live COMPLETE attests it."""
    values = matrix.setdefault("values", matrix.get("rows"))
    height, width = len(values), len(values[0])
    widths, merges = [24] * width, []
    if role == "summary":
        if matrix.get("layout_version") not in (4, 5):
            raise ValueError("new summary style requires the cell-only layout")
        stripes = matrix.get("row_stripes")
        if stripes is None:
            stripes = ["detail" if index % 2 else "white" for index in range(height)]
        if len(stripes) != height or any(value not in ("detail", "white") for value in stripes):
            raise ValueError("Summary row stripes do not match its committed height")
        matrix["row_stripes"] = list(stripes)
        widths[:3] = [115, 100, 430]
        merges = [[0, 2, col, col + 1] for col in range(3)]
        for block in matrix["blocks"]:
            start, end = block["start_column"] - 1, block["end_column"]
            widths[start:end] = [150] * (end - start)
            merges.append([0, 1, start, end])
    elif role == "invoice":
        for panel in matrix["panels"]:
            start, end = panel["start_column"] - 1, panel["end_column"]
            widths[start:end] = [140 if matrix.get("layout_version") in (3, 4) else 110, 410, 140, 140]
            if matrix.get("layout_version") in (3, 4):
                merges.append([0, 1, start, end])
    elif role in ("unusual", "reversal"):
        stripes = matrix.get("row_stripes")
        if stripes is None:
            stripes = ["white" if index % 2 else "stripe" for index in range(height)]
        if len(stripes) != height or any(value not in ("white", "stripe") for value in stripes):
            raise ValueError("detail row stripes do not match its committed height")
        matrix["row_stripes"] = list(stripes)
        sizes = {"REPORT DATE": 110, "CELL": 70 if matrix.get("layout_version") == 3 else 50, "NO": 70, "TRANSACTION DATE": 165,
            "TRANSACTION ID": 300, "BASE ID": 220, "TRANSACTION TYPE": 150, "TRANSACTION": 410,
            "KREDIT": 120, "DEBET": 120, "SALDO AWAL": 130, "SALDO AKHIR": 130,
            "NOMOR RS": 130, "REMARKS": 480 if role == "reversal" else 420, "UNUSUAL REASON": 480}
        widths = [sizes[header] for header in matrix["headers"]]
    else:
        raise ValueError("unknown style role")
    matrix["style_contract"] = {"schema_version": 1, "revision": STYLE_REVISION, "role": role,
        "height": height, "width": width, "widths": widths, "merges": sorted(merges),
        "frozen_rows": 2 if role in ("summary", "invoice") else 1,
        "frozen_columns": 3 if role == "summary" else 0, "palette": dict(STYLE_PALETTE)}
    _validate_input_policy(matrix)
    return matrix


def _expected_style(matrix, row, column, section):
    """Independent semantic oracle; never replays the formatting producer."""
    contract = matrix["style_contract"]
    role, palette = contract["role"], contract["palette"]
    header = row < contract["frozen_rows"]
    expected = {"background": palette["header"] if header else palette["white"],
        "foreground": palette["white"] if header else palette["black"], "family": "Arial", "size": 10,
        "bold": header, "align": "CENTER" if header else "LEFT", "vertical": "MIDDLE",
        "wrap": "CLIP", "borders": dict.fromkeys(("top", "bottom", "left", "right"), ("NONE", None))}
    edge, light = ("SOLID_MEDIUM", palette["header"]), ("SOLID", palette["grid"])
    if role == "summary":
        block = next((b for b in matrix["blocks"] if b["start_column"] - 1 <= column < b["end_column"]), None)
        if column >= 3 and block is None:
            expected["background"] = palette["white"]
            return expected
        first, last = (0, 3) if column < 3 else (block["start_column"] - 1, block["end_column"])
        expected["borders"] = dict.fromkeys(("top", "bottom", "left", "right"), light)
        if column == first:
            expected["borders"]["left"] = edge
        if column == last - 1:
            expected["borders"]["right"] = edge
        if row == 0:
            expected["borders"]["top"] = edge
        if row == 1:
            expected["borders"]["bottom"] = edge
        if not header:
            key, previous = matrix["row_keys"][row], matrix["row_keys"][row - 1]
            group = key.split(":", 1)[0]
            stripes = matrix.get("row_stripes", [])
            stripe = stripes[row] if len(stripes) == len(matrix["values"]) else None
            expected["background"] = (palette["mandiri"] if group == "MANDIRI" else
                palette["cash"] if group == "CASH" else
                palette[stripe] if stripe else palette["detail"] if row % 2 else palette["white"])
            if group in ("DETAIL", "MANDIRI") and group != previous.split(":", 1)[0]:
                expected["borders"]["top"] = edge
                if column == 1:
                    expected.update(background=palette["section"], bold=True)
            if block:
                expected.update(align="RIGHT", number_type="NUMBER", number_pattern="#,##0")
                if key in ("MANDIRI:net:Total", "CASH:cash:SELISIH") and column == first:
                    expected["borders"]["top"] = edge
    elif role == "invoice":
        panel = next((p for p in matrix["panels"] if p["start_column"] - 1 <= column < p["end_column"]), None)
        if panel is None:
            expected["background"] = palette["white"]
            return expected
        start, end = panel["start_column"] - 1, panel["end_column"]
        if not header:
            expected["align"] = "CENTER" if column == start else "LEFT" if column == start + 1 else "RIGHT"
            if column >= start + 2:
                expected.update(number_type="NUMBER", number_pattern="#,##0;(#,##0);-")
        if header or section:
            first, last = (0, 2) if header else (section["start_row"] - 1, section["end_row"])
            expected["borders"] = dict.fromkeys(("top", "bottom", "left", "right"), light)
            for side, active in (("top", row == first), ("bottom", row == last - 1),
                                 ("left", column == start), ("right", column == end - 1)):
                if active:
                    expected["borders"][side] = edge
            if not header:
                prefix = {"CASH IN - NGRS": "cash", "SELLTHRU": "sellthru", "ACCOUNTING": "accounting"}[section["section"]]
                expected["background"] = palette[prefix + ("_header" if row == first else "_body")]
                if row == first:
                    expected["bold"] = True
                    if column == start + 1:
                        expected["align"] = "CENTER"
        if matrix.get("layout_version") == 4:
            if column == start:
                expected["borders"]["left"] = edge
            if column == end - 1:
                expected["borders"]["right"] = edge
            if row + 1 in matrix.get("qris_subtitle_rows", []):
                expected.update(background=palette["cash_header"], foreground=palette["header"],
                    bold=True, align="CENTER", vertical="BOTTOM")
            elif row + 1 in matrix.get("qris_child_rows", []):
                expected.update(bold=True, vertical="BOTTOM")
                if column == start + 1:
                    expected["foreground"] = palette["header"]
            if column >= start + 2 and row + 1 in [*matrix.get("qris_subtitle_rows", []), *matrix.get("qris_child_rows", [])]:
                expected["wrap"] = "OVERFLOW_CELL"
    else:
        expected["wrap"] = "WRAP" if header else "CLIP"
        if not header:
            stripes = matrix.get("row_stripes", [])
            stripe = stripes[row] if len(stripes) == contract["height"] else None
            expected.update(background=palette[stripe] if stripe else
                            palette["white"] if row % 2 else palette["stripe"], vertical="TOP")
            name = matrix["headers"][column]
            if name in ("CELL", "NO"):
                expected.update(align="CENTER", number_type="NUMBER", number_pattern="0")
            elif name in ("KREDIT", "DEBET", "SALDO AWAL", "SALDO AKHIR"):
                expected.update(align="RIGHT", number_type="NUMBER", number_pattern="#,##0;(#,##0);-")
            elif name in ("TRANSACTION ID", "BASE ID", "TRANSACTION TYPE", "TRANSACTION", "NOMOR RS"):
                expected["number_type"] = "TEXT"
            elif name == "REPORT DATE":
                expected["align"] = "RIGHT"
            elif name in ("REMARKS", "UNUSUAL REASON"):
                expected["wrap"] = "WRAP"
            if row + 1 in matrix.get("day_separator_rows", [2]):
                expected["borders"]["top"] = edge
            elif row > 1:
                expected["borders"]["top"] = ("SOLID", palette["detail_grid"])
            if row < contract["height"] - 1:
                expected["borders"]["bottom"] = ("SOLID", palette["detail_grid"])
    return expected


def _color_hex(container, name, fallback):
    color = container.get(name)
    if color is None:
        style = container.get(name + "Style", {})
        if "themeColor" in style:
            raise ValueError("unresolved theme color in attested style")
        color = style.get("rgbColor")
    if color is None:
        return fallback
    components = [color.get(channel, 0) for channel in ("red", "green", "blue")]
    if any(type(value) not in (int, float) or not 0 <= value <= 1 for value in components):
        raise ValueError("invalid effective style color")
    return "".join(f"{round(value * 255):02X}" for value in components)


def _style_view(value, default):
    value = value or default
    text = value.get("textFormat", {})
    default_text = default.get("textFormat", {})
    result = {"background": _color_hex(value, "backgroundColor", "FFFFFF"),
        "foreground": _color_hex(text, "foregroundColor", "000000"),
        "family": text.get("fontFamily", default_text.get("fontFamily", "Arial")),
        "size": text.get("fontSize", default_text.get("fontSize", 10)), "bold": text.get("bold", False),
        "align": value.get("horizontalAlignment", "LEFT"),
        "vertical": value.get("verticalAlignment", default.get("verticalAlignment", "BOTTOM")),
        "wrap": value.get("wrapStrategy", default.get("wrapStrategy", "OVERFLOW_CELL")),
        "number_type": value.get("numberFormat", {}).get("type"),
        "number_pattern": value.get("numberFormat", {}).get("pattern"), "borders": {}}
    for side in ("top", "bottom", "left", "right"):
        border = value.get("borders", {}).get(side, {})
        style = border.get("style", "NONE")
        result["borders"][side] = (style, None if style == "NONE" else _color_hex(border, "color", "000000"))
    return result


def _range_box(area):
    return [area.get("startRowIndex", 0), area["endRowIndex"],
            area.get("startColumnIndex", 0), area["endColumnIndex"]]


def _visible_style(matrix, row, column, sections):
    """Native GridData exposes a merged header through its top-left anchor."""
    merge = next((area for area in matrix["style_contract"]["merges"]
                  if area[0] <= row < area[1] and area[2] <= column < area[3]), None)
    if merge and (row, column) != (merge[0], merge[2]):
        if matrix["values"][row][column] not in ("", None):
            raise ValueError("merged continuation would conceal a matrix value")
        return None
    expected = _expected_style(matrix, row, column, sections.get(row))
    if merge:
        first, last, left, right = merge
        for side, r, c in (("top", first, left), ("bottom", last - 1, left),
                           ("left", first, left), ("right", first, right - 1)):
            expected["borders"][side] = _expected_style(matrix, r, c, sections.get(r))["borders"][side]
    return expected


def _shared_edge(first, second=("NONE", None)):
    strength = {"NONE": 0, "DOTTED": 1, "DASHED": 1, "SOLID": 1,
                "SOLID_MEDIUM": 2, "SOLID_THICK": 3, "DOUBLE": 4}
    if first[0] not in strength or second[0] not in strength:
        raise ValueError("unsupported effective border style")
    if strength[first[0]] == strength[second[0]] and first != second:
        raise ValueError("conflicting shared border colors/styles")
    return first if strength[first[0]] >= strength[second[0]] else second


def _verify_style(adapter, matrix, previous=None, *, allow_unattested=False):
    contract = matrix.get("style_contract")
    if contract is None and allow_unattested:
        return  # Old layout is a value/protection baseline, not a style attestation.
    if (not isinstance(contract, dict) or contract.get("schema_version") != 1
        or contract.get("role") not in ROLES or not isinstance(contract.get("revision"), str)
        or not 0 < len(contract["revision"]) <= 128
        or set(contract.get("palette", {})) != set(STYLE_PALETTE)
        or any(not isinstance(value, str) or not re.fullmatch(r"[A-F0-9]{6}", value)
            for value in contract.get("palette", {}).values())):
        raise ValueError("missing or unsupported presentation style contract")
    supported = (4, 5) if contract["role"] == "summary" else (2, 3, 4) if contract["role"] == "invoice" else (2, 3)
    if matrix.get("layout_version") not in supported:
        raise ValueError("unsupported attested style layout")
    values = matrix["values"]
    height, width = len(values), len(values[0])
    if (contract["height"], contract["width"]) != (height, width) or len(contract["widths"]) != width:
        raise ValueError("style contract dimensions differ from matrix")
    old = previous["values"] if previous else values
    read_height, read_width = max(height, len(old)), max(width, len(old[0]))
    sections = {row: section for section in matrix.get("section_ranges", [])
                for row in range(section["start_row"] - 1, section["end_row"])}
    covered = 0
    anchors, expected_anchors = {}, {}
    previous_actual = previous_expected = previous_boxes = None
    for page in adapter.read_style(read_height, read_width):
        if page["start"] != covered or not page["formats"] or len(page["formats"]) > 100:
            raise ValueError("style readback pagination mismatch")
        grid = page["grid"]
        if (grid.get("frozenRowCount", 0) != contract["frozen_rows"]
            or grid.get("frozenColumnCount", 0) != contract["frozen_columns"] or not grid.get("hideGridlines", False)
            or page["widths"][:width] != contract["widths"]):
            raise ValueError("style column width/freeze mismatch")
        merges = [_range_box(item) for item in page["merges"]
                  if item.get("startRowIndex", 0) < read_height and item.get("startColumnIndex", 0) < read_width]
        if sorted(merges) != contract["merges"] or any(item["range"].get("startRowIndex", 0) < read_height
            and item["range"].get("startColumnIndex", 0) < read_width for item in page.get("banding", [])):
            raise ValueError("style merged-range/banding mismatch")
        for offset, row in enumerate(page["formats"]):
            index = page["start"] + offset
            if len(row) != read_width or index >= read_height:
                raise ValueError("style readback dimensions mismatch")
            if contract["role"] in ("unusual", "reversal") and index < height:
                pixels = page["heights"][offset]
                if type(pixels) is not int or pixels < 21 or index == 0 and pixels != 21:
                    raise ValueError("detail row height clips the attested presentation")
            actual_row, expected_row, boxes = [None] * width, [None] * width, [None] * width
            for col, raw in enumerate(row):
                if index >= height or col >= width:
                    if page["entered"][offset][col]:
                        raise ValueError("obsolete tail formatting was not cleared")
                    continue
                expected = _visible_style(matrix, index, col, sections)
                box = next((tuple(area) for area in contract["merges"]
                            if area[0] <= index < area[1] and area[2] <= col < area[3]),
                           (index, index + 1, col, col + 1))
                key = (box[0], box[2])
                if expected is None:
                    expected, actual = expected_anchors[key], anchors[key]
                else:
                    actual = _style_view(raw, page.get("default", {}))
                    if box[1] - box[0] > 1 or box[3] - box[2] > 1:
                        anchors[key], expected_anchors[key] = actual, expected
                if any(actual.get(name) != value for name, value in expected.items() if name != "borders"):
                    raise ValueError(f"{contract['role']} effective style mismatch at row {index + 1}, "
                                     f"column {col + 1}: actual={actual}, expected={expected}")
                actual_row[col], expected_row[col], boxes[col] = actual, expected, box
            if index < height:
                # Inspect every visible grid edge once, with the previous row as
                # a bounded halo. Either neighboring cell may own the same line.
                for col in range(width):
                    actual, expected = actual_row[col]["borders"], expected_row[col]["borders"]
                    edges = []
                    if index == 0 or boxes[col] != previous_boxes[col]:
                        edges.append((_shared_edge(actual["top"], previous_actual[col]["borders"]["bottom"] if index else ("NONE", None)),
                                      _shared_edge(expected["top"], previous_expected[col]["borders"]["bottom"] if index else ("NONE", None))))
                    if col == 0 or boxes[col] != boxes[col - 1]:
                        edges.append((_shared_edge(actual["left"], actual_row[col - 1]["borders"]["right"] if col else ("NONE", None)),
                                      _shared_edge(expected["left"], expected_row[col - 1]["borders"]["right"] if col else ("NONE", None))))
                    if col == width - 1:
                        edges.append((actual["right"], expected["right"]))
                    if index == height - 1:
                        edges.append((actual["bottom"], expected["bottom"]))
                    if any(actual_edge != expected_edge for actual_edge, expected_edge in edges):
                        raise ValueError(f"{contract['role']} effective style border mismatch at row {index + 1}, "
                                         f"column {col + 1}: edges={edges}, actual={actual}, expected={expected}")
                previous_actual, previous_expected, previous_boxes = actual_row, expected_row, boxes
        covered += len(page["formats"])
    if covered != read_height:
        raise ValueError("incomplete style readback")


def validate_output_targets(spreadsheet_id, legacy_spreadsheet_id, accounts, targets):
    if not spreadsheet_id or spreadsheet_id == legacy_spreadsheet_id:
        raise ValueError("v2 workbook must be distinct from v1/legacy workbook")
    if set(targets) != set(accounts):
        raise ValueError("v2 target roster differs from report account roster")
    ids, titles = set(), set()
    for account, config in accounts.items():
        if account != config.account or set(targets[account]) != set(ROLES):
            raise ValueError("each cluster requires exactly four output roles")
        for role, target in targets[account].items():
            target.sheet_target()
            if target.role != role or target.spreadsheet_id != spreadsheet_id:
                raise ValueError("all v2 roles must use the one pinned workbook")
            if target.title != cell_worksheet_title(account, role, target.naming_scheme):
                raise ValueError("worksheet title does not match exact v2 cluster/role")
            if target.worksheet_id in ids or target.title in titles:
                raise ValueError("v2 worksheet IDs/titles must be unique")
            ids.add(target.worksheet_id)
            titles.add(target.title)


def build_cell_cluster_matrices_v2(config, cell_days, through_date, *, summary_manual_values=None,
                                   summary_reset_dates=None, dummy=False,
                                   first_report_date=None, calculate_saldo=True):
    report = build_cluster_report(config, cell_days, through_date,
        first_report_date=first_report_date, calculate_saldo=calculate_saldo)
    matrices = {
        "summary": render_cell_matrix(report, manual_values=summary_manual_values or {},
            reset_dates=summary_reset_dates or set(), dummy=dummy, presentation_version=5),
        "invoice": build_cell_invoice_matrix(config, cell_days, dummy=dummy),
        "unusual": build_cell_detail_history(config, cell_days, "unusual"),
        "reversal": build_cell_detail_history(config, cell_days, "reversal"),
    }
    for role, matrix in matrices.items():
        if "values" not in matrix:
            matrix["values"] = matrix["rows"]
        values = matrix["values"]
        if not values or not values[0] or any(len(row) != len(values[0]) for row in values):
            raise ValueError("v2 renderer must return a nonempty rectangular matrix")
        json.dumps(values, allow_nan=False)
        bind_cell_style_contract(role, matrix)
    return matrices


def _sheet_date(value):
    if not isinstance(value, str):
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _date_groups(role, matrix):
    if role in ("summary", "invoice") and matrix.get("date_ranges"):
        return {item["report_date"]: (item["start_row"] - 1,
                item["end_row"] - item["start_row"] + 1) for item in matrix["date_ranges"]}
    values = matrix["values"]
    first_data_row = 3 if role == "summary" else 2 if role == "invoice" else 1
    groups = {}
    for index, row in enumerate(values[first_data_row:], first_data_row):
        day = _sheet_date(row[0]) if row else None
        if day:
            if day in groups and groups[day][0] + groups[day][1] == index:
                start, count = groups[day]
                groups[day] = (start, count + 1)
            else:
                groups[day] = (index, 1)
    return groups


def _shift_formula_rows(value, boundary, delta):
    if not isinstance(value, str) or not value.startswith("=") or not delta:
        return value
    return re.sub(
        r"(?<![A-Za-z0-9_])(\$?[A-Z]{1,3})(\$?)(\d+)(?!\s*\()",
        lambda match: match[1] + match[2] + str(
            int(match[3]) + delta if int(match[3]) > boundary else int(match[3])
        ),
        value,
    )


def _row_operation(old_range, new_count, insertion_index):
    if old_range:
        old_start, old_count = old_range
        difference = new_count - old_count
        if difference > 0:
            return {"insert_row": old_start + old_count, "insert_count": difference}
        if difference < 0:
            return {"delete_row": old_start + new_count, "delete_count": -difference}
        return {}
    return ({"insert_row": insertion_index, "insert_count": new_count}
            if new_count else {})


def mark_cell_date_saldo_unverified(matrix, report_date):
    ranges = [item for item in matrix.get("date_ranges", [])
              if item["report_date"] == report_date]
    if len(ranges) != 1:
        raise ValueError("daily Summary must contain exactly one report-date block")
    start = ranges[0]["start_row"] - 1
    width = len(matrix["values"][0])
    status = [report_date, "BALANCE", "CUMULATIVE SALDO UNVERIFIED"] + [""] * (width - 3)
    matrix["values"].insert(start, status)
    matrix["numeric_preview"].insert(start, status[:])
    matrix["row_keys"].insert(start, "BALANCE_STATUS")
    matrix["row_stripes"].insert(start, "detail" if start % 2 else "white")
    ranges[0]["end_row"] += 1
    for row_index in range(start + 1, len(matrix["values"])):
        matrix["values"][row_index] = [
            _shift_formula_rows(value, start, 1) for value in matrix["values"][row_index]
        ]
    for entry in matrix.get("editable_inputs", []):
        if entry["row"] > start:
            entry["row"] += 1
        entry["default"] = _shift_formula_rows(entry["default"], start, 1)
    matrix["report_fingerprint"] = fingerprint(matrix["values"])
    return bind_cell_style_contract("summary", matrix)


def preserve_cell_date_saldo_chain(prior, replacement, report_date):
    old_ranges = [item for item in prior.get("date_ranges", [])
                  if item["report_date"] == report_date]
    if not old_ranges:
        return False
    old_start = old_ranges[0]["start_row"] - 1
    old_end = old_ranges[0]["end_row"]
    new_range = next((item for item in replacement["date_ranges"]
                      if item["report_date"] == report_date), None)
    if new_range is None:
        raise ValueError("replacement Summary lacks its existing date block")
    new_start, new_end = new_range["start_row"] - 1, new_range["end_row"]
    saldo_columns = [block["end_column"] - 1 for block in replacement["blocks"]]
    has_chain = any(isinstance(prior["values"][row][column], str)
                    and prior["values"][row][column].startswith("=")
                    for row in range(old_start, old_end) for column in saldo_columns)
    if not has_chain:
        return False
    old_keys = prior["row_keys"][old_start:old_end]
    new_keys = replacement["row_keys"][new_start:new_end]
    if old_keys != new_keys:
        raise ValueError("existing Summary SALDO chain cannot be aligned to the replacement block")
    # These formulas intentionally keep the pre-existing cross-date dependency chain alive.
    for offset in range(old_end - old_start):
        for column in saldo_columns:
            replacement["values"][new_start + offset][column] = prior["values"][old_start + offset][column]
            replacement["numeric_preview"][new_start + offset][column] = prior["numeric_preview"][old_start + offset][column]
    replacement["report_fingerprint"] = fingerprint(replacement["values"])
    return True


def _merge_summary_date(prior, replacement, report_date, preserve_saldo_chain):
    old_groups = _date_groups("summary", prior) if prior else {}
    new_groups = _date_groups("summary", replacement)
    if set(new_groups) != {report_date}:
        raise ValueError("date-scoped Summary replacement must contain exactly one date")
    old_range, new_range = old_groups.get(report_date), new_groups[report_date]
    old_count = old_range[1] if old_range else 0
    new_count = new_range[1]
    if old_range:
        boundary = old_range[0] + old_count
        delta = new_count - old_count
        insertion_index = boundary
    else:
        insertion_index = next((start for day, (start, _) in sorted(old_groups.items())
                                if day > report_date), len(prior["values"]) if prior else 3)
        boundary, delta = insertion_index, new_count
    base = prior or replacement
    values, preview = [row[:] for row in base["values"][:3]], [row[:] for row in base["numeric_preview"][:3]]
    row_keys, ranges, inputs = base["row_keys"][:3], [], []
    saldo_columns = {block["end_column"] - 1 for block in replacement["blocks"]}
    base_stripes = base.get("row_stripes", [
        "detail" if index % 2 else "white" for index in range(len(base["values"]))
    ])
    stripes = base_stripes[:3]
    for day in sorted(set(old_groups) | {report_date}):
        source = replacement if day == report_date else prior
        source_start, count = (new_groups if day == report_date else old_groups)[day]
        start = len(values)
        for row in source["values"][source_start:source_start + count]:
            value = []
            for column, item in enumerate(row):
                if day == report_date and preserve_saldo_chain and column in saldo_columns:
                    value.append(item)
                else:
                    delta_rows = start - source_start if day == report_date else 0
                    value.append(_shift_formula_rows(item, 0, delta_rows) if day == report_date
                                 else _shift_formula_rows(item, boundary, delta))
            values.append(value)
        preview.extend(row[:] for row in source["numeric_preview"][source_start:source_start + count])
        row_keys.extend(source["row_keys"][source_start:source_start + count])
        source_stripes = source.get("row_stripes", [
            "detail" if index % 2 else "white" for index in range(len(source["values"]))
        ])
        stripes.extend(("detail" if index % 2 else "white")
                       for index in range(start, start + count) if day == report_date)
        if day != report_date:
            stripes.extend(source_stripes[source_start:source_start + count])
        ranges.append({"report_date": day, "start_row": start + 1, "end_row": len(values)})
        source_first_row = source_start + 1
        for entry in source.get("editable_inputs", []):
            if entry["report_date"] != day:
                continue
            inputs.append({**entry, "row": start + entry["row"] - source_first_row + 1,
                "default": (_shift_formula_rows(entry["default"], 0, start - source_start)
                            if day == report_date else
                            _shift_formula_rows(entry["default"], boundary, delta))})
    matrix = {
        **base, "values": values, "numeric_preview": preview, "row_keys": row_keys,
        "date_ranges": ranges, "editable_inputs": inputs, "row_stripes": stripes,
        "manual_reset_audit": [],
        "report_fingerprint": fingerprint(values),
    }
    target = next(item for item in ranges if item["report_date"] == report_date)
    patch = {"start_row": target["start_row"] - 1, "row_count": new_count}
    patch.update(_row_operation(old_range, new_count, insertion_index))
    if prior is None:
        patch.update({"start_row": 0, "row_count": len(values), "initial": True})
        patch.pop("insert_row", None)
        patch.pop("insert_count", None)
        patch.pop("delete_row", None)
        patch.pop("delete_count", None)
    return bind_cell_style_contract("summary", matrix), patch


def _merge_invoice_date(prior, replacement, report_date):
    old_groups = _date_groups("invoice", prior) if prior else {}
    new_groups = _date_groups("invoice", replacement)
    if set(new_groups) != {report_date}:
        raise ValueError("date-scoped invoice replacement must contain exactly one date")
    old_range, new_range = old_groups.get(report_date), new_groups[report_date]
    new_count = new_range[1]
    if old_range:
        insertion_index = old_range[0] + old_range[1]
    elif old_groups:
        next_start = next((start for day, (start, _) in sorted(old_groups.items())
                           if day > report_date), None)
        insertion_index = next_start if next_start is not None else len(prior["values"])
    else:
        insertion_index = 2
    base = prior or replacement
    values, sections = [row[:] for row in base["values"][:2]], list(base.get("row_sections", [None, None])[:2])
    dates, ranges, section_ranges, subtitles, children = sorted(set(old_groups) | {report_date}), [], [], [], []
    pending_separators = []
    for date_index, day in enumerate(dates):
        if date_index:
            pending_separators.append((len(values), len(values) + 2))
            values.extend([[""] * len(values[0]) for _ in range(2)])
            sections.extend([None, None])
        source = replacement if day == report_date else prior
        source_start, count = (new_groups if day == report_date else old_groups)[day]
        start = len(values)
        values.extend(row[:] for row in source["values"][source_start:source_start + count])
        source_sections = source.get("row_sections", [None] * len(source["values"]))
        sections.extend(source_sections[source_start:source_start + count])
        ranges.append({"report_date": day, "start_row": start + 1, "end_row": len(values)})
        for area in source.get("section_ranges", []):
            if area.get("report_date") == day:
                section_ranges.append({**area,
                    "start_row": start + area["start_row"] - source_start,
                    "end_row": start + area["end_row"] - source_start})
        for key, output in (("qris_subtitle_rows", subtitles), ("qris_child_rows", children)):
            output.extend(start + row - source_start for row in source.get(key, [])
                          if source_start < row <= source_start + count)
    old_count = old_range[1] if old_range else 0
    if old_range:
        difference = new_count - old_count
        patch = ({"insert_row": insertion_index, "insert_count": difference}
                 if difference > 0 else
                 {"delete_row": old_range[0] + new_count, "delete_count": -difference}
                 if difference < 0 else {})
        spacer_ranges = []
    elif old_groups and new_count:
        next_start = next((start for day, (start, _) in sorted(old_groups.items())
                           if day > report_date), None)
        if next_start is None:
            patch = {"insert_row": insertion_index, "insert_count": new_count + 2}
            spacer_ranges = [(insertion_index, insertion_index + 2)]
        else:
            patch = {"insert_row": insertion_index, "insert_count": new_count + 2}
            spacer_ranges = [(insertion_index + new_count, insertion_index + new_count + 2)]
    elif not old_groups and new_count:
        patch, spacer_ranges = {}, []
    else:
        patch, spacer_ranges = {}, []
    matrix = {
        **base, "values": values, "row_sections": sections, "dates": dates,
        "date_ranges": ranges, "section_ranges": section_ranges,
        "qris_subtitle_rows": subtitles, "qris_child_rows": children,
        "fingerprint": fingerprint(values),
    }
    target = next(item for item in ranges if item["report_date"] == report_date)
    patch.update({"start_row": target["start_row"] - 1, "row_count": new_count,
                  "spacer_ranges": spacer_ranges})
    if old_range is None and old_groups and report_date > max(old_groups):
        patch["start_row"] = insertion_index + 2
    if prior is None:
        patch.update({"start_row": 0, "row_count": len(values), "spacer_ranges": [], "initial": True})
        patch.pop("insert_row", None)
        patch.pop("insert_count", None)
        patch.pop("delete_row", None)
        patch.pop("delete_count", None)
    return bind_cell_style_contract("invoice", matrix), patch


def _merge_detail_date(role, prior, replacement, report_date):
    old_groups = _date_groups(role, prior) if prior else {}
    new_groups = _date_groups(role, replacement)
    old_range, new_range = old_groups.get(report_date), new_groups.get(report_date)
    new_count = new_range[1] if new_range else 0
    insertion_index = old_range[0] + old_range[1] if old_range else (
        next((start for day, (start, _) in sorted(old_groups.items()) if day > report_date),
             len(prior["values"]) if prior else 1))
    base = prior or replacement
    values = [row[:] for row in base["values"][:1]]
    base_stripes = base.get("row_stripes", [
        "white" if index % 2 else "stripe" for index in range(len(base["values"]))
    ])
    stripes = base_stripes[:1]
    ranges, separators = [], []
    for day in sorted((set(old_groups) - {report_date}) | ({report_date} if new_range else set())):
        source = replacement if day == report_date else prior
        source_start, count = (new_groups if day == report_date else old_groups)[day]
        start = len(values)
        values.extend(row[:] for row in source["values"][source_start:source_start + count])
        source_stripes = source.get("row_stripes", [
            "white" if index % 2 else "stripe" for index in range(len(source["values"]))
        ])
        stripes.extend(("white" if index % 2 else "stripe")
                       for index in range(start, start + count) if day == report_date)
        if day != report_date:
            stripes.extend(source_stripes[source_start:source_start + count])
        ranges.append({"report_date": day, "start_row": start + 1, "end_row": len(values)})
        separators.append(start + 1)
    matrix = {**base, "values": values, "rows": values, "date_ranges": ranges,
        "dates": [item["report_date"] for item in ranges], "day_separator_rows": separators,
        "row_stripes": stripes, "row_count": len(values) - 1, "empty": len(values) == 1}
    patch = _row_operation(old_range, new_count, insertion_index)
    target = next((item for item in ranges if item["report_date"] == report_date), None)
    patch.update({"start_row": target["start_row"] - 1 if target else insertion_index,
                  "row_count": new_count})
    if prior is None:
        patch.update({"start_row": 0, "row_count": len(values), "initial": True})
        patch.pop("insert_row", None)
        patch.pop("insert_count", None)
        patch.pop("delete_row", None)
        patch.pop("delete_count", None)
    return bind_cell_style_contract(role, matrix), patch


def merge_cell_date_matrix(role, prior, replacement, report_date, *, preserve_saldo_chain=False):
    """Replace one date's saved matrix while retaining every other date block."""
    if role == "summary":
        return _merge_summary_date(prior, replacement, report_date, preserve_saldo_chain)
    if role == "invoice":
        return _merge_invoice_date(prior, replacement, report_date)
    if role in ("unusual", "reversal"):
        return _merge_detail_date(role, prior, replacement, report_date)
    raise ValueError("unknown cell worksheet role")


def _date_format_requests(role, target, matrix, patch):
    start, end = patch["start_row"], patch["start_row"] + patch["row_count"]
    intervals = [(start, end), *patch.get("spacer_ranges", [])]
    requests = (_format_requests(target.sheet_target(), matrix) if role == "summary" else
        invoice_format_requests(target.worksheet_id, matrix) if role == "invoice" else
        detail_format_requests(target.worksheet_id, matrix))
    if patch.get("initial"):
        return requests
    result = []
    for request in requests:
        operation = next(iter(request))
        if operation == "setBasicFilter" and role in ("invoice", "unusual", "reversal"):
            result.append(copy.deepcopy(request))
            continue
        for interval_start, interval_end in intervals:
            clipped = copy.deepcopy(request)
            payload = clipped[operation]
            if operation in ("repeatCell", "updateBorders"):
                area = payload["range"]
                first = max(interval_start, area["startRowIndex"])
                last = min(interval_end, area["endRowIndex"])
                if first >= last:
                    continue
                area["startRowIndex"], area["endRowIndex"] = first, last
                if operation == "updateBorders" and (interval_start, interval_end) in patch.get("spacer_ranges", []):
                    payload.pop("top", None)
                    payload.pop("bottom", None)
            elif operation == "updateDimensionProperties":
                area = payload["range"]
                if area["dimension"] != "ROWS":
                    continue
                first = max(interval_start, area["startIndex"])
                last = min(interval_end, area["endIndex"])
                if first >= last:
                    continue
                area["startIndex"], area["endIndex"] = first, last
            elif operation == "autoResizeDimensions":
                area = payload["dimensions"]
                if area["dimension"] != "ROWS":
                    continue
                first = max(interval_start, area["startIndex"])
                last = min(interval_end, area["endIndex"])
                if first >= last:
                    continue
                area["startIndex"], area["endIndex"] = first, last
            else:
                continue
            result.append(clipped)
    return result


def _source_calculation_history(rows):
    source_ids = {}
    for row in rows:
        value = row["report_date"]
        day = value.isoformat() if hasattr(value, "isoformat") else str(value)
        source_ids.setdefault(day, []).append(row["calculation_id"])
    return {day: fingerprint(sorted(ids)) for day, ids in source_ids.items()}


def _validate_deleted_row_tails(saved, adapters, metadata, call):
    for role in ROLES:
        patch = saved[role].get("patch", {})
        delete_count = patch.get("delete_count", 0)
        if not delete_count:
            continue
        grid = metadata[role].get("properties", {}).get("gridProperties", {})
        columns = grid.get("columnCount")
        width = len(saved[role]["values"][0])
        if type(columns) is not int or columns < width:
            raise ValueError("cannot verify the worksheet width before deleting rows")
        if columns > 100:
            raise ValueError("cannot safely inspect off-matrix cells before deleting rows")
        if columns == width:
            continue
        start = patch["delete_row"]
        end = start + delete_count
        reader = getattr(adapters[role], "read_range", None)
        values = (call(reader, start, end, columns) if reader else
                  call(adapters[role].read, end, columns)[start:end])
        rows = _padded(values, delete_count, columns)
        if any(value not in ("", None) for row in rows for value in row[width:]):
            raise ValueError("row deletion would remove unmanaged cells outside the saved matrix width")


def _restore_prior_guards(prior, targets, adapters, guarded, guards, previous, send, call):
    restoration = []
    for role in ROLES:
        restoration.extend(_protection_requests(
            targets[role].sheet_target(), prior[role]["matrix"], guarded[role], guards[role],
        ))
    send(restoration)
    expected = {"status": "COMPLETE", "epoch": previous["epoch"],
                "fingerprint": previous["fingerprint"]}
    for role in ROLES:
        meta = call(adapters[role].metadata)
        _verify_final_protections(targets[role].sheet_target(), prior[role]["matrix"], meta)
        _verify_marker(meta, expected)


def _write_date_snapshot(saved, targets, adapters, store, epoch, mutate, send_formatting,
                         report, rows_per_request):
    for role in ROLES:
        target, item = targets[role], saved[role]
        patch, values = item.get("patch"), item["values"]
        if not isinstance(patch, dict):
            raise ValueError("date-scoped publication snapshot lacks its row patch")
        for kind in ("insert", "delete"):
            count = patch.get(kind + "_count", 0)
            if not count:
                continue
            start = patch[kind + "_row"]
            dimension_range = {"sheetId": target.worksheet_id, "dimension": "ROWS",
                               "startIndex": start, "endIndex": start + count}
            if kind == "insert":
                request = {"insertDimension": {"range": dimension_range,
                                                "inheritFromBefore": start > 0}}
            else:
                request = {"deleteDimension": {"range": dimension_range}}
            mutate(adapters[role], [request])
            store.confirm_chunk(epoch, f"{role}-rows-{kind}")
        start = patch["start_row"]
        rows = values[start:start + patch["row_count"]]
        requests = matrix_write_requests(rows, target.worksheet_id, start_row=start,
            rows_per_request=rows_per_request, allow_formulas=role == "summary")
        report("worksheet_write_started", role=role, completed=0, total=len(requests))
        for index, request in enumerate(requests):
            mutate(adapters[role], [request])
            store.confirm_chunk(epoch, f"{role}-date-rows-{index}")
            report("worksheet_chunk_confirmed", role=role,
                   completed=index + 1, total=len(requests))
    formatting = []
    for role in ROLES:
        formatting.extend(_date_format_requests(
            role, targets[role], saved[role]["matrix"], saved[role]["patch"],
        ))
    send_formatting(formatting, "date-format")


def _write_full_snapshot(saved, prior, targets, adapters, guarded, store, epoch, mutate,
                         send_formatting, report, rows_per_request):
    cleanup = []
    for role in ROLES:
        values = saved[role]["values"]
        old = prior[role]["values"] if prior else values
        height, width = max(len(values), len(old)), max(len(values[0]), len(old[0]))
        sheet_id = targets[role].worksheet_id
        area = {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": height,
                "startColumnIndex": 0, "endColumnIndex": width}
        for merge in guarded[role].get("merges", []):
            if merge.get("startRowIndex", 0) < height and merge.get("startColumnIndex", 0) < width:
                if merge.get("endRowIndex", height + 1) > height or merge.get("endColumnIndex", width + 1) > width:
                    raise ValueError("unmanaged merge crosses the governed migration footprint")
                cleanup.append({"unmergeCells": {"range": merge}})
        for band in guarded[role].get("bandedRanges", []):
            bound = band["range"]
            if bound.get("startRowIndex", 0) < height and bound.get("startColumnIndex", 0) < width:
                if bound.get("endRowIndex", height + 1) > height or bound.get("endColumnIndex", width + 1) > width:
                    raise ValueError("unmanaged banding crosses the governed migration footprint")
                cleanup.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
        cleanup.extend([
            {"repeatCell": {"range": area, "cell": {"userEnteredFormat": {}}, "fields": "userEnteredFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                "startIndex": 0, "endIndex": height}, "properties": {"pixelSize": 21}, "fields": "pixelSize"}},
        ])
        if width > len(values[0]):
            cleanup.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                "startIndex": len(values[0]), "endIndex": width}, "properties": {"pixelSize": 100}, "fields": "pixelSize"}})
    send_formatting(cleanup, "cleanup")
    for role in ROLES:
        target, values = targets[role], saved[role]["values"]
        requests = matrix_write_requests(values, target.worksheet_id,
            rows_per_request=rows_per_request, allow_formulas=role == "summary")
        report("worksheet_write_started", role=role, completed=0, total=len(requests))
        for index, request in enumerate(requests):
            mutate(adapters[role], [request])
            store.confirm_chunk(epoch, f"{role}-rows-{index}")
            report("worksheet_chunk_confirmed", role=role,
                   completed=index + 1, total=len(requests))
        if prior:
            old = prior[role]["values"]
            if len(old) > len(values):
                mutate(adapters[role], [{"updateCells": {"range": {
                    "sheetId": target.worksheet_id, "startRowIndex": len(values), "endRowIndex": len(old),
                    "startColumnIndex": 0, "endColumnIndex": len(old[0])},
                    "rows": [], "fields": "userEnteredValue,userEnteredFormat"}}])
            if len(old[0]) > len(values[0]):
                mutate(adapters[role], [{"updateCells": {"range": {
                    "sheetId": target.worksheet_id, "startRowIndex": 0,
                    "endRowIndex": max(len(old), len(values)),
                    "startColumnIndex": len(values[0]), "endColumnIndex": len(old[0])},
                    "rows": [], "fields": "userEnteredValue,userEnteredFormat"}}])
    formatting = []
    for role in ROLES:
        target, matrix = targets[role], saved[role]["matrix"]
        formatting.extend(_format_requests(target.sheet_target(), matrix) if role == "summary" else
            invoice_format_requests(target.worksheet_id, matrix) if role == "invoice" else
            detail_format_requests(target.worksheet_id, matrix))
    send_formatting(formatting)
    report("formatting_applied")


def _read_baseline(adapters, prior):
    manual = {}
    for role, saved in prior.items():
        _verify_style(adapters[role], saved["matrix"], allow_unattested=True)
        expected = copy.deepcopy(saved["values"])
        actual = _padded(adapters[role].read(len(expected), len(expected[0])), len(expected), len(expected[0]))
        if role == "summary":
            manual = _manual_values(saved["matrix"], actual)
            for entry in saved["matrix"]["editable_inputs"]:
                key = (entry["report_date"], entry["cell_code"])
                if key in manual:
                    expected[entry["row"] - 1][entry["column"] - 1] = manual[key]
        if actual != expected:
            raise ValueError(f"v2 {role} tab differs from its committed publication")
    return manual


def _require_empty(adapter, metadata):
    grid = metadata["properties"].get("gridProperties", {})
    rows, cols = grid.get("rowCount", 100), grid.get("columnCount", 100)
    if rows * cols > 5_000_000:
        raise ValueError("initial empty worksheet preflight exceeds bounded grid")
    if any(value not in ("", None) for row in adapter.read(rows, cols) for value in row):
        raise ValueError("unpublished v2 target is not empty")


def preflight_unpublished_v2_sheet(adapter, expected_title, metadata=None):
    """Check an existing fresh target before any missing tabs are added."""
    metadata = adapter.metadata() if metadata is None else metadata
    properties = metadata["properties"]
    if properties["sheetId"] != adapter.worksheet_id:
        raise ValueError("v2 worksheet identity mismatch")
    if properties.get("title") != expected_title:
        raise ValueError("worksheet title does not match exact v2 cluster/role")
    protections = metadata.get("protectedRanges", [])
    if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
        raise RuntimeError("unresolved v2 refresh guard")
    if any(not item.get("description", "").startswith(MANAGED_PREFIX) for item in protections):
        raise ValueError("v2 target contains unmanaged protection")
    if _marker_entries(metadata) or protections:
        raise ValueError("unpublished v2 sheet has unmanaged generation/protection state")
    _require_empty(adapter, metadata)


def _verify_values(adapter, saved, previous=None):
    values = saved["values"]
    old = previous["values"] if previous else values
    height, width = max(len(values), len(old)), max(len(values[0]), len(old[0]))
    expected = _padded(values, height, width)
    if _padded(adapter.read(height, width), height, width) != expected:
        raise ValueError("v2 value/formula/tail readback mismatch")


def preflight_cell_cluster_v2(store, spreadsheet_id, targets, adapters, cluster_id, *, allow_legacy_migration=False):
    """Read-only identity, prior-epoch and content checks before tab mutation."""
    if set(targets) != set(ROLES) or set(adapters) != set(ROLES):
        raise ValueError("v2 preflight requires exactly four output roles")
    if type(allow_legacy_migration) is not bool:
        raise ValueError("explicit presentation-migration mode required")
    if store.cluster_id != cluster_id or store.target_id != spreadsheet_id:
        raise ValueError("v2 database lock scope mismatch")
    if len({target.worksheet_id for target in targets.values()}) != len(ROLES):
        raise ValueError("v2 worksheet IDs must be unique")
    publication = store.latest_publication(sheet_only=True)
    prior = None
    if publication:
        if publication["status"] != "COMPLETE":
            raise RuntimeError("v2 target has unresolved publication state")
        snapshot = publication["snapshot"]
        prior = snapshot.get("worksheets")
        if (snapshot.get("schema_version"), snapshot.get("spreadsheet_id")) != (2, spreadsheet_id):
            raise ValueError("database target has an incompatible prior publication")
        if set(prior or {}) != set(ROLES):
            raise ValueError("prior v2 publication must cover all four outputs")
        if not allow_legacy_migration and any(item["matrix"].get("style_contract") is None for item in prior.values()):
            raise ValueError("legacy layout requires an approved configuration-only presentation migration")
    for role in ROLES:
        target, adapter = targets[role], adapters[role]
        target.sheet_target()
        if target.role != role or target.spreadsheet_id != spreadsheet_id:
            raise ValueError("v2 output role/workbook mismatch")
        if adapter.spreadsheet_id != spreadsheet_id or adapter.worksheet_id != target.worksheet_id:
            raise ValueError("opened worksheet/workbook differs from approved IDs")
        meta = adapter.metadata()
        if meta["properties"]["sheetId"] != target.worksheet_id:
            raise ValueError("v2 worksheet identity mismatch")
        if target.title != cell_worksheet_title(cluster_id + "_B", role, target.naming_scheme) or meta["properties"].get("title") != target.title:
            raise ValueError("worksheet title does not match exact v2 cluster/role")
        protections = meta.get("protectedRanges", [])
        if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
            raise RuntimeError("unresolved v2 refresh guard")
        if any(not item.get("description", "").startswith(MANAGED_PREFIX) for item in protections):
            raise ValueError("v2 target contains unmanaged protection")
        if prior:
            prior_identity = (prior[role]["worksheet_id"], prior[role]["title"])
            current_identity = (target.worksheet_id, target.title)
            account = cluster_id + "_B"
            alias_only_rename = (
                prior_identity[0] == target.worksheet_id
                and prior_identity[1] == cell_worksheet_title(account, role, "code")
                and target.naming_scheme == "cluster_alias"
                and target.title == cell_worksheet_title(account, role, "cluster_alias")
            )
            if prior_identity != current_identity and not alias_only_rename:
                raise ValueError("v2 target set changed from prior committed snapshot")
            _verify_marker(meta, {"status": "COMPLETE", "epoch": publication["epoch"],
                                  "fingerprint": publication["fingerprint"]})
            _verify_final_protections(target.sheet_target(), prior[role]["matrix"], meta)
            _verify_style(adapter, prior[role]["matrix"], allow_unattested=True)
        else:
            preflight_unpublished_v2_sheet(adapter, target.title, meta)
    if prior:
        _read_baseline(adapters, prior)
    return publication


def validate_refresh_request_id(refresh_request_id):
    if refresh_request_id is not None and (
        not isinstance(refresh_request_id, str)
        or not 1 <= len(refresh_request_id) <= 128
        or re.fullmatch(r"[A-Za-z0-9_-]+", refresh_request_id) is None
    ):
        raise ValueError("refresh request ID must be 1-128 safe ASCII characters")


def publish_cell_cluster_v2(store, spreadsheet_id, targets, adapters, batch, config, *,
                            changed_dates=None, additional_batches=None, configuration_only=False,
                            rows_per_request=200, publication_only=False,
                            preserve_manual_inputs=False, date_scoped=False, report_date=None,
                            refresh_request_id: str | None = None):
    """Publish four tabs; an optional refresh ID makes same-execution retries write-free."""
    from psycopg import InterfaceError, OperationalError
    validate_refresh_request_id(refresh_request_id)
    if type(rows_per_request) is not int or rows_per_request < 1:
        raise ValueError("positive v2 write chunk size required")
    if any(type(value) is not bool for value in (
        configuration_only, publication_only, preserve_manual_inputs, date_scoped,
    )):
        raise ValueError("publication mode flags must be explicit booleans")
    if date_scoped:
        if not publication_only or report_date is None:
            raise ValueError("date-scoped publication requires a persisted date handoff")
        submitted_date = business_date(report_date).isoformat()
    elif report_date is not None:
        raise ValueError("report_date is only valid for a date-scoped publication")
    if refresh_request_id is not None and not publication_only:
        raise ValueError("refresh request ID requires committed publication-only history")
    if publication_only:
        if batch is not None or changed_dates is not None or additional_batches or configuration_only:
            raise ValueError("publication-only mode cannot import or recalculate source batches")
        submitted = submitted_date if date_scoped else store.latest_report_date()
        if not submitted:
            raise ValueError("publication-only mode requires committed cell history")
        additional_batches = []
    else:
        validate_cluster_batch(batch, config.cells)
        if changed_dates != [batch[0]["report_date"]]:
            raise ValueError("publish one complete changed cluster date at a time")
        submitted = changed_dates[0]
    if configuration_only:
        selected = store.current_results(submitted, submitted)
        expected = {(item["identity"]["cell_code"], item["generation_id"]) for item in batch}
        actual = {(item["cell_code"], item["generation_id"]) for item in selected}
        if len(selected) != len(batch) or expected != actual:
            raise ValueError("configuration-only operation cannot import new source generations")
    if additional_batches is None:
        through = max(submitted, store.latest_report_date() or submitted)
        additional_batches = recalculate_stale_history(store, config, through, submitted_dates={submitted})
    for cohort in additional_batches:
        validate_cluster_batch(cohort, config.cells)

    mutation_uncertain, ownership_lost = False, False

    def alive():
        nonlocal ownership_lost
        try:
            store.assert_alive()
        except Exception:
            ownership_lost = True
            raise

    def call(method, *args):
        alive()
        result = method(*args)
        alive()
        return result

    def mutate(adapter, requests):
        nonlocal mutation_uncertain
        alive()
        mutation_uncertain = True
        result = adapter.batch_update(requests)
        mutation_uncertain = False
        alive()
        return result

    def send(requests):
        if len(json.dumps({"requests": requests}).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("v2 workbook batch exceeds bounded request size")
        return mutate(adapters["summary"], requests)

    def send_formatting(requests, prefix="format"):
        chunk, size, index = [], 16, 0
        for request in requests:
            length = len(json.dumps(request).encode("utf-8")) + 2
            if length + 16 > MAX_REQUEST_BYTES:
                raise ValueError("one formatting request exceeds bounded request size")
            if size + length > MAX_REQUEST_BYTES:
                send(chunk)
                store.confirm_chunk(staged["epoch"], f"{prefix}-{index}")
                chunk, size, index = [], 16, index + 1
            chunk.append(request)
            size += length
        if chunk:
            send(chunk)
            store.confirm_chunk(staged["epoch"], f"{prefix}-{index}")

    previous = preflight_cell_cluster_v2(store, spreadsheet_id, targets, adapters, config.account[:-2],
                                         allow_legacy_migration=configuration_only)
    if refresh_request_id is not None:
        used = store.publications_for_refresh_request(refresh_request_id)
        if used:
            latest = store.latest_publication()
            publication = used[0] if len(used) == 1 else None
            if (publication is None or latest is None or previous is None
                or publication["epoch"] != latest["epoch"]
                or publication["epoch"] != previous["epoch"]
                or publication["status"] != "COMPLETE"
                or latest["status"] != "COMPLETE"):
                raise ValueError("refresh request ID is not associated with the latest COMPLETE publication")
            through = submitted if date_scoped else max(submitted, store.latest_report_date() or submitted)
            current_history = _source_calculation_history(
                store.current_results(submitted, submitted) if date_scoped else
                store.current_results(config.first_report_date, through),
            )
            if current_history != publication["snapshot"].get("source_calculations"):
                raise ValueError("refresh request ID cannot be reused with changed source-calculation history")
            for role in ROLES:
                saved = publication["snapshot"]["worksheets"][role]["values"]
                grid = call(adapters[role].metadata)["properties"].get("gridProperties", {})
                rows, columns = grid.get("rowCount"), grid.get("columnCount")
                if (type(rows) is not int or type(columns) is not int
                        or rows < len(saved) or columns < len(saved[0])
                        or rows > 100_000 or columns > 100 or rows * columns > 5_000_000):
                    raise ValueError("v2 retry readback exceeds bounded grid")
                actual = call(adapters[role].read, rows, columns)
                if (not isinstance(actual, list) or len(actual) > rows
                        or any(not isinstance(row, list) or len(row) > columns for row in actual)):
                    raise ValueError("v2 retry readback has invalid grid dimensions")
                if any(value not in ("", None) for row_index, row in enumerate(actual)
                       for column_index, value in enumerate(row)
                       if row_index >= len(saved) or column_index >= len(saved[0])):
                    raise ValueError("v2 retry found values outside the committed report")
            return {"epoch": publication["epoch"], "fingerprint": publication["fingerprint"],
                    "status": "COMPLETE", "roles": list(ROLES)}
    prior = previous["snapshot"]["worksheets"] if previous else None
    metadata = {role: call(adapters[role].metadata) for role in ROLES}
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    staged, write_started, guarded, guards = None, False, None, {}
    try:
        requests, reply_indexes = [], {}
        for role in ROLES:
            target = targets[role]
            protections = metadata[role].get("protectedRanges", [])
            computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
            guard = {"description": GUARD_PREFIX + operation_id, "warningOnly": False,
                     "editors": {"users": [target.writer_email]}, "unprotectedRanges": []}
            if computed:
                guards[role] = computed[0]["protectedRangeId"]
                requests.append({"updateProtectedRange": {"protectedRange": {
                    "protectedRangeId": guards[role], **guard},
                    "fields": "description,warningOnly,editors,unprotectedRanges"}})
            else:
                reply_indexes[role] = len(requests)
                requests.append({"addProtectedRange": {"protectedRange": {
                    "range": {"sheetId": target.worksheet_id}, **guard}}})
            requests.extend({"updateProtectedRange": {"protectedRange": {
                "protectedRangeId": item["protectedRangeId"], "warningOnly": False,
                "editors": {"users": [target.writer_email]}}, "fields": "warningOnly,editors"}}
                for item in protections if item.get("description") == MANAGED_PREFIX + "BANK INPUT")
        replies = send(requests)
        for role, index in reply_indexes.items():
            guards[role] = replies["replies"][index]["addProtectedRange"]["protectedRange"]["protectedRangeId"]
        guarded = {role: call(adapters[role].metadata) for role in ROLES}
        for role in ROLES:
            target, meta = targets[role], guarded[role]
            if not any(item.get("protectedRangeId") == guards[role]
                and item.get("range") == {"sheetId": target.worksheet_id}
                and item.get("description") == GUARD_PREFIX + operation_id
                and item.get("warningOnly", False) is False and not item.get("unprotectedRanges")
                and _editors_match(item.get("editors", {}), [target.writer_email], meta.get("implicitOwnerEmails", []))
                for item in meta.get("protectedRanges", [])):
                raise ValueError(f"v2 {role} temporary whole-sheet guard not confirmed")
            if any(item.get("unprotectedRanges") or not _editors_match(item.get("editors", {}),
                [target.writer_email], meta.get("implicitOwnerEmails", []))
                for item in meta.get("protectedRanges", []) if item.get("description") == MANAGED_PREFIX + "BANK INPUT"):
                raise ValueError("v2 refresh did not lock all MANDIRI inputs")
        # Read manual inputs only after every tab's protection is confirmed.
        if prior is None:
            for role in ROLES:
                call(_require_empty, adapters[role], guarded[role])
        manual = _read_baseline(adapters, prior) if prior else {}
        audits = []

        def render_committed(session):
            if date_scoped:
                rows = session.current_results(submitted, submitted)
                days = [{"identity": {cell.cell_code: cell.to_dict() for cell in config.cells}[row["cell_code"]],
                         "report_date": row["report_date"].isoformat() if hasattr(row["report_date"], "isoformat") else str(row["report_date"]),
                         "generation_id": row["generation_id"], "calculation_id": row["calculation_id"],
                         "config_id": row["config_id"], "rules_version": config.rules_version,
                         "calculation": row["result"]}
                        for row in rows]
                if len(days) != len(config.cells):
                    raise ValueError("date-scoped publication requires one current result per cell")
                manual_day = {key: value for key, value in manual.items() if key[0] == submitted}
                replacements = build_cell_cluster_matrices_v2(
                    config, days, submitted, summary_manual_values=manual_day,
                    first_report_date=submitted, calculate_saldo=False,
                )
                merged, patches = {}, {}
                for role in ROLES:
                    prior_matrix = prior[role]["matrix"] if prior else None
                    preserve_saldo_chain = False
                    if role == "summary" and prior_matrix is not None:
                        prior_matrix = copy.deepcopy(prior_matrix)
                        for entry in prior_matrix["editable_inputs"]:
                            key = (entry["report_date"], entry["cell_code"])
                            if key in manual:
                                row, column = entry["row"] - 1, entry["column"] - 1
                                prior_matrix["values"][row][column] = manual[key]
                                prior_matrix["numeric_preview"][row][column] = manual[key]
                    if role == "summary":
                        has_saldo_chain = (prior_matrix is not None and
                            preserve_cell_date_saldo_chain(prior_matrix, replacements[role], submitted))
                        preserve_saldo_chain = has_saldo_chain
                        if not has_saldo_chain:
                            replacements[role] = mark_cell_date_saldo_unverified(
                                replacements[role], submitted,
                            )
                    merged[role], patches[role] = merge_cell_date_matrix(
                        role, prior_matrix, replacements[role], submitted,
                        preserve_saldo_chain=preserve_saldo_chain,
                    )
                snapshot = {
                    "schema_version": 2, "spreadsheet_id": spreadsheet_id,
                    "worksheets": {role: {"worksheet_id": targets[role].worksheet_id,
                        "title": targets[role].title, "values": merged[role]["values"],
                        "matrix": merged[role], "patch": patches[role]} for role in ROLES},
                    "source_calculations": _source_calculation_history(rows),
                }
                if refresh_request_id is not None:
                    snapshot["refresh_request_id"] = refresh_request_id
                return {"start": submitted, "end": submitted, "snapshot": snapshot}
            through = max(submitted, session.latest_report_date() or submitted)
            identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
            days = [{"identity": identities[row["cell_code"]],
                     "report_date": row["report_date"].isoformat() if hasattr(row["report_date"], "isoformat") else str(row["report_date"]),
                     "generation_id": row["generation_id"], "calculation_id": row["calculation_id"],
                     "config_id": row["config_id"], "rules_version": config.rules_version,
                     "calculation": row["result"]}
                    for row in session.current_results(config.first_report_date, through)]
            reset = sorted(
                set(session.dirty_dates(reset_only=True)) if preserve_manual_inputs else
                (set() if configuration_only else {submitted}) | set(session.dirty_dates(reset_only=True))
            )
            report = build_cluster_report(config, days, through)
            matrices = build_cell_cluster_matrices_v2(config, days, through, summary_manual_values=manual,
                summary_reset_dates=mandiri_reset_dates(report, reset) if reset and prior else set())
            audits.extend(matrices["summary"].pop("manual_reset_audit", []))
            snapshot = {
                "schema_version": 2, "spreadsheet_id": spreadsheet_id,
                "worksheets": {role: {"worksheet_id": targets[role].worksheet_id,
                    "title": targets[role].title, "values": matrices[role]["values"], "matrix": matrices[role]}
                    for role in ROLES},
                "source_calculations": _source_calculation_history(days),
            }
            if refresh_request_id is not None:
                snapshot["refresh_request_id"] = refresh_request_id
            return {"start": config.first_report_date, "end": through, "snapshot": snapshot,
                    "refresh_audit_required": bool(audits)}

        if publication_only:
            rendered = render_committed(store)
            if date_scoped:
                _validate_deleted_row_tails(
                    rendered["snapshot"]["worksheets"], adapters, metadata, call,
                )
            staged = store.stage_publication(
                rendered["start"], rendered["end"], rendered["snapshot"],
                write_gsheet=True,
                refresh_audit_required=rendered.get("refresh_audit_required", False),
            )
        else:
            staged = store.commit_and_stage(batch, config.cells, render_committed,
                write_gsheet=True, additional_batches=additional_batches)
        saved = store.publication_snapshot(staged["epoch"])["snapshot"]["worksheets"]
        for audit in audits:
            store.record_manual_reset(staged["epoch"], audit["cell_code"], audit["report_date"],
                                      audit["previous_value"], audit["replacement_value"])
        marker = {"status": "PENDING", "epoch": staged["epoch"], "fingerprint": staged["fingerprint"]}
        report("publication_staged")
        if staged["status"] != "COMPLETE":
            setup = []
            for role in ROLES:
                target, values = targets[role], saved[role]["values"]
                grid = guarded[role]["properties"].get("gridProperties", {})
                capacity = cell_grid_capacity(len(values), len(values[0]), grid)
                setup.extend([
                    {"updateSheetProperties": {"properties": {"sheetId": target.worksheet_id,
                        "gridProperties": {**capacity, "frozenRowCount": 2 if role in ("summary", "invoice") else 1,
                                           "frozenColumnCount": 3 if role == "summary" else 0,
                                           "hideGridlines": True}},
                        "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount,hideGridlines)"}},
                    _marker_request(target.sheet_target(), guarded[role], marker),
                    *_status_requests(target.sheet_target(), marker),
                ])
            store.begin_sheet_write(staged["epoch"])
            write_started = True
            send(setup)
            for role in ROLES:
                _verify_marker(call(adapters[role].metadata), marker)
            if date_scoped:
                _write_date_snapshot(saved, targets, adapters, store, staged["epoch"],
                                     mutate, send_formatting, report, rows_per_request)
            else:
                _write_full_snapshot(saved, prior, targets, adapters, guarded, store,
                                     staged["epoch"], mutate, send_formatting, report,
                                     rows_per_request)
        else:
            marker["status"] = "COMPLETE"
        for role in ROLES:
            report("worksheet_readback_started", role=role)
            call(_verify_values, adapters[role], saved[role], prior[role] if prior else None)
            call(_verify_style, adapters[role], saved[role]["matrix"], prior[role] if prior else None)
            _verify_marker(call(adapters[role].metadata), marker)
        marker["status"] = "COMPLETE"
        finish = []
        for role in ROLES:
            target = targets[role].sheet_target()
            finish.extend(_protection_requests(target, saved[role]["matrix"], guarded[role], guards[role]))
            finish.extend([_marker_request(target, call(adapters[role].metadata), marker), *_status_requests(target, marker)])
        send(finish)
        for role in ROLES:
            meta = call(adapters[role].metadata)
            _verify_final_protections(targets[role].sheet_target(), saved[role]["matrix"], meta)
            _verify_marker(meta, marker)
            call(_verify_values, adapters[role], saved[role], prior[role] if prior else None)
            call(_verify_style, adapters[role], saved[role]["matrix"], prior[role] if prior else None)
        if staged["status"] != "COMPLETE":
            store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])
        store.finish_target_operation(operation_id)
        return {**staged, "status": "COMPLETE", "roles": list(ROLES)}
    except Exception as exc:
        # Only unacknowledged mutations or lost ownership/DB connectivity are
        # ambiguous. A failed readback after acknowledged writes remains PENDING.
        if not write_started and prior and guarded is not None:
            try:
                if set(guards) != set(ROLES):
                    raise RuntimeError("temporary guards are incomplete")
                _restore_prior_guards(prior, targets, adapters, guarded, guards,
                                      previous, send, call)
            except Exception:
                mutation_uncertain = True
                write_started = True
        uncertain = mutation_uncertain or ownership_lost or isinstance(exc, (InterfaceError, OperationalError))
        if staged and staged["status"] != "COMPLETE":
            try:
                if uncertain and write_started:
                    store.mark_unknown(staged["epoch"], type(exc).__name__)
                elif not uncertain:
                    store.mark_pending(staged["epoch"], type(exc).__name__)
            except Exception:
                pass
        try:
            store.finish_target_operation(operation_id, **{
                "unknown_error" if uncertain else "known_error": type(exc).__name__})
        except Exception:
            pass
        raise
