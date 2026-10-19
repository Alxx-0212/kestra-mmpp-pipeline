"""Governed cell publication: explicit target, one DB session, no blind retries."""
from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass

from .cell_calculation import build_cluster_report, recalculate_stale_history
from .cell_contracts import fingerprint
from .cell_database import validate_cluster_batch
from .cell_sheets import (
    DESCRIPTION_WIDTH_PX, REFERENCE_COLORS, cell_grid_capacity, column_letter,
    mandiri_reset_dates, render_cell_matrix, section_border_rows, section_fill,
)

GUARD_PREFIX = "FINPAY CELL REFRESH V1:"
MANAGED_PREFIX = "FINPAY CELL REPORT V1:"
MAX_REQUEST_BYTES = 1_500_000
PUBLICATION_METADATA_KEY = "finpay_cell_publication_v2"


def _marker_entries(metadata):
    entries = [item for item in metadata.get("developerMetadata", [])
               if item.get("metadataKey") == PUBLICATION_METADATA_KEY]
    if len(entries) > 1:
        raise ValueError("duplicate publication metadata requires reconciliation")
    return entries


def _marker_request(target, metadata, marker):
    entries = _marker_entries(metadata)
    value = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    if entries:
        return {"updateDeveloperMetadata": {
            "dataFilters": [{"developerMetadataLookup": {"metadataId": entries[0]["metadataId"]}}],
            "developerMetadata": {"metadataValue": value}, "fields": "metadataValue",
        }}
    return {"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": PUBLICATION_METADATA_KEY, "metadataValue": value,
        "location": {"sheetId": target.worksheet_id}, "visibility": "DOCUMENT",
    }}}


def _verify_marker(metadata, expected):
    entries = _marker_entries(metadata)
    if not entries or json.loads(entries[0].get("metadataValue", "null")) != expected:
        raise ValueError("publication generation metadata readback mismatch")


def _status_requests(target, marker):
    pending = marker["status"] != "COMPLETE"
    color = {"red": 1, "green": 0.75, "blue": 0.15} if pending else {"red": 0.18, "green": 0.55, "blue": 0.30}
    note = (
        f"Sheet publication INCOMPLETE; epoch {marker['epoch']}. Amber tab: do not use this report."
        if pending else
        f"Sheet contents VERIFIED; epoch {marker['epoch']}. Green confirms Sheets only. "
        "Database completion is tracked separately; a reported pipeline failure requires operator reconciliation."
    )
    if marker["status"] == "DESIGN_PREVIEW":
        color = {"red": 0.20, "green": 0.45, "blue": 0.75}
        note = "SYNTHETIC DESIGN PREVIEW ONLY. No database publication was updated by this presentation change."
    return [
        {"updateSheetProperties": {"properties": {"sheetId": target.worksheet_id,
            "tabColorStyle": {"rgbColor": color}}, "fields": "tabColorStyle"}},
        {"updateCells": {"range": {"sheetId": target.worksheet_id, "startRowIndex": 0,
            "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": note}]}], "fields": "note"}},
    ]


@dataclass(frozen=True)
class CellSheetTarget:
    spreadsheet_id: str
    worksheet_id: int
    writer_email: str
    finance_emails: tuple[str, ...]

    def validate(self, approved_targets):
        if self not in approved_targets or not self.spreadsheet_id:
            raise ValueError("explicit allowlisted cell worksheet target required")
        if type(self.worksheet_id) is not int or self.worksheet_id < 0:
            raise ValueError("worksheet ID must be explicit")
        if not self.writer_email or not self.finance_emails:
            raise ValueError("explicit writer and Finance editor identities required")


@dataclass(frozen=True)
class CellWorksheetTarget:
    """One pinned worksheet role within the separately approved v2 workbook."""
    role: str
    spreadsheet_id: str
    worksheet_id: int
    title: str
    writer_email: str
    finance_emails: tuple[str, ...]
    naming_scheme: str = "code"

    def sheet_target(self):
        if self.role not in {"summary", "invoice", "unusual", "reversal"}:
            raise ValueError("unknown cell worksheet role")
        if not self.spreadsheet_id or type(self.worksheet_id) is not int or self.worksheet_id < 0:
            raise ValueError("worksheet workbook/ID must be explicit")
        if not self.title or not self.writer_email or not self.finance_emails:
            raise ValueError("worksheet title and approved writers must be explicit")
        return CellSheetTarget(self.spreadsheet_id, self.worksheet_id,
                               self.writer_email, self.finance_emails)


