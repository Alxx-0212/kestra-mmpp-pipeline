"""Offline tests for bounded, secret-safe acquisition progress."""
import io
import json
import threading
import unittest
from contextlib import redirect_stdout

from finpay_pipeline.cell_observability import observed
from finpay_pipeline.cell_progress import Progress


class CellProgressTests(unittest.TestCase):
    def test_emits_kestra_envelope_and_only_allowlisted_fields(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with Progress(
                "cell_download", total=1, cluster_id="412110", report_date="2026-10-13",
                account="412110_B", cell_code="412111", role="download",
                heartbeat_interval=None,
            ) as progress:
                progress.update("search_started", completed=0, attempt=1)

        lines = output.getvalue().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.startswith("::") and line.endswith("::"))
            envelope = json.loads(line[2:-2])
            self.assertEqual(len(envelope["logs"]), 1)
            self.assertEqual(envelope["logs"][0]["level"], "INFO")
            record = json.loads(envelope["logs"][0]["message"])
            self.assertEqual(record["stage"], "cell_download")
            self.assertEqual(record["cluster_id"], "412110")
            self.assertEqual(record["report_date"], "2026-10-13")
            self.assertIn("elapsed_seconds", record)
        self.assertEqual(json.loads(
            json.loads(lines[-1][2:-2])["logs"][0]["message"]
        )["completed"], 1)

    def test_eta_appears_only_after_measured_progress(self):
        now = [10.0]
        records = []
        progress = Progress(
            "bulk_acquisition", total=4, logger=records.append,
            clock=lambda: now[0], heartbeat_interval=None,
        )
        with progress:
            self.assertNotIn("estimated_remaining_seconds", records[-1])
            progress.update("waiting", completed=0)
            self.assertNotIn("estimated_remaining_seconds", records[-1])
            now[0] = 12.0
            progress.update("cohort_complete", completed=1)
            self.assertEqual(records[-1]["elapsed_seconds"], 2.0)
            self.assertEqual(records[-1]["estimated_remaining_seconds"], 6.0)
            now[0] = 15.0
            progress.update("cohort_complete", completed=2)
            self.assertEqual(records[-1]["estimated_remaining_seconds"], 5.0)

        self.assertEqual(records[-1]["event"], "stage_complete")
        self.assertEqual(records[-1]["completed"], 4)
        self.assertNotIn("estimated_remaining_seconds", records[-1])

    def test_supported_cluster_selectors_are_normalized_through_observed(self):
        @observed("range_acquisition")
        def operation(cluster_id):
            return cluster_id

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(operation(" 412110, 422010 "), " 412110, 422010 ")
            self.assertEqual(operation(" ALL "), " ALL ")

        records = [
            json.loads(json.loads(line[2:-2])["logs"][0]["message"])
            for line in output.getvalue().splitlines()
        ]
        selectors = [record["cluster_id"] for record in records if record["event"] == "stage_start"]
        self.assertEqual(selectors, ["412110,422010", "ALL"])

    def test_untrusted_text_context_is_dropped_without_blocking_operation(self):
        @observed("range_acquisition")
        def operation(cluster_id, report_date):
            return cluster_id, report_date

        raw_cluster = "https://user:synthetic-secret@example.test/path"
        raw_date = "https://example.test/not-a-date"
        output = io.StringIO()
        with redirect_stdout(output):
            result = operation(raw_cluster, raw_date)

        self.assertEqual(result, (raw_cluster, raw_date))
        self.assertNotIn("cluster_id", output.getvalue())
        self.assertNotIn("report_date", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_total_and_role_resets_start_a_fresh_eta_epoch(self):
        now = [0.0]
        records = []
        progress = Progress(
            "staged_acquisition", logger=records.append, clock=lambda: now[0],
            heartbeat_interval=None,
        )
        with progress:
            now[0] = 2.0
            progress.update("unbounded_progress", completed=1, total=None)
            self.assertNotIn("estimated_remaining_seconds", records[-1])

            now[0] = 4.0
            progress.update("total_known", total=4)
            self.assertNotIn("estimated_remaining_seconds", records[-1])

            now[0] = 6.0
            progress.update("first_role_progress", completed=2, role="summary")
            self.assertEqual(records[-1]["elapsed_seconds"], 6.0)
            self.assertNotIn("estimated_remaining_seconds", records[-1])

            now[0] = 8.0
            progress.update("summary_progress", completed=3)
            self.assertEqual(records[-1]["estimated_remaining_seconds"], 2.0)

            now[0] = 100.0
            progress.update("second_role_start", role="invoice", total=3, completed=0)
            self.assertEqual(records[-1]["elapsed_seconds"], 100.0)
            self.assertNotIn("estimated_remaining_seconds", records[-1])

            now[0] = 102.0
            progress.update("invoice_progress", completed=1)
            self.assertEqual(records[-1]["estimated_remaining_seconds"], 4.0)

            now[0] = 110.0
            progress.update("invoice_total_adjusted", total=6)
            self.assertNotIn("estimated_remaining_seconds", records[-1])

            now[0] = 112.0
            progress.update("invoice_progress", completed=2)
            self.assertEqual(records[-1]["elapsed_seconds"], 112.0)
            self.assertEqual(records[-1]["estimated_remaining_seconds"], 8.0)
            self.assertIn("estimated_remaining_seconds", records[-1])

        self.assertEqual(records[-1]["elapsed_seconds"], 112.0)
        self.assertNotIn("estimated_remaining_seconds", records[-1])

    def test_logger_io_failure_does_not_change_result_or_mask_business_error(self):
        def broken_logger(_record):
            raise OSError("synthetic logging failure")

        def operation():
            with Progress("persist", logger=broken_logger, heartbeat_interval=None) as progress:
                progress.update("write_complete", completed=1, total=1)
                return "committed"

        self.assertEqual(operation(), "committed")
        with self.assertRaisesRegex(ValueError, "business failure"):
            with Progress("persist", logger=broken_logger, heartbeat_interval=None):
                raise ValueError("business failure")

    def test_unknown_fields_and_raw_exceptions_are_never_logged(self):
        with self.assertRaisesRegex(ValueError, "unsupported progress field"):
            Progress("cell_download", password="synthetic-secret")

        records = []
        with self.assertRaisesRegex(ValueError, "synthetic-secret"):
            with Progress("cell_download", logger=records.append, heartbeat_interval=None) as progress:
                with self.assertRaisesRegex(ValueError, "unsupported progress field"):
                    progress.update("bad_event", transaction_id="synthetic-row-id")
                raise ValueError("synthetic-secret")

        self.assertEqual(records[-1]["event"], "stage_failure")
        serialized = json.dumps(records)
        self.assertNotIn("synthetic-secret", serialized)
        self.assertNotIn("synthetic-row-id", serialized)

    def test_heartbeat_thread_is_stopped_and_joined_on_exit(self):
        heartbeat_seen = threading.Event()

        def logger(record):
            if record["event"] == "heartbeat":
                heartbeat_seen.set()

        progress = Progress("cell_download", logger=logger, heartbeat_interval=0.01)
        with progress:
            self.assertTrue(heartbeat_seen.wait(1.0))

        self.assertIsNotNone(progress._thread)
        self.assertFalse(progress._thread.is_alive())

    def test_heartbeat_identifies_current_phase_and_its_elapsed_time(self):
        now = [0.0]
        records = []
        with Progress("cell_download", clock=lambda: now[0], logger=records.append,
                      heartbeat_interval=None) as progress:
            now[0] = 5.0
            progress.update("search_request_start")
            now[0] = 35.0
            progress._emit_locked("heartbeat")
            self.assertEqual(records[-1]["phase"], "search_request_start")
            self.assertEqual(records[-1]["phase_elapsed_seconds"], 30.0)
            self.assertEqual(records[-1]["elapsed_seconds"], 35.0)


if __name__ == "__main__":
    unittest.main()
