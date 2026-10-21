import ast
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / "finpay_pipeline" / "workflows" / "finpay_pipeline.yml"
MONTHLY_WORKFLOW_PATH = REPOSITORY_ROOT / "finpay_pipeline" / "workflows" / "finpay_monthly_materialization.yml"
PACKAGE_API_PATH = REPOSITORY_ROOT / "finpay_pipeline" / "__init__.py"
FINPAY_README_PATH = REPOSITORY_ROOT / "finpay_pipeline" / "README.md"


EXPECTED_TASK_IDS = {
    "branch_after_unusual_flag",
    "deduplicate_transactions",
    "determine_current_date",
    "downstream_processing_branch",
    "filter_qrisduwit_rows",
    "filter_reversal_rows",
    "flag_unusual_transactions",
    "load_and_validate",
    "notify_on_failure",
    "notify_unusual_telegram",
    "parse_and_resolve",
    "pause_after_summary_sheet_write",
    "persist_qrisduwit_to_db",
    "persist_raw_transactions_to_db",
    "persist_reversal_to_db",
    "persist_transactions_to_db",
    "persist_unusual_to_db",
    "post_dedup_branches",
    "prepare_summary_transactions",
    "preprocess_transaction_labels",
    "qrisduwit_upload_branch",
    "reversal_upload_branch",
    "summarize",
    "summary_upload_branch",
    "upload_qrisduwit_to_sheets",
    "upload_reversal_to_sheets",
    "upload_to_sheets",
    "upload_unusual_to_sheets",
    "validate_integrity",
}

EXPECTED_ARTIFACTS = {
    "deduplicated.parquet",
    "integrity_checked.parquet",
    "preprocessed.parquet",
    "qrisduwit.parquet",
    "reversal.parquet",
    "summary.parquet",
    "summary_ready.parquet",
    "unusual.parquet",
    "validated.parquet",
}

