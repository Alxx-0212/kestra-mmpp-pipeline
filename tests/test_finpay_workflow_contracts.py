import ast
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / "finpay_pipeline.yml"
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
EXPECTED_SPREADSHEET_NAME = "Salinan dari MONITORING FINPAY"


def _workflow_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


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
        r"from pipeline import\s+(?:([^\n(][^\n]*)|\((.*?)\))",
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
        inputs = text.split("\ninputs:\n", 1)[1].split("\ntasks:\n", 1)[0]
        self.assertEqual(
            re.findall(r"(?m)^  - id: ([A-Za-z0-9_-]+)$", inputs),
            ["csv_file", "dry_run"],
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

    def test_compatibility_facades_reexport_the_package(self):
        expected = "from finpay_pipeline import *"
        for relative_path in ("pipeline.py", "pipeline_refactored.py"):
            facade = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
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
            "Asia/Makassar",
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


if __name__ == "__main__":
    unittest.main()
