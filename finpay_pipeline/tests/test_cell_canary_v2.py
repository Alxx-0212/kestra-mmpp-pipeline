"""Offline checks of fixed-scope synthetic inputs, never a native E2E run."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from finpay_pipeline.cell_workflow import prepare_cell_bundle_v2
from finpay_pipeline.tests import run_cell_invoice_e2e_canary_v2 as canary


class CellCanaryV2Tests(unittest.TestCase):
    def test_preparation_is_pinned_reproducible_scope_and_no_implicit_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "synthetic-key.json"
            key.write_text(json.dumps({"client_email": "writer@example.test"}))
            data = root / "artifacts"
            with contextlib.redirect_stdout(io.StringIO()):
                canary.prepare(data, key)
            pins = json.loads((data / "pins.json").read_text())
            total = 0
            with contextlib.redirect_stdout(io.StringIO()):
                for day in canary.DATES:
                    runtime, configs, manifest, batches = prepare_cell_bundle_v2(
                        data / day / "cell_sources_v2.zip", data / "configuration.json",
                        pins["configuration"], pins["bundles"][day],
                        approved_configuration_sha256=pins["configuration"])
                    self.assertEqual(set(batches), {"412110_B"})
                    self.assertEqual(len(batches["412110_B"]), 2)
                    for fact in batches["412110_B"]:
                        total += len(fact["raw_rows"])
                        if fact["raw_rows"]:
                            calc = fact["calculation"]
                            self.assertEqual(len(calc["qris"]), 1)
                            self.assertEqual(len(calc["reversal"]), 2)
                            self.assertEqual(calc["duplicate_row_count"], 1)
                            self.assertIn("SELLTHRUSALESFEE", [row["Transaction"] for row in calc["summary"]])
            self.assertEqual(total, 60)
            self.assertEqual((data / "db-password").stat().st_mode & 0o777, 0o600)
            self.assertEqual(runtime["spreadsheet_id"], canary.WORKBOOK)
            self.assertEqual({item["worksheet_id"] for item in runtime["worksheet_targets"]["412110_B"].values()},
                             {1900164101, 1900164102, 1900164103, 1900164104})
            with self.assertRaisesRegex(ValueError, "already exists"):
                canary.prepare(data, key)
            existing = root / "other-artifacts" / canary.DATES[0]
            existing.mkdir(parents=True)
            artifact = existing / "cell_sources_v2.zip"
            artifact.write_bytes(b"unrelated artifact")
            with self.assertRaisesRegex(ValueError, "already exists"):
                canary.prepare(existing.parent, key)
            self.assertEqual(artifact.read_bytes(), b"unrelated artifact")

    def test_new_runtime_versions_preserve_old_callers_and_disable_schedule(self):
        root = Path(__file__).parents[1]
        report = (root / "workflows/finpay_cell_reporting_v2.yml").read_text()
        acquisition = (root / "workflows/finpay_digipos_cell_acquisition_v3.yml").read_text()
        self.assertIn("containerImage: finpay-cell-reporting:2.0", report)
        self.assertIn("result = run_cell_reporting_v2(", report)
        self.assertIn('"cell_v2_*.json"', report)
        self.assertNotIn("retry:", report)
        self.assertIn("containerImage: finpay-digipos-cell-scraper:2.0", acquisition)
        self.assertIn("result = acquire_cell_bundle_v3(", acquisition)
        self.assertIn("flowId: finpay_cell_reporting_v2", acquisition)
        self.assertIn("wait: true", acquisition)
        self.assertIn("transmitFailed: true", acquisition)
        self.assertIn('cron: "0 5 * * *"', acquisition)
        self.assertIn("timezone: Asia/Makassar", acquisition)
        self.assertIn("disabled: true", acquisition)
        self.assertNotIn("LinkAja", report + acquisition)
        self.assertIn("run_cell_reporting(", (root / "workflows/finpay_cell_reporting_v1.yml").read_text())
        self.assertIn("flowId: finpay_cell_reporting_v1", (root / "workflows/finpay_digipos_cell_acquisition_v2.yml").read_text())


if __name__ == "__main__":
    unittest.main()
