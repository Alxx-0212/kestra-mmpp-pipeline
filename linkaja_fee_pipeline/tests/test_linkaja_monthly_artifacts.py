import csv
import json
import tempfile
import unittest
from pathlib import Path

from linkaja_fee_pipeline.database import (
    DAILY_RAW_HEADERS,
    UNRESOLVED_REVERSAL_HEADERS,
    write_linkaja_monthly_artifacts,
)


class LinkAjaMonthlyArtifactTest(unittest.TestCase):
    def test_writes_json_and_csv_artifacts_without_sheet_dependencies(self):
        daily_row = dict.fromkeys(DAILY_RAW_HEADERS, 0)
        daily_row.update({
            "REPORT DATE": "01/07/2026",
            "TRANSACTION SCENARIO": "Digipos B2B Transfer",
            "TRANSACTION COUNT": 1,
        })
        unresolved_row = dict.fromkeys(UNRESOLVED_REVERSAL_HEADERS, "")
        unresolved_row.update({
            "REPORT DATE": "02/07/2026",
            "REVERSAL TRANSACTION ID": "REV-1",
            "ORIGINAL TRANSACTION ID": "MISSING-1",
            "UNUSUAL REASON CODE": "UNRESOLVED_REVERSAL",
        })
        result = {
            "cluster_id": "123456",
            "daily_rows": [daily_row],
            "unresolved_reversal_rows": [unresolved_row],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            daily_path = Path(temp_dir) / "daily.csv"
            unresolved_path = Path(temp_dir) / "unresolved.csv"
            write_linkaja_monthly_artifacts(
                result,
                result_path=result_path,
                daily_path=daily_path,
                unresolved_path=unresolved_path,
            )

            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")),
                result,
            )
            with daily_path.open(newline="", encoding="utf-8") as handle:
                daily_rows = list(csv.DictReader(handle))
            with unresolved_path.open(newline="", encoding="utf-8") as handle:
                unresolved_rows = list(csv.DictReader(handle))

        self.assertEqual(list(daily_rows[0]), DAILY_RAW_HEADERS)
        self.assertEqual(daily_rows[0]["TRANSACTION COUNT"], "1")
        self.assertEqual(
            list(unresolved_rows[0]),
            UNRESOLVED_REVERSAL_HEADERS,
        )
        self.assertEqual(
            unresolved_rows[0]["UNUSUAL REASON CODE"],
            "UNRESOLVED_REVERSAL",
        )

    def test_empty_analysis_still_writes_csv_headers(self):
        result = {"daily_rows": [], "unresolved_reversal_rows": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            daily_path = Path(temp_dir) / "daily.csv"
            unresolved_path = Path(temp_dir) / "unresolved.csv"
            write_linkaja_monthly_artifacts(
                result,
                result_path=result_path,
                daily_path=daily_path,
                unresolved_path=unresolved_path,
            )

            self.assertEqual(
                daily_path.read_text(encoding="utf-8").splitlines()[0],
                ",".join(DAILY_RAW_HEADERS),
            )
            self.assertEqual(
                unresolved_path.read_text(encoding="utf-8").splitlines()[0],
                ",".join(UNRESOLVED_REVERSAL_HEADERS),
            )


if __name__ == "__main__":
    unittest.main()
