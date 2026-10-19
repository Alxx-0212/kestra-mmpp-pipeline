"""Synthetic Reversal/Unusual detail rows preserve cell lineage and schemas."""
import unittest

from finpay_pipeline.cell_calculation import CellReportConfig
from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.cell_detail_sheets import (
    REVERSAL_HEADERS, UNUSUAL_HEADERS, build_cell_detail_history, build_cell_detail_matrix,
    detail_format_requests,
)


class CellDetailSheetTests(unittest.TestCase):
    def setUp(self):
        self.config = CellReportConfig("412110_B", ("412111", "412112"), "2026-10-13",
            "2026-10-13T00:00:00+08:00", {"412111": 1000, "412112": 2000}, "TEST-v2")

    def fact(self, code, unusual=(), reversal=()):
        identity = next(cell.to_dict() for cell in self.config.cells if cell.cell_code == code)
        calc = {"report_date": "2026-10-13", "summary": [], "unusual": list(unusual), "reversal": list(reversal)}
        config_id = fingerprint(self.config.to_dict())
        generation = fingerprint({"cell": code})
        calculation = fingerprint({"generation_id": generation, "config_id": config_id,
                                   "rules_version": self.config.rules_version, "calculation": calc})
        return {"identity": identity, "report_date": "2026-10-13", "config_id": config_id,
                "rules_version": self.config.rules_version, "generation_id": generation,
                "calculation_id": calculation, "calculation": calc}

    @staticmethod
    def row(identifier, hour=12):
        return {"Transaction Date": f"2026-10-13 {hour:02d}:00:00", "Transaction ID": identifier,
                "Transaction Type": "Debit", "processed_transaction_label": "reversal - ngrs",
                "Kredit": 0, "Debet": 10, "Saldo Awal": 100, "Saldo Akhir": 90,
                "Nomor RS": "-", "Remarks": " literal =1+1 ", "base_id": identifier,
                "unusual_reason": "reason retained"}

    def test_unusual_and_reversal_add_cell_after_report_date_and_keep_existing_fields(self):
        facts = [self.fact("412111", unusual=[self.row("SAME-ID")], reversal=[self.row("REV-A")]),
                 self.fact("412112", unusual=[self.row("SAME-ID", 13)], reversal=[self.row("REV-B", 13)])]
        unusual = build_cell_detail_matrix(self.config, facts, "2026-10-13", "unusual")
        reversal = build_cell_detail_matrix(self.config, facts, "2026-10-13", "reversal")
        self.assertEqual(unusual["headers"], UNUSUAL_HEADERS)
        self.assertEqual(reversal["headers"], REVERSAL_HEADERS)
        self.assertEqual(UNUSUAL_HEADERS[:3], ["REPORT DATE", "CELL", "NO"])
        self.assertEqual(REVERSAL_HEADERS[:3], ["REPORT DATE", "CELL", "NO"])
        self.assertEqual([r[1] for r in unusual["rows"][1:]], [1, 2])
        self.assertEqual([r[1] for r in reversal["rows"][1:]], [1, 2])
        self.assertEqual(unusual["cell_display_map"], {"412111": 1, "412112": 2})
        self.assertEqual([r[2] for r in unusual["rows"][1:]], [1, 2])
        self.assertEqual(unusual["rows"][1][12:], [" literal =1+1 ", "reason retained"])
        self.assertEqual(reversal["rows"][1][12], " literal =1+1 ")

    def test_same_source_id_in_two_cells_is_not_deduplicated(self):
        facts = [self.fact(code, reversal=[self.row("COMMON-ID")]) for code in self.config.cell_codes]
        output = build_cell_detail_matrix(self.config, facts, "2026-10-13", "reversal")
        self.assertEqual(output["row_count"], 2)
        self.assertEqual([row[4] for row in output["rows"][1:]], ["COMMON-ID", "COMMON-ID"])

    def test_empty_cell_is_covered_without_synthetic_detail_row(self):
        facts = [self.fact("412111", reversal=[self.row("REV-A")]), self.fact("412112")]
        output = build_cell_detail_matrix(self.config, facts, "2026-10-13", "reversal")
        self.assertEqual(output["cell_codes"], ["412111", "412112"])
        self.assertEqual(output["row_count"], 1)
        self.assertEqual(output["rows"][1][1], 1)

    def test_incomplete_invalid_or_mixed_report_date_blocks_the_whole_tab(self):
        good = [self.fact(code) for code in self.config.cell_codes]
        with self.assertRaisesRegex(ValueError, "missing required cell coverage"):
            build_cell_detail_matrix(self.config, good[:1], "2026-10-13", "unusual")
        invalid = [dict(good[0]), dict(good[1])]
        invalid[1]["calculation_id"] = "bad"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            build_cell_detail_matrix(self.config, invalid, "2026-10-13", "unusual")
        with self.assertRaisesRegex(ValueError, "kind"):
            build_cell_detail_matrix(self.config, good, "2026-10-13", "qris")

    def test_detail_styles_keep_old_table_pattern_with_cell_column(self):
        matrix = build_cell_detail_matrix(self.config,
            [self.fact("412111", reversal=[self.row("A")]), self.fact("412112")],
            "2026-10-13", "reversal")
        requests = detail_format_requests(23, matrix)
        sheet = next(req for req in requests if "updateSheetProperties" in req)
        self.assertEqual(sheet["updateSheetProperties"]["properties"]["gridProperties"]["frozenRowCount"], 1)
        cell_width = next(req for req in requests if req.get("updateDimensionProperties", {}).get("range", {}).get("startIndex") == 1)
        self.assertEqual(cell_width["updateDimensionProperties"]["properties"]["pixelSize"], 70)
        self.assertFalse(any("addBanding" in request for request in requests))
        self.assertTrue(any(item.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("backgroundColor")
                            == {"red": 1, "green": 1, "blue": 1} for item in requests))
        self.assertTrue(any(req.get("setBasicFilter", {}).get("filter", {}).get("range", {}).get("sheetId") == 23
                            for req in requests))

    def test_sparse_cell_two_stays_two_and_unknown_ordinal_is_rejected(self):
        self.config = CellReportConfig("412110_B", ("412112",), "2026-10-13",
            "2026-10-13T00:00:00+08:00", {"412112": 2000}, "TEST-v2")
        fact = self.fact("412112", reversal=[self.row("000123")])
        output = build_cell_detail_matrix(self.config, [fact], "2026-10-13", "reversal")
        self.assertEqual(output["rows"][1][1], 2)
        self.assertEqual(output["rows"][1][4], "000123")
        self.assertEqual(fact["identity"]["cell_code"], "412112")
        self.config = CellReportConfig("412110_B", ("412119",), "2026-10-13",
            "2026-10-13T00:00:00+08:00", {"412119": 2000}, "TEST-v2")
        with self.assertRaisesRegex(ValueError, "cluster-relative"):
            build_cell_detail_matrix(self.config, [self.fact("412119")], "2026-10-13", "reversal")

    def test_empty_detail_has_no_zero_height_format_requests(self):
        matrix = build_cell_detail_matrix(self.config, [self.fact(code) for code in self.config.cell_codes],
            "2026-10-13", "unusual")
        for request in detail_format_requests(23, matrix):
            item = next(iter(request.values()))
            area = item.get("range", {})
            if "startRowIndex" in area:
                self.assertLess(area["startRowIndex"], area["endRowIndex"])

    def test_history_rebuilds_all_cells_and_rejects_date_gaps(self):
        facts = []
        for day in ("2026-10-13", "2026-10-14"):
            left, right = self.row(day + "-A"), self.row(day + "-B", 13)
            left["Transaction Date"] = day + " 12:00:00"
            right["Transaction Date"] = day + " 13:00:00"
            facts.extend([self.fact("412111", reversal=[left]),
                          self.fact("412112", reversal=[right])])
            for item in facts[-2:]:
                item["report_date"] = day
                item["calculation"]["report_date"] = day
                item["generation_id"] = day + item["identity"]["cell_code"]
                item["calculation_id"] = fingerprint({
                    "generation_id": item["generation_id"], "config_id": item["config_id"],
                    "rules_version": item["rules_version"], "calculation": item["calculation"],
                })
        history = build_cell_detail_history(self.config, facts, "reversal")
        self.assertEqual((history["start_date"], history["end_date"], history["row_count"]),
                         ("2026-10-13", "2026-10-14", 4))
        self.assertEqual(history["headers"][1], "CELL")
        self.assertEqual(history["day_separator_rows"], [2, 4])
        tops = [item["updateBorders"]["range"]["startRowIndex"] for item in detail_format_requests(23, history)
                if item.get("updateBorders", {}).get("top", {}).get("style") == "SOLID_MEDIUM"]
        self.assertEqual(tops, [1, 3])
        for item in facts[-2:]:
            item["report_date"] = "2026-10-15"
            item["calculation"]["report_date"] = "2026-10-15"
            item["generation_id"] = "2026-10-15" + item["identity"]["cell_code"]
            for row in item["calculation"]["reversal"]:
                row["Transaction Date"] = "2026-10-15" + row["Transaction Date"][10:]
            item["calculation_id"] = fingerprint({
                "generation_id": item["generation_id"], "config_id": item["config_id"],
                "rules_version": item["rules_version"], "calculation": item["calculation"],
            })
        with self.assertRaisesRegex(ValueError, "missing calendar date"):
            build_cell_detail_history(self.config, facts, "reversal")


if __name__ == "__main__":
    unittest.main()