class ExistingSheetAdapter:
    """Wrap an already-open approved worksheet; never discovers/creates/shares."""

    def __init__(self, spreadsheet, worksheet):
        self.spreadsheet = spreadsheet
        self.worksheet = worksheet
        self._owners = None

    @property
    def spreadsheet_id(self):
        return self.spreadsheet.id

    @property
    def worksheet_id(self):
        return self.worksheet.id

    def metadata(self):
        if self._owners is None:
            response = self.spreadsheet.client.request(
                "get", f"https://www.googleapis.com/drive/v3/files/{self.spreadsheet_id}",
                params={"fields": "id,owners(emailAddress)", "supportsAllDrives": "true"},
            ).json()
            if response.get("id") != self.spreadsheet_id:
                raise ValueError("spreadsheet ownership metadata identity mismatch")
            self._owners = [item["emailAddress"] for item in response.get("owners", [])]
        sheets = self.spreadsheet.fetch_sheet_metadata().get("sheets", [])
        result = dict(next(sheet for sheet in sheets if sheet["properties"]["sheetId"] == self.worksheet_id))
        result["implicitOwnerEmails"] = list(self._owners)
        return result

    def batch_update(self, requests):
        return self.spreadsheet.batch_update({"requests": requests})

    def read(self, rows, columns):
        return self.worksheet.get(f"A1:{column_letter(columns)}{rows}", value_render_option="FORMULA")

    def read_style(self, rows, columns):
        """Read bounded formatting metadata, never values, formulas or other tabs."""
        if type(rows) is not int or type(columns) is not int or not 0 < rows <= 100_000 or not 0 < columns <= 100:
            raise ValueError("style read exceeds governed dimensions")
        title = self.worksheet.title.replace("'", "''")
        structure_fields = "spreadsheetId,sheets(properties(sheetId),merges,bandedRanges(range))"
        structure = self.spreadsheet.fetch_sheet_metadata(params={"fields": structure_fields})
        if structure.get("spreadsheetId") != self.spreadsheet_id:
            raise ValueError("style spreadsheet identity mismatch")
        sheets = structure.get("sheets", [])
        matches = [sheet for sheet in sheets if sheet.get("properties", {}).get("sheetId") == self.worksheet_id]
        if len(matches) != 1 or any(sheet.get("data") for sheet in sheets):
            raise ValueError("style worksheet identity mismatch")
        merges = matches[0].get("merges", [])
        banding = matches[0].get("bandedRanges", [])
        fields = ("spreadsheetId,properties(defaultFormat),sheets(properties(sheetId,gridProperties),"
            "data(startRow,startColumn,columnMetadata(pixelSize),rowMetadata(pixelSize),"
            "rowData(values(effectiveFormat,userEnteredFormat))))")
        for start in range(0, rows, 100):
            stop = min(start + 100, rows)
            result = {"start": start, "formats": [[{} for _ in range(columns)] for _ in range(stop - start)],
                      "entered": [[{} for _ in range(columns)] for _ in range(stop - start)],
                      "widths": [None] * columns, "heights": [None] * (stop - start)}
            response = self.spreadsheet.fetch_sheet_metadata(params={"fields": fields,
                "ranges": f"'{title}'!A{start + 1}:{column_letter(columns)}{stop}"})
            if response.get("spreadsheetId") != self.spreadsheet_id:
                raise ValueError("style spreadsheet identity mismatch")
            sheets = response.get("sheets", [])
            matches = [sheet for sheet in sheets if sheet["properties"]["sheetId"] == self.worksheet_id]
            if len(matches) != 1 or any(sheet.get("data") for sheet in sheets if sheet not in matches):
                raise ValueError("style worksheet identity mismatch")
            sheet = matches[0]
            result["grid"] = sheet["properties"].get("gridProperties", {})
            # Ranges clip grid data and also clip sheet-level merge/banding metadata.
            result["merges"] = merges
            result["banding"] = banding
            result["default"] = response.get("properties", {}).get("defaultFormat", {})
            for block in sheet.get("data", []):
                first, column = block.get("startRow", 0), block.get("startColumn", 0)
                if first != start or column != 0:
                    raise ValueError("unexpected style grid origin")
                for c, meta in enumerate(block.get("columnMetadata", [])):
                    if c >= columns:
                        raise ValueError("style column metadata exceeds requested range")
                    result["widths"][c] = meta.get("pixelSize")
                for offset, meta in enumerate(block.get("rowMetadata", [])):
                    if first + offset >= stop:
                        raise ValueError("style row metadata exceeds requested range")
                    result["heights"][offset] = meta.get("pixelSize")
                for offset, row in enumerate(block.get("rowData", [])):
                    if first + offset >= stop or len(row.get("values", [])) > columns:
                        raise ValueError("style cells exceed requested range")
                    for c, cell in enumerate(row.get("values", [])):
                        result["formats"][offset][c] = cell.get("effectiveFormat", {})
                        result["entered"][offset][c] = cell.get("userEnteredFormat", {})
            yield result


