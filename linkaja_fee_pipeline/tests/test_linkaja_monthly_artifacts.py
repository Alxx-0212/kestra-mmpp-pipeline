import csv
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linkaja_fee_pipeline.database import (
    DAILY_RAW_HEADERS,
    UNRESOLVED_REVERSAL_HEADERS,
    write_linkaja_monthly_artifacts,
)


WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "linkaja_monthly_materialization.yml"


def execute_monthly_task(task_id, directory, environment):
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    task = workflow.split(f"  - id: {task_id}\n", 1)[1].split("\n  - id:", 1)[0]
    script = textwrap.dedent(task.split("    script: |\n", 1)[1])
    outputs = []
    previous_directory = Path.cwd()
    try:
        os.chdir(directory)
        with patch.dict(os.environ, environment, clear=True), patch.object(sys, "path", sys.path.copy()), patch.dict(
            sys.modules, {"kestra": SimpleNamespace(Kestra=SimpleNamespace(outputs=outputs.append))}
        ):
            exec(compile(script, f"{WORKFLOW_PATH.name}:{task_id}", "exec"), {})
    finally:
        os.chdir(previous_directory)
    return outputs


class LinkAjaMonthlyWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.result = {
            "materialization_id": "monthly-writer-test",
            "published": False,
            "cluster_id": "123456",
            "report_month": "2026-07-01",
        }
        self.credential_paths = []

    def _client(self, credential_path):
        self.credential_paths.append(Path(credential_path))
        self.assertEqual(json.loads(Path(credential_path).read_text()), {"fixture": True})
        return object()

    def _run_writer(self, key='{"fixture": true}'):
        (self.directory / "linkaja_monthly_result.json").write_text(json.dumps(self.result))
        return execute_monthly_task("write_linkaja_monthly_google_sheet", self.directory, {
            "SECRET_GCP_SA_KEY": key,
            "TARGET_SPREADSHEET": "synthetic-test-sheet",
        })

    def _write_result(self):
        return json.loads((self.directory / "linkaja_google_write_result.json").read_text())

    def test_monthly_flow_queues_entire_publication_and_render_execution(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        flow_configuration = workflow.split("\ntasks:\n", 1)[0]
        concurrency = re.search(r"^concurrency:\n((?:  .*\n)+)", flow_configuration, re.MULTILINE)
        self.assertIsNotNone(concurrency, "DB commit and Google render need one flow concurrency slot")
        self.assertEqual(dict(line.strip().split(": ", 1) for line in concurrency.group(1).splitlines()),
                          {"limit": "1", "behavior": "QUEUE"})

    def test_monthly_writer_inherits_database_reachable_network(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        writer = workflow.split("  - id: write_linkaja_monthly_google_sheet\n", 1)[1].split("\n  - id:", 1)[0]
        self.assertNotIn("networkMode: host", writer)

    def test_failed_preview_writer_raises_without_database_writes(self):
        for key in ("", "{}", '{"fixture": true}'):
            with self.subTest(key=key), patch(
                "linkaja_fee_pipeline.sheets.make_gspread_client", side_effect=self._client
            ), patch(
                "linkaja_fee_pipeline.sheets.write_linkaja_monthly_to_gsheet",
                side_effect=RuntimeError("synthetic sheet failure"),
            ), patch(
                "linkaja_fee_pipeline.linkaja_postgres_dsn_from_env",
                side_effect=AssertionError("preview must not access the DB"),
            ), patch(
                "linkaja_fee_pipeline.update_linkaja_monthly_google_status",
                side_effect=AssertionError("preview must not audit in the DB"),
            ):
                with self.assertRaisesRegex(RuntimeError, "GCP_SA_KEY|synthetic sheet failure"):
                    self._run_writer(key)
                self.assertEqual(self._write_result()["status"], "FAILED")
                self.assertFalse(any(path.exists() for path in self.credential_paths))

    def test_failed_publication_writer_audits_before_raising(self):
        self.result["published"] = True
        for key in ("", "{}", '{"fixture": true}'):
            audited = []
            with self.subTest(key=key), patch(
                "linkaja_fee_pipeline.sheets.make_gspread_client", side_effect=self._client
            ), patch(
                "linkaja_fee_pipeline.sheets.write_linkaja_monthly_to_gsheet",
                side_effect=RuntimeError("synthetic sheet failure"),
            ), patch("linkaja_fee_pipeline.linkaja_postgres_dsn_from_env", return_value="synthetic-dsn"), patch(
                "linkaja_fee_pipeline.update_linkaja_monthly_google_status",
                side_effect=lambda *args: audited.append(args),
            ):
                with self.assertRaisesRegex(RuntimeError, "GCP_SA_KEY|synthetic sheet failure"):
                    self._run_writer(key)
                write_result = self._write_result()
                self.assertEqual(audited, [("synthetic-dsn", "monthly-writer-test", "FAILED", write_result["error"])])
                self.assertFalse(any(path.exists() for path in self.credential_paths))

    def test_client_creation_failure_raises_and_removes_credential(self):
        def failed_client(path):
            self._client(path)
            raise RuntimeError("synthetic authentication failure")

        with patch("linkaja_fee_pipeline.sheets.make_gspread_client", side_effect=failed_client):
            with self.assertRaisesRegex(RuntimeError, "synthetic authentication failure"):
                self._run_writer()
        self.assertFalse(any(path.exists() for path in self.credential_paths))

    def test_writer_can_retry_then_existing_status_task_records_complete(self):
        self.result["published"] = True
        audited = []
        with patch("linkaja_fee_pipeline.sheets.make_gspread_client", side_effect=self._client), patch(
            "linkaja_fee_pipeline.sheets.write_linkaja_monthly_to_gsheet",
            side_effect=[RuntimeError("transient synthetic failure"), {"worksheet": "synthetic-month"}],
        ), patch("linkaja_fee_pipeline.linkaja_postgres_dsn_from_env", return_value="synthetic-dsn"), patch(
            "linkaja_fee_pipeline.update_linkaja_monthly_google_status",
            side_effect=lambda *args: audited.append(args),
        ):
            with self.assertRaisesRegex(RuntimeError, "transient synthetic failure"):
                self._run_writer()
            outputs = self._run_writer()
            self.assertEqual(outputs[0]["status"], "COMPLETE")
            self.assertEqual(self._write_result()["worksheet"], "synthetic-month")
            self.assertEqual(len(audited), 1, "successful audit remains owned by the next task")
            execute_monthly_task("record_linkaja_monthly_google_status", self.directory, {})
        self.assertEqual(audited, [
            ("synthetic-dsn", "monthly-writer-test", "FAILED", "transient synthetic failure"),
            ("synthetic-dsn", "monthly-writer-test", "COMPLETE", None),
        ])
        self.assertFalse(any(path.exists() for path in self.credential_paths))


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
