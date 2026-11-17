"""Offline design/formula-request tests; no Excel/Google formula engine is used."""
import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import load_workbook

from finpay_pipeline.cell_design import dummy_cluster_report
from finpay_pipeline.cell_calculation import build_cluster_report
from finpay_pipeline.tests import test_cell_calculation as fixtures
from finpay_pipeline.cell_sheets import (
    column_letter, render_cell_matrix as current_matrix, write_cell_design_workbook,
)


def render_cell_matrix(*args, **kwargs):
    """Keep the published wide-v2 regression contract explicit."""
    return current_matrix(*args, **kwargs, presentation_version=2)


class CellSheetDesignTests(unittest.TestCase):
    def setUp(self):
        self.report = dummy_cluster_report("412110_B", ("412111", "412112"))

    def block(self, matrix, code):
        return next(item for item in matrix["blocks"] if item["cell_code"] == code)

    def row(self, matrix, day, key):
        start = next(item["start_row"] for item in matrix["date_ranges"] if item["report_date"] == day)
        return next(index + 1 for index, value in enumerate(matrix["row_keys"])
                    if index + 1 >= start and value == key)

    def test_two_cell_layout_is_twenty_columns_with_identical_date_rows(self):
        matrix = render_cell_matrix(self.report, dummy=True)
        self.assertEqual(len(matrix["values"][0]), 20)
        self.assertEqual([(block["start_column"], block["end_column"]) for block in matrix["blocks"]],
                         [(1, 6), (8, 13), (15, 20)])
        self.assertIn("412111", matrix["values"][1][7])
        self.assertIn("412112", matrix["values"][1][14])
        self.assertEqual(matrix["values"][0][0], "REPORT DATE")
        self.assertEqual(matrix["layout_version"], 2)
        self.assertNotIn("PUBLICATION", [value for row in matrix["values"] for value in row])
        for row in matrix["values"][2:]:
            self.assertEqual(row[0], row[7])
            self.assertEqual(row[0], row[14])
            self.assertEqual(row[2], row[9])
            self.assertEqual(row[2], row[16])
            self.assertEqual(row[6], "")
            self.assertEqual(row[13], "")

    def test_three_cell_layout_and_column_names(self):
        report = dummy_cluster_report("422040_B", ("422041", "422042", "422043"))
        matrix = render_cell_matrix(report, dummy=True)
        self.assertEqual(len(matrix["values"][0]), 27)
        self.assertEqual(matrix["blocks"][-1]["end_column"], 27)
        self.assertEqual([column_letter(number) for number in (11, 18, 25, 27)], ["K", "R", "Y", "AA"])

    def test_all_numeric_detail_saldos_equal_sum_of_cells(self):
        matrix = render_cell_matrix(self.report, dummy=True)
        for key, row in zip(matrix["row_keys"], matrix["numeric_preview"]):
            if type(row[5]) is int:
                self.assertEqual(row[5], row[12] + row[19], key)
        self.assertEqual(matrix["values"][2][5], "=SUM(M3,T3)")

    def test_formulas_reference_correct_cell_columns_not_legacy_d(self):
        matrix = current_matrix(self.report, dummy=True, presentation_version=5)
        row = self.row(matrix, "2026-10-13", "CASH:cash:MANDIRI")
        for code in ("412111", "412112"):
            block = self.block(matrix, code)
            formula = matrix["values"][row - 1][block["start_column"] - 1]
            credit_col = column_letter(block["start_column"] + 1)
            self.assertIn("SUMIFS", formula)
            self.assertIn(f"{credit_col}:{credit_col}", formula)
            self.assertIn('"2026-10-14"', formula)
            self.assertIn('"CASHOUT APOLLO"', formula)
        detail = next(index + 1 for index, key in enumerate(matrix["row_keys"])
                      if key.startswith("DETAIL:") and matrix["values"][index][0] == "2026-10-13")
        saldo_col = self.block(matrix, "412111")["start_column"] + 1
        self.assertTrue(matrix["values"][detail - 1][saldo_col].startswith("=1000000+"))
        next_detail = next(index + 1 for index, key in enumerate(matrix["row_keys"])
                           if key.startswith("DETAIL:") and matrix["values"][index][0] == "2026-10-14")
        self.assertIn("SUMIFS", matrix["values"][next_detail - 1][saldo_col])
        self.assertEqual(matrix["editable_inputs"], [])

    def test_latest_day_bank_default_is_pending_not_zero(self):
        matrix = current_matrix(self.report, dummy=True, presentation_version=5)
        row = self.row(matrix, "2026-10-15", "CASH:cash:MANDIRI")
        block = self.block(matrix, "412111")
        formula = matrix["values"][row - 1][block["start_column"] - 1]
        self.assertIn('"PENDING NEXT DAY"', formula)
        self.assertEqual(matrix["numeric_preview"][row - 1][block["start_column"] - 1],
                         "PENDING NEXT DAY")
        self.assertEqual(matrix["editable_inputs"], [])

    def test_no_invoice_linkaja_or_legacy_panels(self):
        matrix = render_cell_matrix(self.report, dummy=True)
        text = str(matrix["values"]).upper()
        for forbidden in ("INVOICE", "LINKAJA", "CASH-IN", "CASH-OUT"):
            self.assertNotIn(forbidden, text)

    def test_combined_negative_mandiri_is_split_after_netting_cells(self):
        fixture = fixtures.CellCalculationTests()
        fixture.setUp()
        one = fixture.prepare("412111", [fixture.row(amount=0, debit=100)])
        two = fixture.prepare("412112", [fixture.row(amount=20)])
        report = build_cluster_report(fixture.config, [one, two], "2026-10-13")
        matrix = render_cell_matrix(report, dummy=True)
        row = matrix["row_keys"].index("MANDIRI:net:ST")
        self.assertEqual(matrix["numeric_preview"][row][3:5], [0, 80])
        self.assertEqual(matrix["numeric_preview"][row][10], -100)
        self.assertEqual(matrix["numeric_preview"][row][17], 20)
        self.assertEqual(matrix["values"][row][3], f"=MAX(SUM(K{row + 1},R{row + 1}),0)")
        self.assertEqual(matrix["values"][row][4], f"=MAX(-SUM(K{row + 1},R{row + 1}),0)")

    def test_st_formula_includes_separate_sls_hold_once(self):
        fixture = fixtures.CellCalculationTests()
        fixture.setUp()
        rows = [
            fixture.row(label="SELLTHRU", identifier="FAMILY", amount=10_000,
                        remarks="Sellthru Sales Fee"),
            fixture.row(label="SELLTHRUFEE", identifier="FAMILYFEE", amount=0, debit=100,
                        remarks="Platform fee sellthru rp. 100,-"),
            fixture.row(label="SELLTHRUSALESHOLD", identifier="FAMILYSLSHLD", amount=0, debit=20,
                        remarks="Sales hold transaksi sellthru sejumlah 20 rupiah, dari 412110"),
        ]
        facts = [fixture.prepare("412111", rows), fixture.prepare("412112", [])]
        report = build_cluster_report(fixture.config, facts, "2026-10-13")
        matrix = render_cell_matrix(report, dummy=True)
        st_row = matrix["row_keys"].index("MANDIRI:net:ST")
        hold_row = self.row(matrix, "2026-10-13", "DETAIL:SELLTHRUSALESFEE")
        categories = ("SELLTHRU", "SELLTHRUFEE", "SELLTHRUSALESFEE")
        expected = "=" + "+".join(
            f"(K{self.row(matrix, '2026-10-13', 'DETAIL:' + category)}-"
            f"L{self.row(matrix, '2026-10-13', 'DETAIL:' + category)})"
            for category in categories
        )
        self.assertEqual(matrix["values"][st_row][10], expected)
        self.assertEqual(expected.count(f"K{hold_row}"), 1)
        self.assertEqual(matrix["numeric_preview"][st_row][10], 9_880)

    def test_numeric_preview_cash_difference_sign_matches_finance_layout(self):
        matrix = current_matrix(self.report, dummy=True, presentation_version=5)
        row = self.row(matrix, "2026-10-13", "CASH:cash:SELISIH")
        mandiri = self.row(matrix, "2026-10-13", "CASH:cash:MANDIRI")
        running = self.row(matrix, "2026-10-13", "CASH:cash:RUNNING TOTAL")
        block = self.block(matrix, "412111")
        column = block["start_column"] - 1
        mandiri_value = matrix["numeric_preview"][mandiri - 1][column]
        if mandiri_value:
            expected = mandiri_value - matrix["numeric_preview"][running - 1][column]
            self.assertEqual(matrix["numeric_preview"][row - 1][column], expected)
        else:
            self.assertEqual(matrix["numeric_preview"][row - 1][column], "")
        self.assertTrue(matrix["values"][row - 1][column].startswith("=IF"))

    def test_no_category_day_is_zero_movement_and_carries_prior_saldo(self):
        matrix = render_cell_matrix(self.report, dummy=True)
        last = self.row(matrix, "2026-10-13", "DETAIL:SELLTHRUFEE")
        empty = self.row(matrix, "2026-10-14", "DETAIL:SELLTHRUFEE")
        self.assertEqual(matrix["numeric_preview"][empty - 1][19], matrix["numeric_preview"][last - 1][19])

    def test_bad_coverage_manual_or_float_amount_cannot_render(self):
        changed = copy.deepcopy(self.report)
        changed["days"].pop(1)
        with self.assertRaises(ValueError):
            render_cell_matrix(changed)
        with self.assertRaises(ValueError):
            render_cell_matrix(self.report, manual_values={("2026-10-13", "412111"): 12.5})
        with self.assertRaises(ValueError):
            render_cell_matrix(self.report, reset_dates={"2026-10-12"})

    def test_local_workbook_has_formulas_preview_and_no_editable_inputs(self):
        matrix = render_cell_matrix(self.report, dummy=True)
        self.assertEqual(matrix["editable_inputs"], [])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dummy.xlsx"
            write_cell_design_workbook(path, [matrix])
            workbook = load_workbook(path, data_only=False)
            self.assertEqual(len(workbook.worksheets), 3)
            formulas, preview = workbook["412110"], workbook["412110 VALUES"]
            self.assertTrue(formulas.protection.sheet)
            self.assertEqual(formulas.freeze_panes, "D2")
            self.assertEqual(formulas["F3"].data_type, "f")
            self.assertEqual(preview["F3"].value, 3000000)
            for merged in formulas.merged_cells.ranges:
                self.assertFalse(merged.min_col <= 3 < merged.max_col)
                self.assertEqual(merged.min_row, 2)
            for column in (7, 14):
                for row in (1, 2, 3, 10):
                    cell = formulas.cell(row, column)
                    self.assertIsNone(cell.value)
                    self.assertEqual(cell.fill.fgColor.rgb, "00FFFFFF")
                    self.assertIsNone(getattr(cell.border.bottom, "style", None))
            self.assertEqual(formulas["F3"].border.right.style, "medium")
            self.assertTrue(formulas.protection.sheet)
            workbook.close()
        with self.assertRaises(ValueError):
            write_cell_design_workbook("ignored.xlsx", [render_cell_matrix(self.report)])


