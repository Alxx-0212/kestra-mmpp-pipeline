import tempfile
import unittest
from pathlib import Path

from finpay_pipeline.tests import run_cell_october_backfill as run
from finpay_pipeline.tests.publish_cell_october_backfill import receipt, validate_scope


class OctoberPublisherReceiptTests(unittest.TestCase):
    def test_failed_sheet_attempt_can_resume_only_with_original_tab_and_receipt_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for day in run.DATES:
                folder = root / "sources" / day
                folder.mkdir(parents=True)
                bundle = folder / "cell_sources_v2.zip"
                bundle.write_bytes(day.encode())
                sources[day] = {"sha256": run.sha(bundle), "cells": 18, "rows": 0, "empty_cells": 18}
            run.dump(root / "sources.json", sources)
            pin = run.sha(root / "sources.json")
            run.dump(root / "result.json", {"status": "FAILED", "source_summary": sources,
                "source_receipt_sha256": pin})
            (root / "original-tab-metadata.json").write_text("{}")
            self.assertEqual(receipt(root, pin), sources)
            with self.assertRaisesRegex(ValueError, "independent backfill receipt pin"):
                receipt(root, "f" * 64)
            (root / "original-tab-metadata.json").unlink()
            with self.assertRaisesRegex(ValueError, "complete acquired backfill receipt"):
                receipt(root, pin)

    def test_prepared_receipt_and_fixed_scope_cannot_be_rebound(self):
        import copy
        target = {"host": "127.0.0.1", "port": run.PORT, "database": run.DATABASE, "schema": run.SCHEMA, "user": run.USER}
        runtime = {"spreadsheet_id": run.BOOK, "legacy_spreadsheet_id": run.OLD_BOOK,
            "approved_v2_spreadsheet_ids": [run.BOOK], "worksheet_targets": run.TARGETS,
            "worksheet_naming": "cluster_alias", "database_target": target, "approved_database_targets": [target],
            "data_classification": "PRODUCTION", "dummy_data_only": False, "writer_email": "writer@example.test",
            "finance_emails": ["writer@example.test"], "accounts": [{"account": account, "cell_codes": list(codes),
                "first_report_date": run.DATES[0], "opening_boundary": run.DATES[0] + "T00:00:00+08:00",
                "opening_balances": {code: 0 for code in codes}} for account, codes in run.ROSTER.items()]}
        validate_scope(runtime, {"configuration_sha256": "prepared"}, "prepared")
        with self.assertRaisesRegex(ValueError, "prepare-time"):
            validate_scope(runtime, {"configuration_sha256": "prepared"}, "replacement")
        changed = copy.deepcopy(runtime)
        changed["spreadsheet_id"] = "other"
        with self.assertRaisesRegex(ValueError, "escaped"):
            validate_scope(changed, {"configuration_sha256": "prepared"}, "prepared")
        changed = copy.deepcopy(runtime)
        changed["accounts"][0]["opening_balances"][next(iter(run.ROSTER[changed["accounts"][0]["account"]]))] = 1
        with self.assertRaisesRegex(ValueError, "zero-opening"):
            validate_scope(changed, {"configuration_sha256": "prepared"}, "prepared")

    def test_all_306_cell_days_and_sealed_receipt_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = {}
            for day in run.DATES:
                path = root / "sources" / day
                path.mkdir(parents=True)
                bundle = path / "cell_sources_v2.zip"
                bundle.write_bytes(b"receipt-only fixture; actual runtime parses the bundle")
                entries[day] = {"sha256": run.sha(bundle), "cells": 18, "rows": 0, "empty_cells": 18, "origin": "fixture"}
            run.dump(root / "sources.json", entries)
            run.dump(root / "result.json", {"status": "ACQUIRED", "source_summary": entries})
            pin = run.sha(root / "sources.json")
            self.assertEqual(len(receipt(root, pin)), 17)
            with self.assertRaisesRegex(ValueError, "pin mismatch"):
                receipt(root, "f" * 64)
            run.dump(root / "result.json", {"status": "PUBLISHING", "source_summary": entries})
            with self.assertRaisesRegex(ValueError, "acquired"):
                receipt(root, pin)


if __name__ == "__main__":
    unittest.main()