def _typed(value, *, allow_formulas=True):
    if value == "":
        return {}
    if type(value) is int and abs(value) <= 2 ** 53 - 1:
        return {"numberValue": value}
    if isinstance(value, str):
        return {"formulaValue" if allow_formulas and value.startswith("=") else "stringValue": value}
    raise ValueError("unsupported or inexact spreadsheet value")


def matrix_write_requests(values, sheet_id, *, start_row=0, rows_per_request=250, allow_formulas=True):
    if type(rows_per_request) is not int or rows_per_request < 1:
        raise ValueError("positive rows_per_request required")
    requests = []
    offset = 0
    while offset < len(values):
        count = min(rows_per_request, len(values) - offset)
        while True:
            chunk = values[offset:offset + count]
            request = {"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": start_row + offset,
                          "endRowIndex": start_row + offset + count,
                          "startColumnIndex": 0, "endColumnIndex": len(values[0])},
                "rows": [{"values": [{"userEnteredValue": _typed(value, allow_formulas=allow_formulas)} for value in row]} for row in chunk],
                "fields": "userEnteredValue",
            }}
            if len(json.dumps({"requests": [request]}).encode("utf-8")) <= MAX_REQUEST_BYTES:
                break
            if count == 1:
                raise ValueError("a spreadsheet row exceeds the bounded request size")
            count //= 2
        requests.append(request)
        offset += count
    return requests


def _padded(rows, count, width):
    if len(rows) > count or any(len(row) > width for row in rows):
        raise ValueError("unexpected spreadsheet readback dimensions")
    if any(type(value) is bool for row in rows for value in row):
        raise ValueError("unexpected boolean in governed spreadsheet values")
    return [list(rows[index]) + [""] * (width - len(rows[index])) if index < len(rows)
            else [""] * width for index in range(count)]


def _manual_values(old_matrix, actual):
    result = {}
    if old_matrix is None:
        if any(any(value != "" for value in row) for row in actual):
            raise ValueError("non-empty worksheet has no governed cell publication baseline")
        return result
    width = len(old_matrix["values"][0])
    actual = _padded(actual, len(old_matrix["values"]), width)
    for entry in old_matrix["editable_inputs"]:
        value = actual[entry["row"] - 1][entry["column"] - 1]
        if value == entry["default"]:
            continue
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if type(value) is not int or abs(value) > 2 ** 53 - 1:
            raise ValueError("manual bank input is not an exact amount or recognized default")
        result[(entry["report_date"], entry["cell_code"])] = value
    return result


def _validate_input_policy(matrix):
    role = matrix.get("style_contract", {}).get("role")
    if role is None:
        return  # Persisted legacy layouts keep their original compatibility policy.
    inputs = matrix.get("editable_inputs", [])
    if role != "summary":
        if inputs:
            raise ValueError("non-Summary output cannot expose Finance input holes")
        return
    expected = {}
    for area in matrix["date_ranges"]:
        rows = [row for row in range(area["start_row"], area["end_row"] + 1)
                if matrix["row_keys"][row - 1] == "CASH:cash:MANDIRI"]
        if len(rows) != 1:
            raise ValueError("Summary requires exactly one bank input row per date")
        for block in matrix["blocks"]:
            if block["cell_code"] is not None:
                if matrix["values"][1][block["start_column"] - 1] != "DEBET":
                    raise ValueError("bank input is not in its approved DEBET column")
                expected[(area["report_date"], block["cell_code"])] = (rows[0], block["start_column"])
    actual = {(item["report_date"], item["cell_code"]): (item["row"], item["column"]) for item in inputs}
    if len(actual) != len(inputs) or actual != expected:
        raise ValueError("Finance edit holes differ from the exact MANDIRI input contract")


