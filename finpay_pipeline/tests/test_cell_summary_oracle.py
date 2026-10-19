import copy
import unittest

from finpay_pipeline.cell_calculation import CellReportConfig
from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.cell_publication_v2 import build_cell_cluster_matrices_v2
from finpay_pipeline.tests.cell_summary_oracle import audit_scope, audit_summary


class IndependentSummaryOracleTests(unittest.TestCase):
    def test_audit_scope_accepts_only_sealed_october_backfill_destination_and_receipt(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from finpay_pipeline.tests import run_cell_october_backfill as backfill
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = {"host": "127.0.0.1", "port": backfill.PORT, "database": backfill.DATABASE,
                      "schema": backfill.SCHEMA, "user": backfill.USER}
            runtime = {"spreadsheet_id": backfill.BOOK, "legacy_spreadsheet_id": backfill.OLD_BOOK,
                "database_target": target, "worksheet_naming": "cluster_alias",
                "worksheet_targets": backfill.TARGETS}
            config = root / "configuration.json"
            config.write_text(json.dumps(runtime))
            pin = hashlib.sha256(config.read_bytes()).hexdigest()
            sources = {day: {"cells": 18, "rows": 11, "sha256": "a" * 64} for day in backfill.DATES}
            (root / "sources.json").write_text(json.dumps(sources))
            receipt = hashlib.sha256((root / "sources.json").read_bytes()).hexdigest()
            (root / "result.json").write_text(json.dumps({"status": "PASS", "source_summary": sources,
                "source_receipt_sha256": receipt}))
            scope = audit_scope(runtime, root, pin)
            self.assertTrue(scope["full_backfill"])
            self.assertEqual((scope["schema"], scope["workbook"], scope["expected_raw_rows"]),
                ("cell_october_2026", runtime["spreadsheet_id"], 17 * 11))
            changed = dict(runtime, spreadsheet_id="wrong-workbook")
            with self.assertRaisesRegex(ValueError, "approved test scope"):
                audit_scope(changed, root, pin)

    def setUp(self):
        self.config = CellReportConfig("412110_B", ("412111", "412112"), "2026-10-13",
            "2026-10-13T00:00:00+08:00", {"412111": 100, "412112": 200}, "AUDIT-FIXTURE")
        self.facts = []
        for index, day in enumerate(("2026-10-13", "2026-10-14", "2026-10-15")):
            for scale, cell in enumerate(self.config.cells, 1):
                credit, debit = 100 * (index + 1) * scale, (100, 80, 60)[index] * scale
                result = {"report_date": day, "qris": [], "unusual": [], "reversal": [],
                    "summary_ready": [{"processed_transaction_label": "recharge", "Kredit": credit, "Debet": 0},
                                      {"processed_transaction_label": "cashout apollo", "Kredit": 0, "Debet": debit}],
                    "summary": [{"Transaction": "RECHARGE", "Sum_of_Kredit": credit, "Sum_of_Debet": 0, "Transaction_Count": 1},
                                {"Transaction": "CASHOUT APOLLO", "Sum_of_Kredit": 0, "Sum_of_Debet": debit, "Transaction_Count": 1}]}
                generation, config_id = "fixture-" + day + cell.cell_code, fingerprint(self.config.to_dict())
                self.facts.append({"identity": cell.to_dict(), "report_date": day, "generation_id": generation,
                    "config_id": config_id, "rules_version": self.config.rules_version, "calculation": result,
                    "calculation_id": fingerprint({"generation_id": generation, "config_id": config_id,
                        "rules_version": self.config.rules_version, "calculation": result})})
        self.matrix = build_cell_cluster_matrices_v2(self.config, self.facts, "2026-10-15")["summary"]

    def test_all_numeric_rows_default_direction_running_reset_and_difference(self):
        result = audit_summary(self.config, self.facts, self.matrix)
        self.assertEqual((result["cell_days"], result["mismatches"]), (6, 0))
        self.assertFalse(result["opening_balances_finance_approved"])

    def test_independent_oracle_rejects_wrong_bank_and_aggregation(self):
        matrix = copy.deepcopy(self.matrix)
        entry = matrix["editable_inputs"][0]
        matrix["numeric_preview"][entry["row"] - 1][entry["column"] - 1] = 0
        with self.assertRaisesRegex(ValueError, "numeric mismatch"):
            audit_summary(self.config, self.facts, matrix)
        facts = copy.deepcopy(self.facts)
        facts[0]["calculation"]["summary"][0]["Sum_of_Kredit"] += 1
        with self.assertRaisesRegex(ValueError, "ledger aggregation"):
            audit_summary(self.config, facts, self.matrix)

    def test_manual_zero_negative_and_positive_inputs_preserve_default_formula_metadata(self):
        manual = {("2026-10-13", "412111"): 0, ("2026-10-14", "412111"): -5,
                  ("2026-10-13", "412112"): 160}
        matrix = build_cell_cluster_matrices_v2(self.config, self.facts, "2026-10-15",
            summary_manual_values=manual)["summary"]
        self.assertEqual(audit_summary(self.config, self.facts, matrix, manual_inputs=manual)["mismatches"], 0)
        for entry in matrix["editable_inputs"]:
            if (entry["report_date"], entry["cell_code"]) in manual:
                self.assertTrue(entry["default"].startswith(("=E", "=I")))


if __name__ == "__main__":
    unittest.main()