EXPECTED_SECRETS = {
    "FINPAY_DB_HOST",
    "FINPAY_DB_NAME",
    "FINPAY_DB_PASSWORD",
    "FINPAY_DB_PORT",
    "FINPAY_DB_USER",
    "FINPAY_MANDIRI_EDITOR_EMAILS",
    "FINPAY_PROTECTION_EDITOR_EMAILS",
    "FINPAY_SPREADSHEET_LOCALE",
    "FINPAY_SPREADSHEET_TIMEZONE",
    "FINPAY_SPREADSHEET_WRITER_EMAILS",
    "GCP_SA_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}

EXPECTED_CLUSTER_WORKSHEETS = {
    "411311": "PKY",
    "421306": "MRT",
    "421307": "TDR",
    "421315": "BGI",
    "421318": "MRW",
    "421320": "TNT",
}
EXPECTED_SPREADSHEET_NAME = "Salinan dari Monitoring Finpay & LinkAja"


def _workflow_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _monthly_workflow_text():
    return MONTHLY_WORKFLOW_PATH.read_text(encoding="utf-8")


def _literal_package_exports():
    module = ast.parse(PACKAGE_API_PATH.read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in statement.targets
        ):
            return set(ast.literal_eval(statement.value))
    raise AssertionError("finpay_pipeline.__all__ is not a literal assignment")


def _workflow_imports(text):
    imported = set()
    pattern = re.compile(
        r"from finpay_pipeline import\s+(?:([^\n(][^\n]*)|\((.*?)\))",
        re.DOTALL,
    )
    for single_line, grouped in pattern.findall(text):
        raw_names = single_line or grouped
        imported.update(re.findall(r"\b[A-Za-z_]\w*\b", raw_names))
    return imported


def _task_block(text, task_id):
    match = re.search(
        rf"(?m)^(?P<indent>[ \t]*)- id: {re.escape(task_id)}[ \t]*$",
        text,
    )
    if match is None:
        raise AssertionError(f"task not found: {task_id}")
    indent_width = len(match.group("indent"))
    following_lines = text[match.start():].splitlines()
    block = [following_lines[0]]
    for line in following_lines[1:]:
        next_task = re.match(r"^(\s*)- id: ", line)
        if next_task and len(next_task.group(1)) <= indent_width:
            break
        if line and not line[0].isspace() and line.endswith(":"):
            break
        block.append(line)
    return "\n".join(block)


class FinPayWorkflowContractTest(unittest.TestCase):
    def test_flow_identity_and_manual_input_contract(self):
        text = _workflow_text()
        self.assertRegex(text, r"(?m)^id: finpay_daily_pipeline_v5$")
        self.assertRegex(text, r"(?m)^namespace: finance\.finpay$")
        self.assertRegex(text, r"(?ms)^concurrency:\s+limit: 1\s+behavior: QUEUE$")
        inputs = text.split("\ninputs:\n", 1)[1].split("\ntasks:\n", 1)[0]
        self.assertEqual(
            re.findall(r"(?m)^  - id: ([A-Za-z0-9_-]+)$", inputs),
            ["csv_file", "dry_run", "source_filename", "write_gsheet", "send_telegram"],
        )
        self.assertNotRegex(text, r"(?m)^triggers:$")

        parser_pattern = re.search(
            r'PATTERN = re\.compile\(\s*r"([^"]+)"',
            text,
        )
        self.assertIsNotNone(parser_pattern)
        self.assertEqual(
            parser_pattern.group(1),
            r"finpay-(\d+)[(_](\d{2}-\d{2}-\d{4})to\d{2}-\d{2}-\d{4}[)_]?\.(csv|xlsx)$",
        )

    def test_task_ids_are_stable(self):
        task_and_error_sections = _workflow_text().split("\ntasks:\n", 1)[1]
        actual = set(
            re.findall(
                r"(?m)^\s+- id: ([A-Za-z0-9_-]+)$",
                task_and_error_sections,
            )
        )
        self.assertEqual(actual, EXPECTED_TASK_IDS)

    def test_cluster_spreadsheet_and_worksheet_mapping_is_stable(self):
        text = _workflow_text()
        pattern = re.compile(
            r'"(?P<cluster>\d+)":\s*\{\s*'
            r'"target_spreadsheet":\s*"(?P<spreadsheet>[^"]+)",\s*'
            r'"target_worksheet":\s*"(?P<worksheet>[^"]+)"',
            re.DOTALL,
        )
        matches = pattern.findall(text)
        actual_mapping = {
            cluster: worksheet for cluster, _, worksheet in matches
        }
        self.assertEqual(actual_mapping, EXPECTED_CLUSTER_WORKSHEETS)
        self.assertEqual(
            {spreadsheet for _, spreadsheet, _ in matches},
            {EXPECTED_SPREADSHEET_NAME},
        )

    def test_runtime_image_artifacts_and_secrets_are_stable(self):
        text = _workflow_text()
        images = set(re.findall(r"(?m)^\s*containerImage: (\S+)$", text))
        artifacts = set(
            re.findall(
                r"(?m)^\s*- ([A-Za-z0-9_.-]+\.(?:csv|json|parquet))$",
                text,
            )
        )
        secrets = set(re.findall(r"secret\('([^']+)'\)", text))
        self.assertEqual(images, {"finpay-pipeline:3.11"})
        self.assertEqual(artifacts, EXPECTED_ARTIFACTS)
        self.assertEqual(secrets, EXPECTED_SECRETS)

    def test_every_workflow_import_is_in_the_public_api(self):
        workflow_imports = _workflow_imports(_workflow_text())
        package_exports = _literal_package_exports()
        self.assertTrue(workflow_imports)
        self.assertEqual(workflow_imports - package_exports, set())

    def test_dry_run_guards_every_production_side_effect_task(self):
        text = _workflow_text()
        side_effect_tasks = {
            "notify_unusual_telegram",
            "pause_after_summary_sheet_write",
            "persist_qrisduwit_to_db",
            "persist_raw_transactions_to_db",
            "persist_reversal_to_db",
            "persist_transactions_to_db",
            "persist_unusual_to_db",
            "upload_qrisduwit_to_sheets",
            "upload_reversal_to_sheets",
            "upload_to_sheets",
            "upload_unusual_to_sheets",
        }
        for task_id in sorted(side_effect_tasks):
            with self.subTest(task_id=task_id):
                task_block = _task_block(text, task_id)
                self.assertIn("runIf:", task_block)
                self.assertIn("inputs.dry_run == false", task_block)

    def test_failure_notification_is_disabled_for_dry_run_or_opt_out(self):
        task_block = _task_block(_workflow_text(), "notify_on_failure")
        self.assertIn("runIf:", task_block)
        self.assertIn("inputs.dry_run == false", task_block)
        self.assertIn("inputs.send_telegram", task_block)

    def test_summary_upload_passes_explicit_report_date(self):
        task_block = _task_block(_workflow_text(), "upload_to_sheets")
        self.assertIn("REPORT_DATE:", task_block)
        self.assertIn('report_date=os.environ["REPORT_DATE"]', task_block)

    def test_compatibility_facades_reexport_the_package(self):
        expected = "from finpay_pipeline import *"
        for relative_path in ("pipeline.py", "pipeline_refactored.py"):
            facade = (REPOSITORY_ROOT / "finpay_pipeline" / relative_path).read_text(encoding="utf-8")
            self.assertIn(expected, facade)

    def test_canonical_readme_documents_current_workflow_contracts(self):
        readme = FINPAY_README_PATH.read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())

        for required_contract in (
            "finpay_daily_pipeline_v5",
            "finance.finpay",
            "finpay-pipeline:3.11",
            "no Kestra trigger or schedule",
            "csv_file",
            "dry_run",
            "source_filename",
            "write_gsheet",
            "send_telegram",
            "Asia/Makassar",
            "2026-09-01",
            "SELLTHRUSALESFEE",
            "finpay_monthly_materialization_v1",
            EXPECTED_SPREADSHEET_NAME,
        ):
            with self.subTest(contract=required_contract):
                self.assertIn(required_contract, normalized_readme)

        for cluster_id, worksheet in EXPECTED_CLUSTER_WORKSHEETS.items():
            with self.subTest(cluster_id=cluster_id):
                self.assertIn(f"`{cluster_id}`", readme)
                self.assertIn(f"`{worksheet}`", readme)

        for artifact in EXPECTED_ARTIFACTS:
            with self.subTest(artifact=artifact):
                self.assertIn(f"`{artifact}`", readme)

        for table_name in (
            "finpay_qrisduwit_transactions",
            "finpay_raw_transactions",
            "finpay_reversal_transactions",
            "finpay_transactions",
            "finpay_unusual_transactions",
        ):
            with self.subTest(table=table_name):
                self.assertIn(f"`{table_name}`", readme)

        self.assertIn("Legacy `.xls` is explicitly rejected", normalized_readme)

    def test_bulk_cell_flow_exposes_copyable_destination_and_cluster_group(self):
        flow = (REPOSITORY_ROOT / "finpay_pipeline/workflows/finpay_cell_bulk_v1.yml").read_text()
        for setting in (
            "cell_cluster_group", "cell_spreadsheet_id", "cell_spreadsheet_name",
            "legacy_spreadsheet_id", "cell_worksheet_ids_json",
            "create_spreadsheet_if_missing", "create_worksheets_if_missing",
        ):
            self.assertIn(f"{setting}:", flow)
        self.assertIn("cell_spreadsheet_name: \"{{ secret('FINPAY_CELL_SPREADSHEET_NAME') }}\"", flow)
        self.assertIn("legacy_spreadsheet_id: \"{{ secret('FINPAY_LEGACY_SPREADSHEET_ID') }}\"", flow)
        self.assertIn("create_spreadsheet_if_missing: true", flow)
        self.assertIn("create_worksheets_if_missing: true", flow)
        self.assertIn('defaults: "{{ vars.cell_cluster_group }}"', flow)
        self.assertIn("comma-separated cluster group", flow)
        self.assertIn("CELL_FORMAT_SPREADSHEET_NAME: \"{{ vars.cell_spreadsheet_name }}\"", flow)
        self.assertIn("CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING", flow)
        self.assertIn("CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING", flow)
        for task_id in ("preflight_cell_bulk_targets", "publish_cell_bulk"):
            task = flow.split(f"  - id: {task_id}\n", 1)[1].split("\n  - id:", 1)[0]
            for env_name in (
                "CELL_FORMAT_SPREADSHEET_ID", "CELL_FORMAT_SPREADSHEET_NAME",
                "CELL_FORMAT_LEGACY_SPREADSHEET_ID", "CELL_FORMAT_WRITER_EMAIL",
                "CELL_FORMAT_FINANCE_EMAILS", "CELL_FORMAT_WORKSHEET_IDS",
            ):
                with self.subTest(task=task_id, env=env_name):
                    self.assertIn(env_name + ":", task)

    def test_cell_range_forwards_destination_settings_to_day_subflow(self):
        range_flow = (REPOSITORY_ROOT / "finpay_pipeline/workflows/finpay_cell_range_v1.yml").read_text()
        day_flow = (REPOSITORY_ROOT / "finpay_pipeline/workflows/finpay_cell_day_v1.yml").read_text()
        subflow = range_flow.split("flowId: finpay_cell_day_v1", 1)[1]
        settings = {
            "cell_spreadsheet_id": ("STRING", "CELL_FORMAT_SPREADSHEET_ID"),
            "cell_spreadsheet_name": ("STRING", "CELL_FORMAT_SPREADSHEET_NAME"),
            "legacy_spreadsheet_id": ("STRING", "CELL_FORMAT_LEGACY_SPREADSHEET_ID"),
            "cell_worksheet_ids_json": ("STRING", "CELL_FORMAT_WORKSHEET_IDS"),
            "create_spreadsheet_if_missing": ("BOOLEAN", "CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING"),
            "create_worksheets_if_missing": ("BOOLEAN", "CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING"),
        }
        for name, (input_type, environment_name) in settings.items():
            with self.subTest(setting=name):
                self.assertIn(f'{name}: "{{{{ vars.{name} }}}}"', subflow)
                self.assertIn(
                    f'- id: {name}\n    type: {input_type}\n    defaults: "{{{{ vars.{name} }}}}"',
                    day_flow,
                )
                for task_id in ("validate_cell_sheet_targets", "publish_cell_four_tabs"):
                    task = _task_block(day_flow, task_id)
                    self.assertIn(f'{environment_name}: "{{{{ inputs.{name} }}}}"', task)