def _protection_requests(target, matrix, metadata, guard_id):
    _validate_input_policy(matrix)
    managed = [item for item in metadata.get("protectedRanges", [])
               if item.get("description", "").startswith(MANAGED_PREFIX)
               and item["protectedRangeId"] != guard_id]
    requests = [{"deleteProtectedRange": {"protectedRangeId": item["protectedRangeId"]}} for item in managed]
    inputs = [{"sheetId": target.worksheet_id, "startRowIndex": item["row"] - 1,
               "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
               "endColumnIndex": item["column"]} for item in matrix.get("editable_inputs", [])]
    requests.append({"updateProtectedRange": {"protectedRange": {
        "protectedRangeId": guard_id, "description": MANAGED_PREFIX + "COMPUTED",
        "warningOnly": False, "editors": {"users": [target.writer_email]},
        "unprotectedRanges": inputs,
    }, "fields": "description,warningOnly,editors,unprotectedRanges"}})
    for grid in inputs:
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": grid, "description": MANAGED_PREFIX + "BANK INPUT",
            "warningOnly": False,
            "editors": {"users": sorted(set((target.writer_email, *target.finance_emails)))},
        }}})
    return requests


def _format_requests(target, matrix):
    if matrix.get("layout_version") in (3, 4, 5):
        return _compact_format_requests(target, matrix)
    width = len(matrix["values"][0])
    sheet_id = target.worksheet_id
    requests = [{"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 2,
                  "startColumnIndex": 0, "endColumnIndex": width},
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.14, "green": 0.24, "blue": 0.30},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }}, "fields": "userEnteredFormat(backgroundColor,textFormat)",
    }}]
    block_borders = []
    for block in matrix["blocks"]:
        start = block["start_column"] - 1
        requests.append({"mergeCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 3 if start == 0 else start, "endColumnIndex": start + 6},
            "mergeType": "MERGE_ALL",
        }})
        for offset, pixels in enumerate((115, 100, 390, 130, 155, 150)):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": start + offset, "endIndex": start + offset + 1},
                "properties": {"pixelSize": pixels}, "fields": "pixelSize",
            }})
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": len(matrix["values"]),
                      "startColumnIndex": start + 3, "endColumnIndex": start + 6},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }})
        edge = {"style": "SOLID_MEDIUM", "color": {"red": 0.14, "green": 0.24, "blue": 0.30}}
        for first, last in [(0, len(matrix["values"])), (0, 2), (2, 3), *[(item["start_row"] - 1, item["end_row"]) for item in matrix["date_ranges"]]]:
            block_borders.append({"updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": first, "endRowIndex": last,
                          "startColumnIndex": start, "endColumnIndex": start + 6},
                "left": edge, "right": edge, "top": edge, "bottom": edge,
            }})
    for spacer in (6, 13, 20):
        if spacer >= width:
            continue
        grid = {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(matrix["values"]),
                "startColumnIndex": spacer, "endColumnIndex": spacer + 1}
        requests.extend([
            {"repeatCell": {"range": grid, "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 1, "green": 1, "blue": 1},
            }}, "fields": "userEnteredFormat.backgroundColor"}},
            {"updateBorders": {"range": grid, **{side: {"style": "NONE"} for side in
                ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                "startIndex": spacer, "endIndex": spacer + 1}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
        ])
    requests.extend(block_borders)
    for item in matrix["editable_inputs"]:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": item["row"] - 1,
                      "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
                      "endColumnIndex": item["column"]},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 0.95, "blue": 0.75}}},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    return requests


def _rgb(hex_color):
    return dict(zip(("red", "green", "blue"), (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))))


