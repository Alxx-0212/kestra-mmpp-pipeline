import contextlib
import io
import json
import unittest

from finpay_pipeline.cell_observability import observed, report


class CellObservabilityTests(unittest.TestCase):
    def records(self, stream):
        return [json.loads(json.loads(line[2:-2])["logs"][0]["message"])
                for line in stream.getvalue().splitlines()]

    def test_observed_preserves_return_and_filters_call_arguments(self):
        @observed("preprocess")
        def operation(report_date, cluster_id, database_password):
            report("cell_checked", completed=1, total=2)
            return 42

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = operation("2026-10-01", "412110", "PRIVATE")
        self.assertEqual(result, 42)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual([record["event"] for record in self.records(output)],
                         ["stage_start", "cell_checked", "stage_complete"])

    def test_nested_stages_restore_parent_context(self):
        @observed("inner")
        def inner():
            report("inner_event")

        @observed("outer")
        def outer():
            inner()
            report("outer_event")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            outer()
            report("outside_is_noop")
        records = self.records(output)
        self.assertEqual([record["stage"] for record in records],
                         ["outer", "inner", "inner", "inner", "outer", "outer"])

    def test_exception_is_propagated_without_logging_its_message(self):
        @observed("persist")
        def fail():
            raise ValueError("PRIVATE")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, "PRIVATE"):
            fail()
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(self.records(output)[-1]["event"], "stage_failure")
