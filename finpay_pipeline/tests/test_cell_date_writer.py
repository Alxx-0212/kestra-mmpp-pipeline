import json
import re
import unittest
from unittest.mock import patch

from finpay_pipeline.cell_publication import (
    CellWorksheetTarget, _bounded_date_write_batches, _invoice_spacer_ranges,
    publish_cell_date_blocks,
)


class FakeSpreadsheet:
    def __init__(self, targets, rows_by_role):
        self.id = "test-sheet"
        self.targets = targets
        self.rows = {role: [list(row) for row in rows] for role, rows in rows_by_role.items()}
        self.protections = {role: [] for role in targets}
        self.metadata = []
        self.batches = []
        self.row_count = {role: 2000 for role in targets}
        self.column_count = {role: 32 for role in targets}
        self.merges = {role: [] for role in targets}
        self.grid_properties = {role: {} for role in targets}
        self.column_widths = {role: {} for role in targets}
        self.cell_formats = {role: {} for role in targets}
        self.grid_data_reads = 0

    def fetch_sheet_metadata(self, params=None):
        if params and params.get("includeGridData"):
            self.grid_data_reads += 1
        sheets = []
        for role, target in self.targets.items():
            sheet = {
                "properties": {
                    "sheetId": target.worksheet_id,
                    "title": target.title,
                    "gridProperties": {
                        "rowCount": self.row_count[role],
                        "columnCount": self.column_count[role],
                    },
                },
                "protectedRanges": list(self.protections[role]),
                "merges": list(self.merges[role]),
            }
            if params and params.get("includeGridData"):
                ranges = params.get("ranges", [])
                if isinstance(ranges, str):
                    ranges = [ranges]
                data = []
                for a1_range in ranges:
                    match = re.fullmatch(r"'((?:[^']|'')*)'!([A-Z]+)(\d*)[: ]([A-Z]+)?(\d*)", a1_range)
                    if not match or match.group(1).replace("''", "'") != target.title:
                        continue
                    start_column = _column_index(match.group(2))
                    end_column = _column_index(match.group(4) or match.group(2)) + 1
                    start_row = int(match.group(3) or "1") - 1
                    if match.group(5):
                        end_row = int(match.group(5))
                    else:
                        end_row = max((index + 1 for index, row in enumerate(self.rows[role])
                                       if any(value not in ("", None) for value in row)), default=start_row)
                    row_data = []
                    for row_index in range(start_row, end_row):
                        source = self.rows[role][row_index] if row_index < len(self.rows[role]) else []
                        cells = []
                        for column in range(start_column, end_column):
                            value = source[column] if column < len(source) else ""
                            cell = {}
                            if value not in ("", None):
                                if isinstance(value, str):
                                    key = "formulaValue" if value.startswith("=") else "stringValue"
                                    cell["userEnteredValue"] = {key: value}
                                elif type(value) in (int, float):
                                    cell["userEnteredValue"] = {"numberValue": value}
                                else:
                                    cell["userEnteredValue"] = {"stringValue": str(value)}
                            entered_format = self.cell_formats[role].get((row_index, column), {})
                            if entered_format:
                                cell["userEnteredFormat"] = entered_format
                            cell["effectiveFormat"] = entered_format or {
                                "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                            }
                            cells.append(cell)
                        row_data.append({"values": cells})
                    data.append({"startRow": start_row, "startColumn": start_column,
                                 "rowData": row_data})
                sheet["data"] = data
            sheets.append(sheet)
        return {"spreadsheetId": self.id, "sheets": sheets,
                "developerMetadata": list(self.metadata)}

    def values_batch_get(self, ranges, params=None):
        result = []
        for a1_range in ranges:
            match = re.fullmatch(r"'((?:[^']|'')*)'!([A-Z]+)(\d*)[: ]([A-Z]+)?(\d*)", a1_range)
            if not match:
                raise AssertionError(f"unexpected range: {a1_range}")
            title = match.group(1).replace("''", "'")
            role = next(role for role, target in self.targets.items() if target.title == title)
            start_column = _column_index(match.group(2))
            end_column = _column_index(match.group(4) or match.group(2)) + 1
            start_row = int(match.group(3) or "1") - 1
            if match.group(5):
                end_row = int(match.group(5))
            else:
                end_row = max((index + 1 for index, row in enumerate(self.rows[role])
                               if any(value not in ("", None) for value in row)), default=0)
            values = []
            for row_index in range(start_row, end_row):
                row = self.rows[role][row_index] if row_index < len(self.rows[role]) else []
                values.append(row[start_column:end_column] if row else [])
            result.append({"range": a1_range, "values": values})
        return {"valueRanges": result}

    def batch_update(self, body):
        requests = body["requests"]
        self.batches.append(requests)
        for request in requests:
            operation, value = next(iter(request.items()))
            if operation == "appendDimension":
                role = self._role_for_id(value["sheetId"])
                dimension = value["dimension"]
                if dimension == "ROWS":
                    self.row_count[role] += value["length"]
                else:
                    self.column_count[role] += value["length"]
            elif operation in ("insertDimension", "deleteDimension"):
                span = value["range"]
                role = self._role_for_id(span["sheetId"])
                start, end = span["startIndex"], span["endIndex"]
                if operation == "insertDimension":
                    self.rows[role][start:start] = [[""] * self.column_count[role] for _ in range(end - start)]
                    self.row_count[role] += end - start
                else:
                    del self.rows[role][start:end]
                    self.row_count[role] -= end - start
            elif operation == "updateCells":
                grid = value["range"]
                role = self._role_for_id(grid["sheetId"])
                start_row, start_col = grid["startRowIndex"], grid["startColumnIndex"]
                while len(self.rows[role]) < start_row + len(value["rows"]):
                    self.rows[role].append([""] * self.column_count[role])
                for row_offset, row_data in enumerate(value["rows"]):
                    target_row = self.rows[role][start_row + row_offset]
                    for column_offset, cell_data in enumerate(row_data.get("values", [])):
                        cell = cell_data.get("userEnteredValue")
                        column = start_col + column_offset
                        while len(target_row) <= column:
                            target_row.append("")
                        if cell is None or not cell:
                            target_row[column] = ""
                        else:
                            target_row[column] = next(iter(cell.values()))
            elif operation == "addProtectedRange":
                role = self._role_for_id(value["protectedRange"]["range"]["sheetId"])
                self.protections[role].append({**value["protectedRange"], "protectedRangeId": len(self.protections[role]) + 1})
            elif operation == "updateProtectedRange":
                role = self._role_for_id(value["protectedRange"]["range"]["sheetId"])
                protected_id = value["protectedRange"]["protectedRangeId"]
                index = next(i for i, item in enumerate(self.protections[role])
                             if item["protectedRangeId"] == protected_id)
                self.protections[role][index] = {**value["protectedRange"],
                                                 "protectedRangeId": protected_id}
            elif operation == "deleteProtectedRange":
                protected_id = value["protectedRangeId"]
                for role in self.protections:
                    self.protections[role] = [item for item in self.protections[role]
                                              if item["protectedRangeId"] != protected_id]
            elif operation == "deleteDeveloperMetadata":
                metadata_id = value["dataFilter"]["developerMetadataLookup"]["metadataId"]
                self.metadata = [item for item in self.metadata if item["metadataId"] != metadata_id]
            elif operation == "updateSheetProperties":
                role = self._role_for_id(value["properties"]["sheetId"])
                self.grid_properties[role].update(value["properties"].get("gridProperties", {}))
            elif operation == "updateDimensionProperties":
                dimension = value["range"]["dimension"]
                if dimension == "COLUMNS":
                    role = self._role_for_id(value["range"]["sheetId"])
                    self.column_widths[role][value["range"]["startIndex"]] = value["properties"]["pixelSize"]
            elif operation == "mergeCells":
                role = self._role_for_id(value["range"]["sheetId"])
                self.merges[role].append(dict(value["range"]))
            elif operation == "unmergeCells":
                role = self._role_for_id(value["range"]["sheetId"])
                self.merges[role] = [item for item in self.merges[role]
                                     if item != value["range"]]
            elif operation == "repeatCell":
                area = value["range"]
                role = self._role_for_id(area["sheetId"])
                background = value.get("cell", {}).get("userEnteredFormat", {}).get("backgroundColor")
                if background is not None:
                    for row in range(area.get("startRowIndex", 0), area.get("endRowIndex", len(self.rows[role]))):
                        for column in range(area.get("startColumnIndex", 0),
                                            area.get("endColumnIndex", self.column_count[role])):
                            self.cell_formats[role][(row, column)] = {"backgroundColor": background}
            elif operation in ("updateBorders", "autoResizeDimensions", "setBasicFilter"):
                continue
            else:
                raise AssertionError(f"unexpected batch request: {operation}")
        return {"replies": [{} for _ in requests]}

    def _role_for_id(self, sheet_id):
        return next(role for role, target in self.targets.items() if target.worksheet_id == sheet_id)


def _column_index(letters):
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value - 1


def _target(role, sheet_id, title):
    return CellWorksheetTarget(role, "test-sheet", sheet_id, title,
                               "writer@example.test", ("viewer@example.test",))


def _matrix(role, dates, rows_per_date):
    header_rows = {"summary": 3, "invoice": 2}.get(role, 1)
    width = {"summary": 6, "invoice": 4}.get(role, 3)
    values = [[""] * width for _ in range(header_rows)]
    if role == "summary":
        values[0][:3] = ["REPORT DATE", "SECTION", "KETERANGAN"]
    else:
        values[0][0] = "REPORT DATE"
    date_ranges = []
    for day in dates:
        if role == "invoice" and date_ranges:
            values.extend([[""] * width for _ in range(2)])
        start = len(values) + 1
        for row_index in range(rows_per_date[day]):
            if role == "summary":
                values.append([day, "DETAIL", f"row-{row_index}", 10, 2,
                               f"=F{len(values)}+D{len(values) + 1}-E{len(values) + 1}"])
            else:
                values.append([day, f"row-{row_index}", 10, 2][:width])
        if rows_per_date[day]:
            date_ranges.append({"report_date": day, "start_row": start,
                                "end_row": len(values)})
    headers = values[:header_rows] if role in ("summary", "invoice") else values[0]
    return {"values": values, "date_ranges": date_ranges, "headers": headers}


def _detail_history_matrix(report_date, later_date):
    headers = ["REPORT DATE", "CELL", "NO", "TRANSACTION DATE", "TRANSACTION ID",
               "BASE ID", "TRANSACTION", "KREDIT", "DEBET", "SALDO AWAL",
               "SALDO AKHIR", "NOMOR RS", "REMARKS", "REASON"]

    def row(day, label):
        return [day, label, 1, *([""] * (len(headers) - 3))]

    values = [headers, row(report_date, "replacement-0"), row(report_date, "replacement-1"),
              row(later_date, "later")]
    return {"values": values, "rows": values, "headers": headers}


class StatelessCellDateWriterTests(unittest.TestCase):
    def setUp(self):
        self.targets = {
            "summary": _target("summary", 11, "Summary"),
            "invoice": _target("invoice", 12, "Invoice"),
            "unusual": _target("unusual", 13, "Unusual"),
            "reversal": _target("reversal", 14, "Reversal"),
        }

    def test_rewrites_only_matching_date_blocks_and_batches_roles(self):
        old = "2026-11-13"
        day = "2026-11-14"
        matrices = {
            "summary": _matrix("summary", [old, day], {old: 2, day: 2}),
            "invoice": _matrix("invoice", [old, day], {old: 1, day: 2}),
            "unusual": _matrix("unusual", [old, day], {old: 1, day: 2}),
            "reversal": _matrix("reversal", [old], {old: 1}),
        }
        initial = {
            "summary": matrices["summary"]["values"][:5] + [[day, "DETAIL", "old", 1, 0, 1]],
            "invoice": matrices["invoice"]["values"][:4] + [[day, "old", 1, 0]],
            "unusual": matrices["unusual"]["values"][:2] + [[day, "old", 1]],
            "reversal": matrices["reversal"]["values"],
        }
        spreadsheet = FakeSpreadsheet(self.targets, initial)
        result = publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)

        self.assertEqual(result["report_date"], day)
        self.assertEqual(result["roles"], list(self.targets))
        self.assertEqual(len(spreadsheet.batches), 1)
        self.assertEqual(
            {row[2] for row in spreadsheet.rows["summary"] if len(row) > 2 and row[0] == day},
            {"row-0", "row-1"},
        )
        self.assertEqual(sum(row[0] == old for row in spreadsheet.rows["summary"]), 2)
        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["invoice"]), 2)
        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["unusual"]), 2)
        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["reversal"]), 0)
        self.assertTrue(all(
            any(item.get("description") == "FINPAY CELL REPORT V1:COMPUTED"
                and not item.get("unprotectedRanges") for item in spreadsheet.protections[role])
            for role in self.targets
        ))

    def test_invoice_spacers_are_whitened_in_the_existing_batch_update(self):
        days = ["2026-11-13", "2026-11-14", "2026-11-15"]
        matrices = {
            role: _matrix(role, days, {day: 1 for day in days})
            for role in self.targets
        }
        invoice = matrices["invoice"]
        invoice["style_contract"] = {
            "widths": [100] * len(invoice["values"][0]),
            "frozen_rows": 2,
            "frozen_columns": 0,
            "merges": [],
        }
        initial = {role: [row[:] for row in matrix["values"]]
                   for role, matrix in matrices.items()}
        spreadsheet = FakeSpreadsheet(self.targets, initial)
        spacer_ranges = _invoice_spacer_ranges(invoice, days[1])
        accounting = {"red": 249 / 255, "green": 229 / 255, "blue": 178 / 255}
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        width = len(invoice["values"][0])
        for start, end in spacer_ranges:
            for row in range(start, end):
                for column in range(width):
                    spreadsheet.cell_formats["invoice"][(row, column)] = {
                        "backgroundColor": accounting,
                    }

        def white_spacer_requests(role, target, matrix, patch):
            if role != "invoice":
                return []
            return [{"repeatCell": {
                "range": {"sheetId": target.worksheet_id, "startRowIndex": start,
                          "endRowIndex": end, "startColumnIndex": 0,
                          "endColumnIndex": len(matrix["values"][0])},
                "cell": {"userEnteredFormat": {"backgroundColor": white}},
                "fields": "userEnteredFormat.backgroundColor",
            }} for start, end in patch["spacer_ranges"]]

        with patch("finpay_pipeline.cell_publication_v2._date_format_requests",
                   side_effect=white_spacer_requests):
            result = publish_cell_date_blocks(spreadsheet, self.targets, matrices, days[1])

        self.assertEqual(result["http_write_batches"], 1)
        self.assertEqual(len(spreadsheet.batches), 1)
        for start, end in spacer_ranges:
            for row in range(start, end):
                self.assertEqual(
                    spreadsheet.cell_formats["invoice"][(row, 0)]["backgroundColor"], white,
                )
        self.assertTrue(any(
            request.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("backgroundColor") == white
            for request in spreadsheet.batches[0]
        ))
        self.assertEqual(spreadsheet.grid_data_reads, 1)

    def test_invoice_spacer_griddata_readback_rejects_accounting_fill(self):
        days = ["2026-11-13", "2026-11-14", "2026-11-15"]
        matrices = {
            role: _matrix(role, days, {day: 1 for day in days})
            for role in self.targets
        }
        invoice = matrices["invoice"]
        invoice["style_contract"] = {
            "widths": [100] * len(invoice["values"][0]),
            "frozen_rows": 2,
            "frozen_columns": 0,
            "merges": [],
        }
        spreadsheet = FakeSpreadsheet(
            self.targets,
            {role: [row[:] for row in matrix["values"]]
             for role, matrix in matrices.items()},
        )
        accounting = {"red": 249 / 255, "green": 229 / 255, "blue": 178 / 255}

        def accounting_spacer_requests(role, target, matrix, patch):
            if role != "invoice":
                return []
            return [{"repeatCell": {
                "range": {"sheetId": target.worksheet_id, "startRowIndex": start,
                          "endRowIndex": end, "startColumnIndex": 0,
                          "endColumnIndex": len(matrix["values"][0])},
                "cell": {"userEnteredFormat": {"backgroundColor": accounting}},
                "fields": "userEnteredFormat.backgroundColor",
            }} for start, end in patch["spacer_ranges"]]

        with patch("finpay_pipeline.cell_publication_v2._date_format_requests",
                   side_effect=accounting_spacer_requests):
            with self.assertRaisesRegex(ValueError, "invoice spacer background is not white"):
                publish_cell_date_blocks(spreadsheet, self.targets, matrices, days[1])
        self.assertEqual(spreadsheet.grid_data_reads, 1)

    def test_overflow_batches_keep_spacer_styles_with_a_date_value_write(self):
        value_requests = [
            {"updateCells": {"range": {"sheetId": 12, "startRowIndex": index,
                                        "endRowIndex": index + 1, "startColumnIndex": 0,
                                        "endColumnIndex": 4}, "rows": [], "fields": "userEnteredValue"}}
            for index in (30, 31)
        ]
        spacer_fill = {"repeatCell": {
            "range": {"sheetId": 12, "startRowIndex": 10, "endRowIndex": 12,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}},
            "fields": "userEnteredFormat.backgroundColor",
        }}
        spacer_border = {"updateBorders": {
            "range": {"sheetId": 12, "startRowIndex": 10, "endRowIndex": 12,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "top": {"style": "NONE"}, "bottom": {"style": "NONE"},
        }}
        unrelated_style = {"repeatCell": {
            "range": {"sheetId": 12, "startRowIndex": 30, "endRowIndex": 31,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
            "fields": "userEnteredFormat.horizontalAlignment",
        }}
        paired = [value_requests[0], spacer_fill, spacer_border]
        max_bytes = len(json.dumps({"requests": paired}).encode("utf-8"))

        batches = _bounded_date_write_batches(
            value_requests, [spacer_fill, spacer_border, unrelated_style], [(10, 12)], max_bytes,
        )

        self.assertGreater(len(batches), 1)
        self.assertEqual(batches[0], paired)
        self.assertTrue(any(value_requests[1] in batch for batch in batches))
        self.assertTrue(any(unrelated_style in batch for batch in batches))

    def test_publisher_overflow_keeps_invoice_spacer_fill_with_date_values(self):
        days = ["2026-11-13", "2026-11-14", "2026-11-15"]
        matrices = {
            role: _matrix(role, days, {day: 8 for day in days})
            for role in self.targets
        }
        invoice = matrices["invoice"]
        invoice["style_contract"] = {
            "widths": [100] * len(invoice["values"][0]),
            "frozen_rows": 2,
            "frozen_columns": 0,
            "merges": [],
        }
        spreadsheet = FakeSpreadsheet(
            self.targets,
            {role: [row[:] for row in matrix["values"]]
             for role, matrix in matrices.items()},
        )
        spacer_ranges = _invoice_spacer_ranges(invoice, days[1])
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        width = len(invoice["values"][0])

        def white_spacer_requests(role, target, matrix, patch):
            if role != "invoice":
                return []
            return [{"repeatCell": {
                "range": {"sheetId": target.worksheet_id, "startRowIndex": start,
                          "endRowIndex": end, "startColumnIndex": 0,
                          "endColumnIndex": len(matrix["values"][0])},
                "cell": {"userEnteredFormat": {"backgroundColor": white}},
                "fields": "userEnteredFormat.backgroundColor",
            }} for start, end in patch["spacer_ranges"]]

        date_range = next(item for item in invoice["date_ranges"]
                          if item["report_date"] == days[1])
        date_start = date_range["start_row"] - 1
        date_end = date_range["end_row"]
        with patch("finpay_pipeline.cell_publication.MAX_REQUEST_BYTES", 6000), \
                patch("finpay_pipeline.cell_publication_v2._date_format_requests",
                      side_effect=white_spacer_requests):
            result = publish_cell_date_blocks(
                spreadsheet, self.targets, matrices, days[1], rows_per_request=1,
            )

        self.assertGreater(result["http_write_batches"], 1)
        white_spacer_batches = []
        for batch in spreadsheet.batches:
            has_white_spacer = any(
                request.get("repeatCell", {}).get("range", {}).get("startRowIndex") == start
                and request.get("repeatCell", {}).get("range", {}).get("endRowIndex") == end
                and request.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("backgroundColor") == white
                for request in batch for start, end in spacer_ranges
            )
            has_date_values = any(
                request.get("updateCells", {}).get("range", {}).get("sheetId") == self.targets["invoice"].worksheet_id
                and date_start <= request["updateCells"]["range"].get("startRowIndex", -1) < date_end
                and request["updateCells"]["range"].get("endColumnIndex") == width
                for request in batch
            )
            if has_white_spacer and has_date_values:
                white_spacer_batches.append(batch)
        self.assertTrue(white_spacer_batches)

    def test_missing_date_appends_once_and_repeat_rewrite_does_not_duplicate(self):
        day = "2026-11-14"
        matrices = {
            "summary": _matrix("summary", [day], {day: 1}),
            "invoice": _matrix("invoice", [day], {day: 1}),
            "unusual": _matrix("unusual", [day], {day: 1}),
            "reversal": _matrix("reversal", [day], {day: 0}),
        }
        initial = {
            "summary": matrices["summary"]["values"][:3],
            "invoice": matrices["invoice"]["values"][:2],
            "unusual": matrices["unusual"]["values"][:1],
            "reversal": matrices["reversal"]["values"][:1],
        }
        spreadsheet = FakeSpreadsheet(self.targets, initial)

        publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)
        first_rows = {role: [row[:] for row in spreadsheet.rows[role]] for role in self.targets}
        publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)

        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["summary"]), 1)
        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["invoice"]), 1)
        self.assertEqual(sum(row[0] == day for row in spreadsheet.rows["unusual"]), 1)
        self.assertEqual(spreadsheet.rows, first_rows)

    def test_empty_tabs_receive_summary_opening_and_static_layout(self):
        day = "2026-10-01"
        matrices = {
            role: _matrix(role, [day], {day: 1})
            for role in self.targets
        }
        summary = matrices["summary"]
        summary["values"][2][:6] = [day, "OPENING", "OPENING BALANCE", 0, 0, 1000]
        for role, matrix in matrices.items():
            width = len(matrix["values"][0])
            matrix["style_contract"] = {
                "widths": [100] * width,
                "frozen_rows": 2 if role in ("summary", "invoice") else 1,
                "frozen_columns": 3 if role == "summary" else 0,
                "merges": [[0, 2, 0, 1]] if role == "summary" else [],
            }
        spreadsheet = FakeSpreadsheet(self.targets, {role: [] for role in self.targets})
        for role in self.targets:
            spreadsheet.column_count[role] = 2

        with patch("finpay_pipeline.cell_publication_v2._date_format_requests", return_value=[]):
            publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)

        self.assertEqual(spreadsheet.rows["summary"][0][:3],
                         ["REPORT DATE", "SECTION", "KETERANGAN"])
        self.assertEqual(spreadsheet.rows["summary"][2][:6],
                         [day, "OPENING", "OPENING BALANCE", 0, 0, 1000])
        self.assertEqual(spreadsheet.column_count["summary"], 6)
        self.assertEqual(spreadsheet.grid_properties["summary"]["frozenColumnCount"], 3)
        self.assertEqual(len(spreadsheet.merges["summary"]), 1)
        self.assertEqual(spreadsheet.column_widths["summary"], {index: 100 for index in range(6)})

    def test_detail_history_without_date_ranges_rewrites_first_block_idempotently(self):
        day, later = "2026-10-01", "2026-10-02"
        matrices = {
            role: _matrix(role, [day, later], {day: 1, later: 1})
            for role in self.targets
        }
        matrices["unusual"] = _detail_history_matrix(day, later)
        initial = {role: [row[:] for row in matrix["values"]]
                   for role, matrix in matrices.items()}
        unusual_headers = matrices["unusual"]["headers"]
        unusual_width = len(unusual_headers)
        initial["unusual"] = [
            unusual_headers,
            [day, "old", 1, *([""] * (unusual_width - 3))],
            [later, "keep", 1, *([""] * (unusual_width - 3))],
        ]
        spreadsheet = FakeSpreadsheet(self.targets, initial)

        publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)
        first_rows = [row[:] for row in spreadsheet.rows["unusual"]]
        publish_cell_date_blocks(spreadsheet, self.targets, matrices, day)

        detail_rows = [row for row in spreadsheet.rows["unusual"] if row and row[0] in (day, later)]
        self.assertEqual([row[:2] for row in detail_rows], [
            [day, "replacement-0"], [day, "replacement-1"], [later, "keep"],
        ])
        self.assertEqual(spreadsheet.rows["unusual"], first_rows)


if __name__ == "__main__":
    unittest.main()