def _compact_format_requests(target, matrix):
    sheet, width, height = target.worksheet_id, len(matrix["values"][0]), len(matrix["values"])
    requests = []

    def grid(sr, er, sc, ec):
        return {"sheetId": sheet, "startRowIndex": sr, "endRowIndex": er,
                "startColumnIndex": sc, "endColumnIndex": ec}

    def format_range(area, value, fields):
        requests.append({"repeatCell": {"range": area, "cell": {"userEnteredFormat": value}, "fields": fields}})

    format_range(grid(0, height, 0, width), {"textFormat": {"fontFamily": "Arial", "fontSize": 10,
        "foregroundColor": _rgb(REFERENCE_COLORS["text"]), "bold": False},
        "horizontalAlignment": "LEFT", **({"verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP"}
            if matrix.get("layout_version") in (4, 5) else {})},
        "userEnteredFormat" if matrix.get("layout_version") in (4, 5) else "userEnteredFormat(textFormat,horizontalAlignment)")
    format_range(grid(0, 2, 0, width), {"backgroundColor": _rgb(REFERENCE_COLORS["header"]),
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        **({"wrapStrategy": "CLIP"} if matrix.get("layout_version") in (4, 5) else {}),
        "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True,
                       "foregroundColor": _rgb("FFFFFF")}}, "userEnteredFormat")
    for index in range(2, height):
        format_range(grid(index, index + 1, 0, width), {"backgroundColor": _rgb(section_fill(matrix, index + 1))},
                     "userEnteredFormat.backgroundColor")
        key = matrix["row_keys"][index].split(":", 1)[0]
        if key in ("DETAIL", "MANDIRI") and key != matrix["row_keys"][index - 1].split(":", 1)[0]:
            format_range(grid(index, index + 1, 1, 2), {"backgroundColor": _rgb(REFERENCE_COLORS["section"]),
                "textFormat": {"bold": True, "foregroundColor": _rgb(REFERENCE_COLORS["text"]),
                    **({"fontFamily": "Arial", "fontSize": 10} if matrix.get("layout_version") in (4, 5) else {})}},
                "userEnteredFormat(backgroundColor,textFormat)")
    format_range(grid(0, height, 2, 3), {"wrapStrategy": "CLIP"}, "userEnteredFormat.wrapStrategy")
    for col, pixels in enumerate((115, 100, DESCRIPTION_WIDTH_PX)):
        requests.extend([
            {"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
                "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": pixels}, "fields": "pixelSize"}},
            {"mergeCells": {"range": grid(0, 2, col, col + 1), "mergeType": "MERGE_ALL"}},
        ])
    border = {"style": "SOLID_MEDIUM", "color": _rgb(REFERENCE_COLORS["header"])}
    areas = [(0, 3)]
    for block in matrix["blocks"]:
        start, end = block["start_column"] - 1, block["end_column"]
        areas.append((start, end))
        requests.append({"mergeCells": {"range": grid(0, 1, start, end), "mergeType": "MERGE_ALL"}})
        format_range(grid(2, height, start, end), {"horizontalAlignment": "RIGHT",
            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}},
            "userEnteredFormat(horizontalAlignment,numberFormat)")
        for col in range(start, end):
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
                "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}})
    for spacer in (6, 10, 14):
        if spacer >= width:
            continue
        area = grid(0, height, spacer, spacer + 1)
        format_range(area, {"backgroundColor": _rgb("FFFFFF")}, "userEnteredFormat.backgroundColor")
        requests.append({"updateBorders": {"range": area, **{side: {"style": "NONE"} for side in
            ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}})
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet, "dimension": "COLUMNS",
            "startIndex": spacer, "endIndex": spacer + 1}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}})
    light = {"style": "SOLID", "color": _rgb("A5B2BF")}
    section_rows, subtotal_rows = section_border_rows(matrix)
    # Apply the reference grid before strong rules so reruns do not leave
    # old Cash or category borders from an earlier presentation.
    grid_requests = []
    for start, end in areas:
        grid_requests.append({"updateBorders": {"range": grid(0, height, start, end),
            **{side: light for side in ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}})
    requests.extend(grid_requests)
    for start, end in areas:
        requests.append({"updateBorders": {"range": grid(0, height, start, end), "left": border, "right": border}})
        requests.append({"updateBorders": {"range": grid(0, 2, start, end), "top": border, "bottom": border}})
        for row in section_rows:
            requests.append({"updateBorders": {"range": grid(row - 1, row, start, end), "top": border}})
    for block in matrix["blocks"]:
        for row in subtotal_rows:
            start = block["start_column"] - 1
            requests.append({"updateBorders": {"range": grid(row - 1, row, start, start + 1), "top": border}})
    return requests


def _verify_final_protections(target, matrix, metadata):
    _validate_input_policy(matrix)
    protections = metadata.get("protectedRanges", [])
    expected = [{"sheetId": target.worksheet_id, "startRowIndex": item["row"] - 1,
                 "endRowIndex": item["row"], "startColumnIndex": item["column"] - 1,
                 "endColumnIndex": item["column"]} for item in matrix.get("editable_inputs", [])]
    computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    inputs = [item for item in protections if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
    if len(computed) != 1 or len(inputs) != len(expected):
        raise ValueError("governed protection counts do not match committed layout")
    principal = computed[0]
    owners = metadata.get("implicitOwnerEmails", [])
    if principal.get("range") != {"sheetId": target.worksheet_id} or principal.get("warningOnly", False) is not False:
        raise ValueError("computed range protection was not confirmed")
    actual_holes = sorted(json.dumps(item, sort_keys=True) for item in principal.get("unprotectedRanges", []))
    expected_holes = sorted(json.dumps(item, sort_keys=True) for item in expected)
    if not _editors_match(principal.get("editors", {}), [target.writer_email], owners) or actual_holes != expected_holes:
        raise ValueError("computed/input protection policy mismatch")
    by_range = {json.dumps(item["range"], sort_keys=True): item for item in inputs}
    for grid in expected:
        item = by_range.get(json.dumps(grid, sort_keys=True), {})
        if item.get("unprotectedRanges") or item.get("warningOnly", False) is not False or not _editors_match(
            item.get("editors", {}), [target.writer_email, *target.finance_emails], owners,
        ):
            raise ValueError("Finance input editor protections were not confirmed")


def _editors_match(editors, users, implicit_owners=()):
    actual, expected, owners = set(editors.get("users", [])), set(users), set(implicit_owners)
    return (actual <= expected | owners and expected - owners <= actual
            and not editors.get("groups") and not editors.get("domainUsersCanEdit", False))


def publish_cell_cluster(store, target, approved_targets, adapter, batch, config,
                         *, changed_dates, rows_per_request=250, additional_batches=None,
                         configuration_only=False):
    """Commit/render/publish while retaining the caller's live session lock.

    External calls here are operational code for later approved live use.
    Offline validation uses fake adapters/stores only. Unknown write outcomes
    persist as UNKNOWN/IN_FLIGHT and require operator-proven request quiescence.
    """
    target.validate(approved_targets)
    validate_cluster_batch(batch, config.cells)
    if store.cluster_id != config.account[:-2] or store.target_id != target.spreadsheet_id:
        raise ValueError("publisher session/target identity mismatch")
    if (adapter.spreadsheet_id, adapter.worksheet_id) != (target.spreadsheet_id, target.worksheet_id):
        raise ValueError("opened spreadsheet/worksheet differs from approved IDs")
    if changed_dates != [batch[0]["report_date"]]:
        raise ValueError("publish one complete changed cluster date at a time")
    if configuration_only:
        old_sources = store.current_results(changed_dates[0], changed_dates[0])
        expected = {(item["identity"]["cell_code"], item["generation_id"]) for item in batch}
        actual = {(item["cell_code"], item["generation_id"]) for item in old_sources}
        if len(old_sources) != len(batch) or actual != expected:
            raise ValueError("configuration-only operation cannot import new source generations")
    if additional_batches is None:
        through = max(changed_dates[0], store.latest_report_date() or changed_dates[0])
        additional_batches = recalculate_stale_history(store, config, through, submitted_dates=set(changed_dates))
    for cohort in additional_batches:
        validate_cluster_batch(cohort, config.cells)

    def call(method, *args):
        store.assert_alive()
        response = method(*args)
        store.assert_alive()
        return response

    metadata = call(adapter.metadata)
    if metadata["properties"]["sheetId"] != target.worksheet_id:
        raise ValueError("metadata worksheet identity mismatch")
    protections = metadata.get("protectedRanges", [])
    marker_entries = _marker_entries(metadata)
    if marker_entries and json.loads(marker_entries[0].get("metadataValue", "null")).get("status") == "DESIGN_PREVIEW":
        raise ValueError("design preview needs an approved DB-backed baseline before normal publication")
    if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
        raise RuntimeError("orphaned refresh guard requires operator reconciliation")
    if any(not item.get("description", "").startswith(MANAGED_PREFIX) for item in protections):
        raise ValueError("worksheet contains unmanaged protection; use a dedicated approved worksheet")
    computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    if len(computed) > 1 or any(item.get("range") != {"sheetId": target.worksheet_id} for item in computed):
        raise ValueError("managed computed protection must cover the whole approved worksheet")
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    staged = None
    write_started = False
    try:
        guard_value = {"description": GUARD_PREFIX + operation_id, "warningOnly": False,
                       "editors": {"users": [target.writer_email]}, "unprotectedRanges": []}
        if computed:
            guard_id = computed[0]["protectedRangeId"]
            request = {"updateProtectedRange": {"protectedRange": {
                "protectedRangeId": guard_id, **guard_value,
            }, "fields": "description,warningOnly,editors,unprotectedRanges"}}
        else:
            request = {"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": target.worksheet_id}, **guard_value,
            }}}
        lock_inputs = [{"updateProtectedRange": {"protectedRange": {
            "protectedRangeId": item["protectedRangeId"], "warningOnly": False,
            "editors": {"users": [target.writer_email]},
        }, "fields": "warningOnly,editors"}} for item in protections
            if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
        guard_reply = call(adapter.batch_update, [request, *lock_inputs])
        if not computed:
            guard_id = guard_reply["replies"][0]["addProtectedRange"]["protectedRange"]["protectedRangeId"]
        guarded = call(adapter.metadata)
        if not any(item.get("protectedRangeId") == guard_id
                   and item.get("range") == {"sheetId": target.worksheet_id}
                   and item.get("description") == GUARD_PREFIX + operation_id
                   and item.get("warningOnly", False) is False
                   and _editors_match(item.get("editors", {}), [target.writer_email], guarded.get("implicitOwnerEmails", []))
                   and not item.get("unprotectedRanges") for item in guarded.get("protectedRanges", [])):
            raise ValueError("temporary input protection was not confirmed")
        if any(item.get("unprotectedRanges") or not _editors_match(item.get("editors", {}), [target.writer_email], guarded.get("implicitOwnerEmails", []))
               for item in guarded.get("protectedRanges", []) if item.get("description") == MANAGED_PREFIX + "BANK INPUT"):
            raise ValueError("temporary bank input lock was not confirmed")
        previous = store.latest_publication()
        old_matrix = previous["snapshot"].get("matrix") if previous else None
        if old_matrix:
            actual = call(adapter.read, len(old_matrix["values"]), len(old_matrix["values"][0]))
        else:
            dimensions = metadata["properties"].get("gridProperties", {})
            actual = call(adapter.read, dimensions.get("rowCount", 1000), dimensions.get("columnCount", 27))
        manual = _manual_values(old_matrix, actual)
        audits = []

        def render_committed(session):
            through = max(changed_dates[0], session.latest_report_date() or changed_dates[0])
            current = session.current_results(config.first_report_date, through)
            identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
            facts = [{"identity": identities[row["cell_code"]],
                      "report_date": row["report_date"].isoformat() if hasattr(row["report_date"], "isoformat") else row["report_date"],
                      "calculation_id": row["calculation_id"], "generation_id": row["generation_id"],
                      "config_id": row["config_id"], "calculation": row["result"]} for row in current]
            report = build_cluster_report(config, facts, through)
            all_changed = sorted(set(changed_dates) | set(session.dirty_dates()))
            reset_changed = sorted((set() if configuration_only else set(changed_dates))
                                   | set(session.dirty_dates(reset_only=True)))
            matrix = render_cell_matrix(report, manual_values=manual,
                                        reset_dates=mandiri_reset_dates(report, reset_changed) if reset_changed else set())
            audits.extend(matrix.pop("manual_reset_audit"))
            from .cell_calculation import affected_publication_range
            first, last = affected_publication_range(config.first_report_date, all_changed, through)
            generations = {}
            for fact in facts:
                generations.setdefault(fact["report_date"], []).append(fact["calculation_id"])
            return {"start": first, "end": last,
                    "snapshot": {"worksheet_id": target.worksheet_id, "matrix": matrix,
                                 "source_calculations": {day: fingerprint(sorted(ids)) for day, ids in generations.items()}},
                    "refresh_audit_required": bool(audits)}

        staged = store.commit_and_stage(batch, config.cells, render_committed, write_gsheet=True,
                                       additional_batches=additional_batches)
        committed = store.publication_snapshot(staged["epoch"])
        matrix = committed["snapshot"]["matrix"]
        if committed["snapshot"]["worksheet_id"] != target.worksheet_id or matrix.get("dummy"):
            raise ValueError("committed snapshot is not approved for this worksheet")
        for audit in audits:
            store.record_manual_reset(staged["epoch"], audit["cell_code"], audit["report_date"],
                                      audit["previous_value"], audit["replacement_value"])
        expected = copy.deepcopy(matrix["values"])
        marker = {"status": "PENDING", "epoch": staged["epoch"], "fingerprint": staged["fingerprint"]}
        if len(expected) > 100_000:
            raise ValueError("matrix exceeds bounded publication size")
        dimensions = guarded["properties"].get("gridProperties", {})
        managed_width = max(len(expected[0]), len(old_matrix["values"][0]) if old_matrix else 0)
        setup = [{"unmergeCells": {"range": {"sheetId": target.worksheet_id,
            "startRowIndex": 0, "endRowIndex": 3, "startColumnIndex": 0,
            "endColumnIndex": managed_width}}}, {"updateSheetProperties": {"properties": {
            "sheetId": target.worksheet_id, "gridProperties": {
                **cell_grid_capacity(len(expected), len(expected[0]), dimensions),
                "frozenRowCount": 2 if matrix.get("layout_version") == 3 else 1, "frozenColumnCount": 3,
            }}, "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)"}}]
        # Stage DB state before any irreversible governed values mutation.
        if staged["status"] == "COMPLETE":
            marker["status"] = "COMPLETE"
            _verify_marker(call(adapter.metadata), marker)
            if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
                raise ValueError("previous complete report no longer matches governed snapshot")
        else:
            store.begin_sheet_write(staged["epoch"])
            write_started = True
            call(adapter.batch_update, setup + [_marker_request(target, guarded, marker)] + _status_requests(target, marker))
            for index, request in enumerate(matrix_write_requests(expected, target.worksheet_id, rows_per_request=rows_per_request)):
                call(adapter.batch_update, [request])
                store.confirm_chunk(staged["epoch"], f"rows-{index}")
            if old_matrix and len(old_matrix["values"]) > len(expected):
                blank = [[""] * len(old_matrix["values"][0])]
                clear = matrix_write_requests(blank, target.worksheet_id, start_row=len(expected))[0]
                clear["updateCells"]["range"]["endRowIndex"] = len(old_matrix["values"])
                call(adapter.batch_update, [clear])
            if old_matrix and len(old_matrix["values"][0]) > len(expected[0]):
                call(adapter.batch_update, [{"updateCells": {
                    "range": {"sheetId": target.worksheet_id, "startRowIndex": 0,
                              "endRowIndex": max(len(expected), len(old_matrix["values"])),
                              "startColumnIndex": len(expected[0]), "endColumnIndex": managed_width},
                    "rows": [], "fields": "userEnteredValue,userEnteredFormat,note",
                }}, {"updateDimensionProperties": {"range": {"sheetId": target.worksheet_id,
                    "dimension": "COLUMNS", "startIndex": len(expected[0]), "endIndex": managed_width},
                    "properties": {"pixelSize": 100}, "fields": "pixelSize"}}])
            if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
                raise ValueError("governed generation/value/formula readback mismatch")
            _verify_marker(call(adapter.metadata), marker)
        finish = [{"updateSheetProperties": {"properties": {"sheetId": target.worksheet_id,
            "gridProperties": cell_grid_capacity(len(expected), len(expected[0]), dimensions)},
            "fields": "gridProperties(rowCount,columnCount)"}}, {"unmergeCells": {"range": {"sheetId": target.worksheet_id,
            "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0,
            "endColumnIndex": len(expected[0])}}}]
        finish += _format_requests(target, matrix) + _protection_requests(target, matrix, guarded, guard_id)
        marker["status"] = "COMPLETE"
        finish.append(_marker_request(target, call(adapter.metadata), marker))
        finish.extend(_status_requests(target, marker))
        if len(json.dumps({"requests": finish}).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("protection/confirmation request exceeds publication size limit")
        call(adapter.batch_update, finish)
        final_metadata = call(adapter.metadata)
        if any(item.get("description", "").startswith(GUARD_PREFIX) for item in final_metadata.get("protectedRanges", [])):
            raise ValueError("temporary protection restoration was not confirmed")
        _verify_final_protections(target, matrix, final_metadata)
        _verify_marker(final_metadata, marker)
        if _padded(call(adapter.read, len(expected), len(expected[0])), len(expected), len(expected[0])) != expected:
            raise ValueError("final generation/formula readback mismatch")
        if staged["status"] != "COMPLETE":
            store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])
        store.finish_target_operation(operation_id)
        return {**staged, "status": "COMPLETE"}
    except Exception as exc:
        # Never retry or remove a guard after an unconfirmed external mutation.
        # A dead connection leaves its durable IN_FLIGHT intent for recovery.
        try:
            if staged and write_started:
                store.mark_unknown(staged["epoch"], type(exc).__name__)
            store.finish_target_operation(operation_id, unknown_error=type(exc).__name__)
        except Exception:
            pass
        raise