class CompactSheetDesignTests(unittest.TestCase):
    def test_capacity_only_grows_and_keeps_a_scroll_buffer(self):
        from finpay_pipeline.cell_sheets import cell_grid_capacity
        self.assertEqual(cell_grid_capacity(99, 14, {"rowCount": 200, "columnCount": 14}),
                         {"rowCount": 200, "columnCount": 100})
        self.assertEqual(cell_grid_capacity(1200, 18, {"rowCount": 1000, "columnCount": 120}),
                         {"rowCount": 1400, "columnCount": 120})
        self.assertEqual(cell_grid_capacity(1200, 18, {"rowCount": 1200, "columnCount": 100}),
                         {"rowCount": 1200, "columnCount": 100})

    def test_shared_details_and_exact_numeric_projection(self):
        report = dummy_cluster_report("412110_B", ("412111", "412112"))
        wide = current_matrix(report, dummy=True, presentation_version=2)
        compact = current_matrix(report, dummy=True)
        self.assertEqual(compact["layout_version"], 3)
        self.assertEqual(len(compact["values"][0]), 14)
        self.assertEqual(compact["values"][1][3:6], ["DEBET", "KREDIT", "SALDO"])
        self.assertEqual(compact["values"][2][5], "=SUM(J3,N3)")
        for old, new in zip(wide["numeric_preview"][2:], compact["numeric_preview"][2:]):
            self.assertEqual(old[:6], new[:6])
            self.assertEqual(old[10:13], new[7:10])
            self.assertEqual(old[17:20], new[11:14])
            self.assertEqual(new.count(new[0]), 1)
            self.assertEqual(new[6], "")
            self.assertEqual(new[10], "")
        self.assertEqual(compact["editable_inputs"], [])

    def test_three_cell_layout_has_eighteen_columns_and_reference_palette(self):
        report = dummy_cluster_report("422040_B", ("422041", "422042", "422043"))
        matrix = current_matrix(report, dummy=True)
        self.assertEqual(len(matrix["values"][0]), 18)
        self.assertEqual([b["start_column"] for b in matrix["blocks"]], [4, 8, 12, 16])
        self.assertEqual(matrix["format_palette"]["header"], "1F4E78")
        self.assertEqual(matrix["format_palette"]["cash"], "F6EDFC")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "compact.xlsx"
            write_cell_design_workbook(path, [matrix])
            book = load_workbook(path, data_only=False)
            sheet = book["422040"]
            self.assertEqual(sheet.freeze_panes, "D3")
            self.assertEqual({str(area) for area in sheet.merged_cells.ranges},
                             {"A1:A2", "B1:B2", "C1:C2", "D1:F1", "H1:J1", "L1:N1", "P1:R1"})
            self.assertEqual(sheet["D1"].alignment.horizontal, "center")
            self.assertEqual(sheet["D4"].alignment.horizontal, "right")
            self.assertEqual(sheet["C4"].alignment.horizontal, "left")
            self.assertEqual(sheet["D1"].font.color.rgb, "00FFFFFF")
            self.assertEqual(sheet["D4"].font.color.rgb, "00000000")
            self.assertEqual(sheet["B4"].font.color.rgb, "00000000")
            for section, color in (("MANDIRI:", "00E5EFF9"), ("CASH:", "00F6EDFC")):
                row = next(i + 1 for i, key in enumerate(matrix["row_keys"]) if key.startswith(section))
                self.assertEqual(sheet.cell(row, 4).fill.fgColor.rgb, color)
            book.close()

    def test_reference_section_borders_and_description_padding(self):
        from finpay_pipeline.cell_publication import CellSheetTarget, _format_requests
        report = dummy_cluster_report("412110_B", ("412111", "412112"))
        matrix = current_matrix(report, dummy=True)
        target = CellSheetTarget("dummy", 7, "writer@example.test", ("writer@example.test",))
        requests = _format_requests(target, matrix)
        width = next(item["updateDimensionProperties"]["properties"]["pixelSize"] for item in requests
                     if item.get("updateDimensionProperties", {}).get("range", {}).get("startIndex") == 2)
        self.assertEqual(width, 430)
        self.assertTrue(any(item.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("wrapStrategy") == "CLIP"
                            for item in requests))
        for section in ("DETAIL:", "MANDIRI:", "CASH:"):
            row = next(i for i, key in enumerate(matrix["row_keys"]) if key.startswith(section))
            strong = [item["updateBorders"] for item in requests if item.get("updateBorders", {}).get("top", {}).get("style") == "SOLID_MEDIUM"
                      and item["updateBorders"]["range"]["startRowIndex"] == row]
            self.assertEqual(bool(strong), section != "CASH:")
        for label in ("MANDIRI:net:Total", "CASH:cash:SELISIH"):
            row = matrix["row_keys"].index(label)
            strong = [item["updateBorders"]["range"] for item in requests
                      if item.get("updateBorders", {}).get("top", {}).get("style") == "SOLID_MEDIUM"
                      and item["updateBorders"]["range"]["startRowIndex"] == row]
            self.assertEqual([(g["startColumnIndex"], g["endColumnIndex"]) for g in strong], [(3, 4), (7, 8), (11, 12)])


    def test_v4_removes_only_aggregate_and_preserves_each_cell_numeric_projection(self):
        for account, codes in (("412110_B", ("412111", "412112")),
                               ("422040_B", ("422041", "422042", "422043"))):
            report = dummy_cluster_report(account, codes)
            old = current_matrix(report, dummy=True)
            new = current_matrix(report, dummy=True, presentation_version=4)
            self.assertEqual(new["layout_version"], 4)
            self.assertEqual([block["cell_code"] for block in new["blocks"]], list(codes))
            self.assertEqual([block["start_column"] for block in new["blocks"]], [4, 8, 12][:len(codes)])
            self.assertEqual(len(new["values"][0]), 4 * len(codes) + 2)
            for before, after in zip(old["numeric_preview"], new["numeric_preview"]):
                self.assertEqual(before[:3], after[:3])
                for index in range(len(codes)):
                    self.assertEqual(before[7 + index * 4:10 + index * 4], after[3 + index * 4:6 + index * 4])
            self.assertEqual(new["editable_inputs"], [])
            with TemporaryDirectory() as directory:
                path = Path(directory) / "cell-only.xlsx"
                write_cell_design_workbook(path, [new])
                book = load_workbook(path, data_only=False)
                self.assertEqual(book.worksheets[0]["B6"].value, len(codes))
                self.assertEqual(book[account[:-2]].freeze_panes, "D3")
                self.assertEqual(book[account[:-2]].max_column, 4 * len(codes) + 2)
                self.assertTrue(all("TOTAL" not in str(cell.value).upper()
                                    for cell in book[account[:-2]][1]))
                book.close()

    def test_dummy_generator_explicit_v4_keeps_full_cell_count(self):
        from finpay_pipeline.cell_design import generate_dummy_cell_design
        with TemporaryDirectory() as directory:
            result = generate_dummy_cell_design(directory, presentation_version=4)
            self.assertEqual((result["clusters"], result["cells"]), (8, 18))

    def test_v4_titles_match_wide_title_columns_for_two_and_three_cells(self):
        for account, codes in (("412110_B", ("412111", "412112")),
                               ("422040_B", ("422041", "422042", "422043"))):
            for dummy in (False, True):
                matrix = current_matrix(dummy_cluster_report(account, codes), dummy=dummy, presentation_version=4)
                expected = [("DUMMY | " if dummy else "") + f"C{index} | CELL {code}"
                            for index, code in enumerate(codes, 1)]
                for kind in ("values", "numeric_preview"):
                    self.assertEqual([matrix[kind][0][block["start_column"] - 1]
                                      for block in matrix["blocks"]], expected)


    def test_v5_bank_inputs_stay_debet_but_reference_next_day_credit(self):
        report = dummy_cluster_report("412110_B", ("412111", "412112"))
        matrix = current_matrix(report, dummy=True, presentation_version=5)
        for day in report["days"]:
            row = next(index + 1 for index, key in enumerate(matrix["row_keys"])
                       if key == "CASH:cash:MANDIRI"
                       and matrix["values"][index][0] == day["report_date"])
            for code in ("412111", "412112"):
                block = next(item for item in matrix["blocks"] if item["cell_code"] == code)
                formula = matrix["values"][row - 1][block["start_column"] - 1]
                self.assertIn("SUMIFS", formula)
                self.assertIn('"CASHOUT APOLLO"', formula)
                if day["report_date"] != report["days"][-1]["report_date"]:
                    self.assertIn('"' + (report["days"][report["days"].index(day) + 1]["report_date"]) + '"', formula)
                else:
                    self.assertIn('"PENDING NEXT DAY"', formula)


if __name__ == "__main__":
    unittest.main()
