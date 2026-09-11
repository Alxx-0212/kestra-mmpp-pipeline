import unittest
import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = REPOSITORY_ROOT / "finpay_topup_pipeline" / "workflows" / "finpay_topup_pipeline.yml"


class FinpayTopupFlowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FLOW_PATH.read_text(encoding="utf-8")
        cls.flow = yaml.safe_load(cls.text)

    @staticmethod
    def _flatten(tasks):
        for task in tasks or []:
            yield task
            yield from FinpayTopupFlowContractTest._flatten(task.get("tasks"))

    @classmethod
    def _task(cls, task_id):
        return next(task for task in cls._flatten(cls.flow.get("tasks")) if task["id"] == task_id)

    def test_flow_identity_is_stable(self):
        self.assertEqual(self.flow["id"], "finpay_topup_pipeline_v1")
        self.assertEqual(self.flow["namespace"], "finance.finpay")

    def test_window_inputs_are_optional_for_schedule_runs(self):
        inputs = {i["id"]: i for i in self.flow["inputs"]}
        self.assertFalse(inputs["start"]["required"])
        self.assertFalse(inputs["end"]["required"])
        self.assertTrue(inputs["dry_run"]["required"])
        self.assertEqual(inputs["refresh_mode"]["defaults"], "FULL")
        self.assertFalse(inputs["checkpoint_dates"]["required"])

    def test_webhook_and_schedule_triggers_coexist(self):
        triggers = {t["id"]: t for t in self.flow["triggers"]}
        webhook = triggers["finpay-topup"]
        schedule = triggers["finpay-topup-schedule"]
        self.assertEqual(webhook["type"], "io.kestra.plugin.core.trigger.Webhook")
        self.assertIn("secret('FINPAY_TOPUP_WEBHOOK_KEY')", webhook["key"])
        self.assertEqual(
            schedule["type"], "io.kestra.plugin.core.trigger.Schedule"
        )
        self.assertEqual(schedule["cron"], "0 6,11,15,19 * * *")
        self.assertEqual(schedule["timezone"], "Asia/Makassar")

    def test_task_ids_are_preserved(self):
        task_ids = {t["id"] for t in self._flatten(self.flow["tasks"])}
        self.assertEqual(
            task_ids,
            {
                "topup_working_directory",
                "prepare_refresh",
                "extract_topup",
                "ingest_topup",
                "refresh_saldo",
                "reextract_topup",
                "restage_topup",
                "reverify_saldo",
                "finalize_refresh",
                "notify_verification_failure",
                "notify_review",
                "summary_topup",
            },
        )

    def test_every_script_block_compiles_as_python(self):
        scripts = [
            (t["id"], t["script"])
            for t in list(self._flatten(self.flow.get("tasks"))) + self.flow.get("errors", [])
            if "script" in t
        ]
        self.assertTrue(scripts)
        for task_id, script in scripts:
            with self.subTest(task=task_id):
                # Kestra expressions are rendered before execution; replace
                # them with valid Python tokens for the syntax-only contract.
                rendered = re.sub(r"\{\{.*?\}\}", "x", script, flags=re.DOTALL)
                compile(rendered, f"<{task_id}>", "exec")

    def test_ingest_stages_instead_of_committing(self):
        script = self._task("ingest_topup")["script"]
        self.assertIn("load_inbox_staged", script)
        self.assertNotIn("load_inbox(", script)
        self.assertIn('status="STAGED"', script)
        compile(script, "<ingest>", "exec")

    def test_verify_branches_commit_purge_and_alert(self):
        script = self._task("refresh_saldo")["script"]
        self.assertIn("verify_live_bucket_topup", script)
        self.assertIn("include_refresh_id=refresh_id", script)
        self.assertIn("verification_attempt=1", script)
        self.assertNotIn("commit_staged_rows", script)
        self.assertNotIn("format_cycle_alert", script)
        self.assertIn("load_inbox_staged", self._task("restage_topup")["script"])
        self.assertIn("verification_attempt=2", self._task("reverify_saldo")["script"])
        finalize = self._task("finalize_refresh")["script"]
        self.assertIn("commit_staged_rows", finalize)
        self.assertIn("purge_staged_rows", finalize)
        self.assertIn("verified_cluster_ids", finalize)
        self.assertIn('status = "PENDING_REVIEW"', finalize)
        self.assertIn("format_cycle_alert", self._task("notify_review")["script"])
        self.assertIn("review_keyboard", self._task("notify_review")["script"])

    def test_ingest_records_kestra_execution_id(self):
        script = self._task("prepare_refresh")["script"]
        self.assertIn("kestra_execution_id=\"{{ execution.id }}\"", script)

    def test_automatic_scope_is_checkpoint_filtered(self):
        script = self._task("prepare_refresh")["script"]
        self.assertIn("refresh_mode", script)
        self.assertIn('refresh_mode in {"AUTO_CLUSTER", "AUTO_ALL"}', script)
        self.assertIn("latest_checkpoints", script)
        self.assertIn("refresh checkpoint changed before extraction", script)
        self.assertIn("scope_cluster_ids", script)
        self.assertIn("checkpoint_dates=checkpoint_dates", script)
        self.assertIn("per_cluster_json", self.text)

    def test_review_alert_uses_webhook_bot_and_logs_are_structured(self):
        script = self._task("notify_review")["script"]
        self.assertIn("TELEGRAM_REVIEW_BOT_TOKEN", self.text)
        self.assertIn("token=token", script)
        self.assertIn('parse_mode="HTML"', script)
        self.assertIn("event=topup_review_alert", script)
        self.assertIn("event=topup_review_match", script)
        self.assertIn("event=topup_refresh_summary", self._task("summary_topup")["script"])
        failure = self._task("notify_verification_failure")
        self.assertIn("VERIFY_FAILED", failure["runIf"])
        self.assertIn("parse_mode=\"HTML\"", failure["script"])

    def test_verification_outputs_match_evidence_without_moving_calculation_to_notification(self):
        verify = self._task("refresh_saldo")["script"]
        notify = self._task("notify_review")["script"]
        for field in (
            "downloaded_rows",
            "calculation_row_count",
            "matching_row_number",
            "matching_type",
            "matching_transaction_at",
        ):
            self.assertIn(field, verify)
        self.assertIn("verification_json", notify)
        self.assertIn("CHECKPOINT_DATES", notify)
        self.assertIn("checkpoint_dates=checkpoint_dates", notify)
        self.assertNotIn("running_saldo_trace", notify)
        self.assertNotIn("load_inbox_staged", notify)

    def test_flow_does_not_emit_raw_extractor_or_failure_objects(self):
        extract = self._task("extract_topup")["script"]
        self.assertNotIn("r'''{{", self.text)
        self.assertNotIn('print(out["stdout"])', extract)
        self.assertNotIn('print(out["stderr"]', extract)
        self.assertIn("event=topup_extract", extract)
        self.assertIn("event=topup_extract_failed", extract)
        self.assertIn("checkpoint_dates or start", extract)
        self.assertIn("successful_clusters", extract)
        self.assertIn("failed_clusters", extract)
        self.assertIn("source_row_count=successful_clusters[cluster_id].get(\"rows\")", extract)

        failure = next(e for e in self.flow["errors"] if e["id"] == "notify_flow_failure")["script"]
        self.assertIn("TELEGRAM_REVIEW_BOT_TOKEN", self.text)
        self.assertIn("review_token = os.environ.get(\"TELEGRAM_REVIEW_BOT_TOKEN\")", failure)
        self.assertIn("token=review_token", failure)
        self.assertNotIn("token = os.environ.get(\"TELEGRAM_BOT_TOKEN\")", failure)
        self.assertIn("format_flow_failure_alert", failure)
        self.assertIn("event=topup_flow_failure_alert", failure)

    def test_errors_block_sends_one_way_alert(self):
        errors = self.flow.get("errors") or []
        self.assertTrue(errors)
        notify = next(e for e in errors if e["id"] == "notify_flow_failure")
        self.assertIn("sendMessage", notify["script"])

    def test_stage_first_loader_only(self):
        full = self.text
        self.assertNotIn("from finpay_topup_pipeline.loader import load_inbox\n", full)

    def test_task_boundaries_are_explicit_and_files_are_shared_by_working_directory(self):
        working = self.flow["tasks"][0]
        self.assertEqual(working["type"], "io.kestra.plugin.core.flow.WorkingDirectory")
        self.assertIn("finpay-topup-inbox/**/*.csv", working["outputFiles"])
        self.assertIn('os.environ.get("WORKING_DIR", "/tmp/kestra-wd")', self._task("extract_topup")["script"])
        self.assertIn('os.environ.get("WORKING_DIR", "/tmp/kestra-wd")', self._task("ingest_topup")["script"])
        self.assertIn("outputs.refresh_saldo.vars.needs_retry", self._task("reextract_topup")["runIf"])
        self.assertIn("outputs.reextract_topup.vars.succeeded", self._task("restage_topup")["runIf"])


if __name__ == "__main__":
    unittest.main()
