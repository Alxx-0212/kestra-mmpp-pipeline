"""Offline per-cell FinPay invoice panels and cutover regressions."""
import unittest

from finpay_pipeline.cell_calculation import CellReportConfig
from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.cell_invoice_sheets import (
    INVOICE_COLORS, SALES_FEE_LABEL, build_cell_invoice_matrix, invoice_format_requests,
)


class CellInvoiceSheetTests(unittest.TestCase):
    def setUp(self):
        self.config = CellReportConfig(
            "412110_B", ("412111", "412112"), "2026-08-31",
            "2026-08-31T00:00:00+08:00", {"412111": 1_000_000, "412112": 2_000_000},
            "SYNTHETIC-v2",
        )

    def fact(self, cell_code, day, measures=(), qris=()):
        identity = next(cell.to_dict() for cell in self.config.cells if cell.cell_code == cell_code)
        summary = [{"Transaction": label, "Sum_of_Kredit": debet, "Sum_of_Debet": kredit,
                    "Transaction_Count": count}
                   for label, debet, kredit, count in measures]
        calculation = {"report_date": day, "summary": summary, "qris": list(qris)}
        config_id = fingerprint(self.config.to_dict())
        generation_id = fingerprint({"cell_code": cell_code, "date": day})
        calculation_id = fingerprint({"generation_id": generation_id, "config_id": config_id,
                                       "rules_version": self.config.rules_version,
                                       "calculation": calculation})
        return {"identity": identity, "report_date": day, "config_id": config_id,
                "rules_version": self.config.rules_version, "generation_id": generation_id,
                "calculation_id": calculation_id, "calculation": calculation}

    def facts(self, day, first=(), second=(), first_qris=(), second_qris=()):
        return [self.fact("412111", day, first, first_qris),
                self.fact("412112", day, second, second_qris)]

    @staticmethod
    def index(matrix, label):
        return next(i for i, row in enumerate(matrix["values"]) if row[1] == label)

    def test_invoice_is_finpay_only_and_has_cash_sellthru_accounting(self):
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01", first_qris=[
            {"Disbursement Date": "04/06/2026", "Kredit": 15}]))
        labels = [row[1] for row in matrix["values"]]
        for label in ("CASH IN - NGRS", "NGRS", "Recharge Fee", "SELLTHRU", "ST",
                      "BIAYA FEE ST", "ACCOUNTING", "QRISDUWIT", "QRISDUWIT - 04/06/2026"):
            self.assertIn(label, labels)
        self.assertNotIn(SALES_FEE_LABEL, labels)
        self.assertFalse(any("LINKAJA" in value for row in matrix["values"] for value in row if isinstance(value, str)))
        self.assertEqual(matrix["scope_note"],
                         "FinPay-only invoice; LinkAja fees and separate LinkAja workflow are excluded.")

    def test_invoice_panels_have_four_columns_and_spacers_for_two_or_three_cells(self):
        two = build_cell_invoice_matrix(self.config, self.facts("2026-10-01"))
        self.assertEqual(len(two["values"][0]), 11)
        self.assertEqual([panel["start_column"] for panel in two["panels"]], [1, 8])
        self.assertEqual(two["values"][0][0], "C1 | CELL 412111")
        self.assertEqual(two["values"][1][:4], ["REPORT DATE", "INVOICE REPORT", "DEBET", "KREDIT"])
        self.assertEqual(two["values"][0][4], "")
        three_config = CellReportConfig("422040_B", ("422041", "422042", "422043"),
            "2026-10-01", "2026-10-01T00:00:00+08:00",
            {"422041": 1000, "422042": 2000, "422043": 3000}, "DUMMY-v2")
        cfg_hash = fingerprint(three_config.to_dict())
        three_facts = []
        for cell in three_config.cells:
            calc = {"report_date": "2026-10-01", "summary": [], "qris": []}
            generation = fingerprint(cell.to_dict())
            three_facts.append({"identity": cell.to_dict(), "report_date": "2026-10-01",
                "config_id": cfg_hash, "rules_version": three_config.rules_version,
                "generation_id": generation,
                "calculation_id": fingerprint({"generation_id": generation, "config_id": cfg_hash,
                    "rules_version": three_config.rules_version, "calculation": calc}),
                "calculation": calc})
        three = build_cell_invoice_matrix(three_config, three_facts)
        self.assertEqual(len(three["values"][0]), 18)
        self.assertEqual([panel["start_column"] for panel in three["panels"]], [1, 8, 15])
        self.assertTrue(all(three["values"][0][column] == "" for column in (4, 5, 6, 11, 12, 13)))

    def test_qris_union_is_sorted_aligned_and_preserves_invalid_or_missing_dates(self):
        q1 = [{"Disbursement Date": "05/06/2026", "Kredit": 100},
              {"Disbursement Date": "04/06/2026", "Kredit": 40},
              {"Disbursement Date": "invalid", "Kredit": 7},
              {"Disbursement Date": "04/06/2026garbage", "Kredit": 9},
              {"Disbursement Date": "", "Kredit": 5}]
        q2 = [{"Disbursement Date": "04-06-2026", "Kredit": 60}]
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01", first_qris=q1, second_qris=q2))
        first = self.index(matrix, "QRISDUWIT - 04/06/2026")
        self.assertEqual(matrix["values"][first][2:4], [40, ""])
        self.assertEqual(matrix["values"][first][9:11], [60, ""])
        self.assertEqual(matrix["values"][self.index(matrix, "QRISDUWIT - 04/06/2026garbage")][2], 9)
        later = self.index(matrix, "QRISDUWIT - 05/06/2026")
        self.assertEqual(matrix["values"][later][2], 100)
        self.assertEqual(matrix["values"][later][9], 0)
        self.assertLess(self.index(matrix, "QRISDUWIT - invalid"),
                        self.index(matrix, "QRISDUWIT - MISSING DISBURSEMENT DATE"))

    def test_slsfee_cutover_and_october_hold_invoice_only(self):
        fee = (("SELLTHRUSALESFEE", 0, 100, 1),)
        before = build_cell_invoice_matrix(self.config, self.facts("2026-08-31", first=fee))
        september_first = build_cell_invoice_matrix(self.config, self.facts("2026-09-01", first=fee))
        september_last = build_cell_invoice_matrix(self.config, self.facts("2026-09-30", first=fee))
        october = build_cell_invoice_matrix(self.config, self.facts("2026-10-01", first=fee))
        self.assertIn(SALES_FEE_LABEL, [row[1] for row in before["values"]])
        self.assertNotIn(SALES_FEE_LABEL, [row[1] for row in september_first["values"]])
        self.assertNotIn(SALES_FEE_LABEL, [row[1] for row in september_last["values"]])
        index = self.index(october, SALES_FEE_LABEL)
        self.assertEqual(october["values"][index][2:4], [0, 100])
        self.assertEqual(october["values"][index][9:11], [0, 0])

    def test_reversal_with_slsfee_suffix_is_not_an_invoice_fee(self):
        # Invoice projection consumes processed category, not a raw ID suffix.
        reversal = (("REVERSAL", 0, 100, 1),)
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01", first=reversal))
        self.assertNotIn(SALES_FEE_LABEL, [row[1] for row in matrix["values"]])

    def test_rejects_incomplete_identity_config_or_unvalidated_summary(self):
        incomplete = self.facts("2026-10-01")[:1]
        with self.assertRaisesRegex(ValueError, "incomplete cell coverage"):
            build_cell_invoice_matrix(self.config, incomplete)
        invalid = self.facts("2026-10-01")
        invalid[0]["identity"]["cell_code"] = "999999"
        with self.assertRaisesRegex(ValueError, "invalid cell identity"):
            build_cell_invoice_matrix(self.config, invalid)
        invalid = self.facts("2026-10-01")
        invalid[0]["calculation"]["summary"] = [{"Transaction": "RECHARGE", "Sum_of_Kredit": 1.5,
            "Sum_of_Debet": 0, "Transaction_Count": 1}]
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            build_cell_invoice_matrix(self.config, invalid)

    def test_invoice_formatting_uses_separate_section_palette_and_spacer(self):
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01"))
        requests = invoice_format_requests(9, matrix)
        props = next(item["updateSheetProperties"]["properties"] for item in requests
                     if "updateSheetProperties" in item)
        self.assertEqual(props["gridProperties"]["frozenRowCount"], 2)
        self.assertEqual(props["gridProperties"].get("frozenColumnCount", 0), 0)
        self.assertTrue(any(item.get("setBasicFilter", {}).get("filter", {}).get("range", {}).get("sheetId") == 9
                            for item in requests))
        cash = next(i for i, section in enumerate(matrix["row_sections"]) if section == "CASH IN - NGRS")
        cash_format = next(item["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"]
                            for item in requests if item.get("repeatCell", {}).get("range", {}).get("startRowIndex") == cash
                            and item["repeatCell"]["range"].get("endRowIndex") == cash + 1
                            and item["repeatCell"]["range"].get("endColumnIndex") == len(matrix["values"][0]))
        self.assertEqual(cash_format, INVOICE_COLORS["cash_header"])
        self.assertTrue(any(item.get("updateBorders", {}).get("right", {}).get("style") == "SOLID_MEDIUM"
                            for item in requests))
        for section, color in (("SELLTHRU", INVOICE_COLORS["sellthru_header"]),
                               ("ACCOUNTING", INVOICE_COLORS["accounting_header"])):
            index = next(i for i, row in enumerate(matrix["values"]) if row[1] == section)
            request = next(item["repeatCell"] for item in requests
                            if item.get("repeatCell", {}).get("range", {}).get("startRowIndex") == index
                            and item["repeatCell"]["range"].get("endRowIndex") == index + 1
                           and item["repeatCell"]["range"].get("endColumnIndex") == len(matrix["values"][0])
                           and item["repeatCell"]["cell"].get("userEnteredFormat", {}).get("backgroundColor"))
            self.assertEqual(request["cell"]["userEnteredFormat"]["backgroundColor"], color)
        self.assertTrue(any(item.get("updateDimensionProperties", {}).get("properties", {}).get("pixelSize") == 24
                            for item in requests))


    def test_sections_are_contiguous_with_two_separator_rows_between_days(self):
        matrix = build_cell_invoice_matrix(self.config,
            self.facts("2026-08-31") + self.facts("2026-09-01"))
        empty = [index for index, row in enumerate(matrix["values"][2:], 2) if not any(row)]
        self.assertEqual(len(empty), 2)
        self.assertNotEqual(empty[0], len(matrix["values"]) - 1)
        ranges = matrix["section_ranges"]
        self.assertEqual(len(ranges), 6)
        for first, second in zip(ranges, ranges[1:]):
            self.assertEqual(second["start_row"] - first["end_row"],
                             1 if first["report_date"] == second["report_date"] else 3)

    def test_panel_rails_cover_both_blank_rows_and_qris_has_reference_subtitle(self):
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-08-31", first_qris=[
            {"Disbursement Date": "2026-08-30", "Kredit": 10}]) + self.facts("2026-09-01"))
        requests = invoice_format_requests(9, matrix)
        for panel in matrix["panels"]:
            self.assertTrue(any(item.get("updateBorders", {}).get("range") == {
                "sheetId": 9, "startRowIndex": 0, "endRowIndex": len(matrix["values"]),
                "startColumnIndex": panel["start_column"] - 1, "endColumnIndex": panel["end_column"]}
                and item["updateBorders"].get("left", {}).get("style") == "SOLID_MEDIUM"
                and item["updateBorders"].get("right", {}).get("style") == "SOLID_MEDIUM" for item in requests))
        self.assertEqual(matrix["values"][matrix["qris_subtitle_rows"][0] - 1][1], "QRISDUWIT")
        self.assertTrue(matrix["qris_child_rows"])

    def test_final_section_title_alignment_is_not_overwritten_by_body_rules(self):
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01"))
        requests = invoice_format_requests(9, matrix)

        def alignment(row, column):
            result = None
            for request in requests:
                item = request.get("repeatCell", {})
                area = item.get("range", {})
                if (area.get("startRowIndex", 0) <= row < area.get("endRowIndex", 0)
                    and area.get("startColumnIndex", 0) <= column < area.get("endColumnIndex", 0)):
                    fmt = item["cell"]["userEnteredFormat"]
                    if item["fields"] == "userEnteredFormat":
                        result = fmt.get("horizontalAlignment")
                    elif "horizontalAlignment" in fmt:
                        result = fmt["horizontalAlignment"]
            return result

        for section in matrix["section_ranges"]:
            for panel in matrix["panels"]:
                row, column = section["start_row"] - 1, panel["start_column"]
                self.assertEqual(alignment(row, column), "CENTER")
                self.assertEqual(alignment(row + 1, column), "LEFT")
                self.assertEqual(alignment(row + 1, column + 1), "RIGHT")
        self.assertEqual(INVOICE_COLORS["cash_body"],
                         {"red": 235 / 255, "green": 248 / 255, "blue": 232 / 255})


    def test_frozen_cell_titles_are_merged_with_wider_date_and_three_column_gaps(self):
        matrix = build_cell_invoice_matrix(self.config, self.facts("2026-10-01"))
        requests = invoice_format_requests(9, matrix)
        merges = [item["mergeCells"]["range"] for item in requests if "mergeCells" in item]
        self.assertEqual([(item["startRowIndex"], item["endRowIndex"], item["startColumnIndex"], item["endColumnIndex"])
                          for item in merges], [(0, 1, 0, 4), (0, 1, 7, 11)])
        widths = [item["updateDimensionProperties"] for item in requests if "updateDimensionProperties" in item]
        self.assertTrue(any(item["range"]["startIndex"] == 0 and item["properties"]["pixelSize"] == 140 for item in widths))
        self.assertTrue(any(item["range"]["startIndex"] == 4 and item["range"]["endIndex"] == 7 for item in widths))


if __name__ == "__main__":
    unittest.main()
