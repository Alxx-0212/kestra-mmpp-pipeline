"""Synthetic per-cell computation, coverage and opening-boundary regressions."""
import copy
import unittest
from unittest.mock import patch

from finpay_pipeline.cell_calculation import (
    CellReportConfig, affected_publication_range, build_cluster_report,
    calculate_cell_day, detail_categories,
)
from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.classification import _collect_validation_unusual_transactions


class CellCalculationTests(unittest.TestCase):
    def setUp(self):
        self.config = CellReportConfig(
            "412110_B", ("412111", "412112"), "2026-10-13",
            "2026-10-13T00:00:00+08:00", {"412111": 1000, "412112": 2000},
            "synthetic-roster-v1",
        )

    def row(self, *, day="2026-10-13", amount=100, debit=0, label="RECHARGE",
            identifier="00012345678901234567", remarks="Transaksi Sellthru"):
        return {"No": 8, "Transaction Date": day + " 12:00:00", "Transaction ID": identifier,
                "Saldo Awal": 1000, "Kredit": amount, "Debet": debit,
                "Saldo Akhir": 1000 + amount - debit, "Transaction Type": "Credit",
                "Transaction": label, "Nomor RS": "0812345678", "Remarks": remarks}

    def prepare(self, code, rows, day="2026-10-13", config=None):
        config = config or self.config
        identity = next(cell for cell in config.cells if cell.cell_code == code)
        source = {**identity.to_dict(), "report_date": day,
                  "validated_rows": len(rows), "footer_total": len(rows),
                  "status": "validated" if rows else "empty",
                  "artifact": "dummy-source.xlsx" if rows else None,
                  "source_sha256": fingerprint(rows)}
        return calculate_cell_day(identity, source, rows, config)

    def test_duplicate_report_preserved_before_calculation_selection(self):
        result = self.prepare("412111", [self.row(), self.row()])
        calculation = result["calculation"]
        self.assertEqual(len(result["raw_rows"]), 2)
        self.assertEqual(calculation["calculation_row_count"], 1)
        self.assertEqual(calculation["duplicate_row_count"], 1)
        self.assertEqual(len(calculation["unusual"]), 1)
        self.assertIn("duplicate row removed", calculation["unusual"][0]["unusual_reason"])
        self.assertEqual(result["raw_rows"][0]["Transaction ID"], self.row()["Transaction ID"])
        self.assertNotIn("No", result["raw_rows"][0])
        self.assertNotIn("Transaction", calculation["summary_ready"][0])
        self.assertEqual(calculation["summary_ready"][0]["processed_transaction_label"], "sellthru")

    def test_same_identifiers_in_different_cells_do_not_deduplicate_each_other(self):
        facts = [self.prepare(code, [self.row()]) for code in self.config.cell_codes]
        report = build_cluster_report(self.config, facts, "2026-10-13")
        self.assertEqual(report["days"][0]["combined"]["measures"]["SELLTHRU"]["count"], 2)
        self.assertEqual(report["days"][0]["combined"]["closing_saldo"], 3200)
        self.assertNotEqual(facts[0]["generation_id"], facts[1]["generation_id"])

    def test_family_validation_runs_once_per_cell(self):
        with patch("finpay_pipeline.cell_calculation._collect_validation_unusual_transactions",
                   wraps=_collect_validation_unusual_transactions) as evaluate:
            self.prepare("412111", [self.row()])
        self.assertEqual(evaluate.call_count, 1)

    def test_padded_family_ids_use_normalized_keys_but_keep_exact_evidence(self):
        rows = [
            self.row(label="SELLTHRU", identifier="BASE", remarks="Sellthru Sales Fee"),
            self.row(label="SELLTHRUFEE", identifier=" BASEFEE ", amount=0, debit=100,
                     remarks=" Platform fee sellthru rp. 100,- "),
            self.row(label="SELLTHRUSALESHOLD", identifier=" BASESLSHLD ", amount=0, debit=10,
                     remarks="Sales hold transaksi sellthru sejumlah 10 rupiah, dari 412110"),
        ]
        result = self.prepare("412111", rows)
        self.assertEqual(result["calculation"]["calculation_row_count"], 3)
        self.assertEqual(result["raw_rows"][1]["Transaction ID"], " BASEFEE ")
        self.assertEqual(result["calculation"]["summary_ready"][1]["Transaction ID"], " BASEFEE ")
        self.assertEqual(result["calculation"]["summary_ready"][1]["Remarks"], rows[1]["Remarks"])
        self.assertEqual(result["calculation"]["summary_ready"][1]["base_id"], "BASE")
        fee = next(row for row in result["calculation"]["summary"] if row["Transaction"] == "SELLTHRUFEE")
        self.assertEqual(fee["Sum_of_Debet"], 100)

    def test_whitespace_only_duplicate_keeps_raw_ids_and_duplicate_evidence(self):
        rows = [self.row(identifier="ID"), self.row(identifier=" ID ")]
        result = self.prepare("412111", rows)
        self.assertEqual(result["calculation"]["calculation_row_count"], 1)
        self.assertEqual(result["raw_rows"][1]["Transaction ID"], " ID ")
        self.assertEqual(result["calculation"]["unusual"][0]["Transaction ID"], " ID ")

    def test_bad_family_in_one_cell_cannot_quarantine_same_id_in_another(self):
        bad = self.prepare("412111", [self.row(remarks="unsupported synthetic remark")])
        good = self.prepare("412112", [self.row()])
        self.assertEqual(bad["calculation"]["calculation_row_count"], 0)
        self.assertTrue(bad["calculation"]["unusual"])
        self.assertEqual(good["calculation"]["calculation_row_count"], 1)

    def test_valid_empty_day_carries_balances_but_missing_day_blocks(self):
        facts = []
        for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
            for code in self.config.cell_codes:
                rows = [] if day == "2026-10-14" else [self.row(day=day)]
                facts.append(self.prepare(code, rows, day))
        report = build_cluster_report(self.config, facts, "2026-10-15")
        self.assertEqual([day["combined"]["closing_saldo"] for day in report["days"]], [3200, 3200, 3400])
        missing = [fact for fact in facts if fact["report_date"] != "2026-10-14"]
        with self.assertRaisesRegex(ValueError, "missing cell coverage.*2026-10-14"):
            build_cluster_report(self.config, missing, "2026-10-15")

    def test_combined_net_is_derived_before_split(self):
        one = self.prepare("412111", [self.row(amount=100)])
        two = self.prepare("412112", [self.row(amount=0, debit=60)])
        report = build_cluster_report(self.config, [one, two], "2026-10-13")
        combined = report["days"][0]["combined"]
        st = next(row for row in combined["mandiri"] if row["label"] == "ST")
        self.assertEqual((st["signed"], st["debet"], st["kredit"]), (40, 40, 0))
        self.assertEqual(combined["closing_saldo"], 3040)

    def test_opening_balance_boundary_is_first_day_start_not_close(self):
        for boundary in ("2026-10-13T23:59:59+08:00", "2026-10-13T00:00:00"):
            config = CellReportConfig(
                self.config.account, self.config.cell_codes, self.config.first_report_date,
                boundary, self.config.opening_balances, self.config.roster_version,
            )
            with self.assertRaisesRegex(ValueError, "opening balance"):
                config.to_dict()

    def test_missing_balance_unknown_cell_and_stale_config_fail(self):
        missing = CellReportConfig(
            self.config.account, self.config.cell_codes, self.config.first_report_date,
            self.config.opening_boundary, {"412111": 1000}, self.config.roster_version,
        )
        with self.assertRaises(ValueError):
            missing.to_dict()
        facts = [self.prepare(code, []) for code in self.config.cell_codes]
        changed = CellReportConfig(
            self.config.account, self.config.cell_codes, self.config.first_report_date,
            self.config.opening_boundary, {"412111": 1500, "412112": 2000}, self.config.roster_version,
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            build_cluster_report(changed, facts, "2026-10-13")
        invalid = copy.deepcopy(facts)
        invalid[0]["identity"]["account"] = "422220_B"
        with self.assertRaisesRegex(ValueError, "another account"):
            build_cluster_report(self.config, invalid, "2026-10-13")

    def test_invalid_money_id_date_and_simultaneous_amounts_fail_before_rules(self):
        for row in ({**self.row(), "Kredit": None}, self.row(amount=100.5),
                    self.row(identifier=123), self.row(day="2026-10-12"),
                    self.row(amount=100, debit=50)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.prepare("412111", [row])

    def test_mutated_summary_cannot_be_published_under_old_fingerprint(self):
        facts = [self.prepare(code, [self.row()]) for code in self.config.cell_codes]
        facts[0]["calculation"]["summary"][0]["Sum_of_Kredit"] = 999
        with self.assertRaisesRegex(ValueError, "calculation fingerprint"):
            build_cluster_report(self.config, facts, "2026-10-13")

    def test_int64_wrap_and_overlarge_aggregate_are_rejected_before_summary(self):
        rows = []
        for identifier in ("DUMMY_A", "DUMMY_B"):
            row = self.row(identifier=identifier)
            row.update({"Saldo Awal": 0, "Kredit": 2 ** 63 - 1, "Saldo Akhir": 2 ** 63 - 1})
            rows.append(row)
        with self.assertRaisesRegex(ValueError, "precision"):
            self.prepare("412111", rows)
        for row in rows:
            row.update({"Kredit": 2 ** 52, "Saldo Akhir": 2 ** 52})
        with self.assertRaisesRegex(ValueError, "aggregate"):
            self.prepare("412111", rows)

    def test_safe_large_aggregate_remains_exact(self):
        rows = [self.row(identifier=identifier, amount=10 ** 12) for identifier in ("DUMMY_A", "DUMMY_B")]
        result = self.prepare("412111", rows)
        self.assertEqual(result["calculation"]["summary"][0]["Sum_of_Kredit"], 2 * 10 ** 12)

    def test_october_hold_is_preprocessed_without_changing_legacy_layout(self):
        rows = [
            self.row(label="SELLTHRU", identifier="FAMILY", remarks="Sellthru Sales Fee"),
            self.row(label="SELLTHRUFEE", identifier="FAMILYFEE", amount=0, debit=100,
                     remarks="Platform fee sellthru rp. 100,-"),
            self.row(label="SELLTHRUSALESHOLD", identifier="FAMILYSLSHLD", amount=0, debit=10,
                     remarks="Sales hold transaksi sellthru sejumlah 10 rupiah, dari 412110"),
        ]
        result = self.prepare("412111", rows)
        hold = result["calculation"]["processed"][2]
        self.assertEqual(hold["raw_transaction_label"], "SELLTHRUSALESHOLD")
        self.assertEqual(hold["processed_transaction_label"], "sellthrusalesfee")
        self.assertEqual(hold["base_id"], "FAMILY")
        self.assertEqual(hold["transaction_id_type"], "SLSFEE")
        self.assertNotIn("SELLTHRUSALESFEE", [key for _, key in detail_categories("2026-10-13")])
        self.assertIn("SELLTHRUSALESFEE", [key for _, key in detail_categories("2026-08-31")])

    def test_historical_correction_recalculates_all_following_balances(self):
        facts = [self.prepare(code, [self.row(day=day)], day)
                 for day in ("2026-10-13", "2026-10-14") for code in self.config.cell_codes]
        changed = [fact for fact in facts if not (fact["identity"]["cell_code"] == "412111"
                                                  and fact["report_date"] == "2026-10-13")]
        changed.append(self.prepare("412111", [self.row(amount=200)]))
        report = build_cluster_report(self.config, changed, "2026-10-14")
        self.assertEqual(report["days"][1]["combined"]["closing_saldo"], 3500)
        self.assertEqual(affected_publication_range("2026-10-13", ["2026-10-14"], "2026-10-16"),
                         ("2026-10-13", "2026-10-16"))

    def test_json_results_retain_literal_remarks_without_notifications(self):
        row = self.row(remarks="=unrecognized literal text")
        result = self.prepare("412111", [row])
        self.assertEqual(result["raw_rows"][0]["Remarks"], row["Remarks"])
        self.assertEqual(result["calculation"]["unusual"][0]["Remarks"], row["Remarks"])


if __name__ == "__main__":
    unittest.main()
