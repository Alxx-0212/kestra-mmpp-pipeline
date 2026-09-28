import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = REPOSITORY_ROOT / "finpay_digipos_acquisition.yml"
DAILY_FLOW_PATH = REPOSITORY_ROOT / "finpay_pipeline" / "workflows" / "finpay_pipeline.yml"


class FinPayDigiposAcquisitionContractTest(unittest.TestCase):
    def test_acquisition_uses_secret_and_schedule_is_disabled_by_default(self):
        text = FLOW_PATH.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^id: finpay_digipos_acquisition_v1$")
        self.assertRegex(text, r"(?m)^namespace: finance\.finpay$")
        self.assertRegex(text, r"(?m)^triggers:$")
        self.assertIn("cron: \"0 5 * * *\"", text)
        self.assertIn("timezone: Asia/Makassar", text)
        self.assertIn("disabled: true", text)
        self.assertIn("trigger.date | dateAdd(-1, 'DAYS')", text)
        self.assertIn("dry_run: false", text)
        self.assertIn("write_gsheet: true", text)
        self.assertIn("DIGIPOS_PASSWORD: \"{{ secret('DIGIPOS_PASSWORD') }}\"", text)
        self.assertNotIn("TselDigipos", text)
        self.assertIn("networkMode: host", text)

    def test_all_exports_are_validated_before_any_daily_subflow(self):
        text = FLOW_PATH.read_text(encoding="utf-8")
        task_section = text.split("\ntasks:\n", 1)[1]
        task_ids = re.findall(r"(?m)^  - id: ([A-Za-z0-9_-]+)$", task_section)
        self.assertEqual(task_ids[0], "scrape_and_validate_all_clusters")
        self.assertEqual(
            task_ids[1:],
            [f"process_{cluster}_daily" for cluster in
             ("411311", "421306", "421307", "421315", "421318", "421320")],
        )
        self.assertEqual(text.count("flowId: finpay_daily_pipeline_v5"), 6)
        self.assertEqual(text.count("wait: true"), 6)
        self.assertEqual(text.count("transmitFailed: true"), 6)
        for cluster in ("411311", "421306", "421307", "421315", "421318", "421320"):
            self.assertIn(f"outputFiles['{cluster}.xlsx']", text)
            self.assertIn(f"filename_{cluster}", text)

    def test_acquisition_passes_required_filename_and_side_effect_flags(self):
        text = FLOW_PATH.read_text(encoding="utf-8")
        for field in ("csv_file", "source_filename", "dry_run", "write_gsheet", "send_telegram"):
            self.assertEqual(text.count(f"      {field}: \"{{{{"), 6, field)
        self.assertIn("finpay_daily_pipeline_v5", DAILY_FLOW_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
