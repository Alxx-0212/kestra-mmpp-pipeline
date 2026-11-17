import re
import unittest

from finpay_pipeline.cell_publication import CellWorksheetTarget, publish_cell_date_blocks


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

    def fetch_sheet_metadata(self, params=None):
        sheets = []
        for role, target in self.targets.items():
            sheets.append({
                "properties": {
                    "sheetId": target.worksheet_id,
                    "title": target.title,
                    "gridProperties": {
                        "rowCount": self.row_count[role],
                        "columnCount": self.column_count[role],
                    },
                },
                "protectedRanges": list(self.protections[role]),
            })
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
                self.row_count[self._role_for_id(value["sheetId"])] += value["length"]
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
            elif operation in ("updateSheetProperties", "repeatCell", "updateBorders",
                               "updateDimensionProperties", "autoResizeDimensions"):
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
    return {"values": values, "date_ranges": date_ranges,
            "headers": values[:header_rows]}


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


if __name__ == "__main__":
    unittest.main()