class FinPayMonthlyWorkflowContractTest(unittest.TestCase):
    def test_monthly_flow_identity_and_inputs(self):
        text = _monthly_workflow_text()
        self.assertRegex(text, r"(?m)^id: finpay_monthly_materialization_v1$")
        self.assertRegex(text, r"(?m)^namespace: finance\.finpay$")
        for input_id in (
            "cluster_id",
            "report_month",
            "calculation_cutoff",
            "expected_source_dates",
            "expected_source_fingerprint",
            "publish",
            "write_gsheet",
        ):
            self.assertIn(f"- id: {input_id}", text)

    def test_monthly_publication_requires_readiness_and_fingerprint(self):
        text = _monthly_workflow_text()
        self.assertIn("expected_source_fingerprint is required", text)
        self.assertIn("publish_finpay_monthly_snapshot", text)
        self.assertIn("source_fingerprint", text)

    def test_daily_upload_uses_public_package_migration_contract(self):
        text = _workflow_text()
        task = _task_block(text, "persist_raw_transactions_to_db")
        self.assertIn("write_finpay_dataframe_to_postgres", task)
        self.assertIn("finpay_source_sha256", task)
        self.assertNotIn("ensure_finpay_monthly_model", task)

    def test_monthly_workflow_imports_are_in_the_public_api(self):
        workflow_imports = _workflow_imports(_monthly_workflow_text())
        package_exports = _literal_package_exports()
        self.assertEqual(workflow_imports - package_exports, set())

    def test_monthly_publication_is_queued_through_sheet_write_and_audit(self):
        text = _monthly_workflow_text()
        self.assertRegex(text, r"(?ms)^concurrency:\s+limit: 1\s+behavior: QUEUE$")
        tasks = text.split("\ntasks:\n", 1)[1]
        self.assertEqual(
            re.findall(r"(?m)^  - id: (\w+)$", tasks),
            [
                "calculate_and_optionally_publish_month",
                "write_finpay_monthly_google_sheet",
                "record_finpay_monthly_google_status",
            ],
        )

    def test_monthly_sheet_retry_and_failure_audit_secret_mapping(self):
        task = _task_block(_monthly_workflow_text(), "write_finpay_monthly_google_sheet")
        self.assertIn('runIf: "{{ inputs.write_gsheet }}"', task)
        self.assertRegex(
            task,
            r"retry:\s+type: exponential\s+interval: PT10S\s+"
            r"maxInterval: PT60S\s+maxAttempts: 3",
        )
        for name in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
            self.assertIn(f"FINPAY_DB_{name}: \"{{{{ secret('FINPAY_DB_{name}') }}}}\"", task)
        self.assertNotIn("networkMode: host", task)


class FinPayMonthlyGoogleWriterBehaviorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(directory.name)
        self.result = {
            "published": False,
            "publication_id": "synthetic-publication",
            "cluster_id": "411311",
            "report_month": "2026-08",
            "status": "READY_FOR_APPROVAL",
        }
        self.env = {
            "SECRET_GCP_SA_KEY": '{"synthetic": true}',
            "TARGET_SPREADSHEET": "synthetic spreadsheet",
            "TARGET_WORKSHEET": "synthetic monthly worksheet",
        }
        self.credential_paths = []
        self.client = object()

        def make_client(path):
            self.credential_paths.append(Path(path))
            self.assertEqual(Path(path).read_text(), '{"synthetic": true}')
            return self.client

        # Only the external boundaries are faked; execute the workflow's real script.
        self.package = ModuleType("finpay_pipeline")
        self.package.make_gspread_client = Mock(side_effect=make_client)
        self.package.write_finpay_monthly_to_gsheet = Mock(
            return_value={"worksheet": "synthetic monthly worksheet", "rows": 7}
        )
        self.package.postgres_dsn_from_env = Mock(return_value="synthetic-not-a-dsn")
        self.package.mark_finpay_google_publication_status = Mock()
        self.kestra = ModuleType("kestra")
        self.outputs = Mock()
        self.kestra.Kestra = SimpleNamespace(outputs=self.outputs)

    def _execute_task(self, task_id="write_finpay_monthly_google_sheet"):
        Path("finpay_monthly_result.json").write_text(json.dumps(self.result))
        task = _task_block(_monthly_workflow_text(), task_id)
        script = textwrap.dedent(task.split("    script: |\n", 1)[1])
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.dict(sys.modules, {"finpay_pipeline": self.package, "kestra": self.kestra}),
            patch.object(sys, "path", list(sys.path)),
        ):
            exec(compile(script, str(MONTHLY_WORKFLOW_PATH), "exec"), {})

    def _assert_write_result(self, status):
        artifact = json.loads(Path("finpay_google_write_result.json").read_text())
        self.assertEqual(artifact["status"], status)
        self.assertEqual(artifact["publication_id"], "synthetic-publication")
        self.outputs.assert_called_with(artifact)
        return artifact

    def test_failed_preview_raises_after_failed_artifact_and_output(self):
        self.package.write_finpay_monthly_to_gsheet.side_effect = RuntimeError("synthetic Sheets outage")
        with self.assertRaisesRegex(RuntimeError, "synthetic Sheets outage"):
            self._execute_task()
        artifact = self._assert_write_result("FAILED")
        self.assertEqual(artifact["error"], "synthetic Sheets outage")
        self.package.postgres_dsn_from_env.assert_not_called()
        self.package.mark_finpay_google_publication_status.assert_not_called()
        self.assertEqual(len(self.credential_paths), 1)
        self.assertFalse(self.credential_paths[0].exists())

    def test_missing_credentials_fail_preview_without_database_writes(self):
        for key in ("", "{}"):
            with self.subTest(key=key):
                self.env["SECRET_GCP_SA_KEY"] = key
                with self.assertRaisesRegex(RuntimeError, "GCP_SA_KEY"):
                    self._execute_task()
                self._assert_write_result("FAILED")
        self.package.make_gspread_client.assert_not_called()
        self.package.write_finpay_monthly_to_gsheet.assert_not_called()
        self.package.postgres_dsn_from_env.assert_not_called()
        self.package.mark_finpay_google_publication_status.assert_not_called()

    def test_published_failure_audits_failed_before_raising(self):
        self.result["published"] = True
        self.package.write_finpay_monthly_to_gsheet.side_effect = RuntimeError("synthetic Sheets outage")

        def audit(dsn, publication_id, status, error):
            self.assertEqual((dsn, publication_id, status, error), (
                "synthetic-not-a-dsn", "synthetic-publication", "FAILED", "synthetic Sheets outage"
            ))
            self._assert_write_result("FAILED")
            self.assertFalse(self.credential_paths[0].exists())

        self.package.mark_finpay_google_publication_status.side_effect = audit
        with self.assertRaisesRegex(RuntimeError, "synthetic Sheets outage"):
            self._execute_task()
        self.package.mark_finpay_google_publication_status.assert_called_once()

    def test_published_missing_credentials_are_audited(self):
        self.result["published"] = True
        self.env["SECRET_GCP_SA_KEY"] = "{}"
        with self.assertRaisesRegex(RuntimeError, "GCP_SA_KEY"):
            self._execute_task()
        artifact = self._assert_write_result("FAILED")
        self.package.mark_finpay_google_publication_status.assert_called_once_with(
            "synthetic-not-a-dsn", "synthetic-publication", "FAILED", artifact["error"]
        )

    def test_client_creation_failure_cleans_up_credentials(self):
        def fail_client(path):
            self.credential_paths.append(Path(path))
            raise RuntimeError("synthetic client failure")

        self.package.make_gspread_client.side_effect = fail_client
        with self.assertRaisesRegex(RuntimeError, "synthetic client failure"):
            self._execute_task()
        self._assert_write_result("FAILED")
        self.assertFalse(self.credential_paths[0].exists())
        self.package.write_finpay_monthly_to_gsheet.assert_not_called()

    def test_success_retains_writer_call_artifact_and_separate_audit(self):
        for published in (False, True):
            with self.subTest(published=published):
                self.result["published"] = published
                self._execute_task()
                artifact = self._assert_write_result("COMPLETE")
                self.assertEqual(artifact, {
                    "status": "COMPLETE",
                    "publication_id": "synthetic-publication",
                    "worksheet": "synthetic monthly worksheet",
                    "rows": 7,
                })
                self.package.write_finpay_monthly_to_gsheet.assert_called_with(
                    self.client,
                    spreadsheet_title="synthetic spreadsheet",
                    worksheet_title="synthetic monthly worksheet",
                    result=self.result,
                )
                self.assertFalse(self.credential_paths[-1].exists())
        self.package.postgres_dsn_from_env.assert_not_called()
        self.package.mark_finpay_google_publication_status.assert_not_called()
        self._execute_task("record_finpay_monthly_google_status")
        self.package.mark_finpay_google_publication_status.assert_called_once_with(
            "synthetic-not-a-dsn", "synthetic-publication", "COMPLETE", None
        )

    def test_failed_attempt_can_be_retried_then_recorded_complete(self):
        self.result["published"] = True
        self.package.write_finpay_monthly_to_gsheet.side_effect = [
            RuntimeError("synthetic transient outage"),
            {"worksheet": "synthetic monthly worksheet", "rows": 7},
        ]
        with self.assertRaisesRegex(RuntimeError, "synthetic transient outage"):
            self._execute_task()
        self._assert_write_result("FAILED")
        self._execute_task()
        self._assert_write_result("COMPLETE")
        self._execute_task("record_finpay_monthly_google_status")
        self.assertEqual(
            [call.args[2] for call in self.package.mark_finpay_google_publication_status.call_args_list],
            ["FAILED", "COMPLETE"],
        )
        self.assertEqual(len(self.credential_paths), 2)
        self.assertFalse(any(path.exists() for path in self.credential_paths))


if __name__ == "__main__":
    unittest.main()
