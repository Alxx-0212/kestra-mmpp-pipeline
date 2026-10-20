"""Four FinPay projections, one cluster/workbook publication epoch, no retries."""
from __future__ import annotations

import copy
import json
import re
import uuid

from .cell_calculation import build_cluster_report, recalculate_stale_history
from .cell_contracts import fingerprint
from .cell_database import validate_cluster_batch
from .cell_detail_sheets import build_cell_detail_history, detail_format_requests
from .cell_invoice_sheets import build_cell_invoice_matrix, invoice_format_requests
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
            expected["background"] = (palette["mandiri"] if group == "MANDIRI" else
                palette["cash"] if group == "CASH" else palette["detail"] if row % 2 else palette["white"])
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
            expected.update(background=palette["white"] if row % 2 else palette["stripe"], vertical="TOP")
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
                    raise ValueError(f"{contract['role']} effective style mismatch at row {index + 1}, column {col + 1}")
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
                        raise ValueError(f"{contract['role']} effective style border mismatch at row {index + 1}, column {col + 1}")
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
                                   summary_reset_dates=None, dummy=False):
    report = build_cluster_report(config, cell_days, through_date)
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
            if _marker_entries(meta) or protections:
                raise ValueError("unpublished v2 sheet has unmanaged generation/protection state")
            _require_empty(adapter, meta)
    if prior:
        _read_baseline(adapters, prior)
    return publication


def publish_cell_cluster_v2(store, spreadsheet_id, targets, adapters, batch, config, *,
                            changed_dates=None, additional_batches=None, configuration_only=False,
                            rows_per_request=200, publication_only=False,
                            preserve_manual_inputs=False):
    """Guard all tabs, stage once, write/read back all, then confirm one epoch."""
    from psycopg import InterfaceError, OperationalError
    if type(rows_per_request) is not int or rows_per_request < 1:
        raise ValueError("positive v2 write chunk size required")
    if any(type(value) is not bool for value in (
        configuration_only, publication_only, preserve_manual_inputs,
    )):
        raise ValueError("publication mode flags must be explicit booleans")
    if publication_only:
        if batch is not None or changed_dates is not None or additional_batches or configuration_only:
            raise ValueError("publication-only mode cannot import or recalculate source batches")
        submitted = store.latest_report_date()
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
    prior = previous["snapshot"]["worksheets"] if previous else None
    metadata = {role: call(adapters[role].metadata) for role in ROLES}
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    staged, write_started = None, False
    try:
        requests, guards, reply_indexes = [], {}, {}
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
            source_ids = {}
            for day in days:
                source_ids.setdefault(day["report_date"], []).append(day["calculation_id"])
            # Every v2 tab is replaced over its full history, including unchanged dates.
            return {"start": config.first_report_date, "end": through, "snapshot": {
                "schema_version": 2, "spreadsheet_id": spreadsheet_id,
                "worksheets": {role: {"worksheet_id": targets[role].worksheet_id,
                    "title": targets[role].title, "values": matrices[role]["values"], "matrix": matrices[role]}
                    for role in ROLES},
                "source_calculations": {day: fingerprint(sorted(ids)) for day, ids in source_ids.items()}},
                "refresh_audit_required": bool(audits)}

        if publication_only:
            rendered = render_committed(store)
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
                for index, request in enumerate(requests):
                    mutate(adapters[role], [request])
                    store.confirm_chunk(staged["epoch"], f"{role}-rows-{index}")
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
        else:
            marker["status"] = "COMPLETE"
        for role in ROLES:
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
