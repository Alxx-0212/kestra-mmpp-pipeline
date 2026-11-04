import unittest
from unittest.mock import Mock, patch

import requests

from finpay_pipeline.cell_notifications import (
    notification_messages, notify_from_environment, send_notifications,
)
from finpay_pipeline.cell_observability import cohort_counts


class CellNotificationTests(unittest.TestCase):
    def messages(self, state="SUCCESS", write_gsheet=True, unusual_rows=2):
        return notification_messages(
            state, cluster_id="412110", start_date="2026-10-01", end_date="2026-10-01",
            execution_id="offline-execution", flow_id="finance.finpay.finpay_cell_day_v1",
            write_gsheet=write_gsheet,
            summary={"cells": 2, "rows": 100, "unusual_rows": unusual_rows, "unusual_cells": 1},
        )

    def test_success_and_unusual_are_separate_bounded_plain_text_messages(self):
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertIn("publication verified", messages[0])
        self.assertIn("Unusual rows: 2", messages[1])
        self.assertTrue(all(len(text) <= 4096 for text in messages))

    def test_clean_success_has_no_unusual_alert(self):
        self.assertEqual(len(self.messages(unusual_rows=0)), 1)

    def test_db_only_success_does_not_claim_sheets_complete(self):
        messages = self.messages(write_gsheet=False)
        self.assertEqual(len(messages), 2)
        self.assertIn("Database committed", messages[0])
        self.assertIn("Sheets publication disabled", messages[0])
        self.assertNotIn("publication verified", messages[0])
        self.assertNotIn("Review the Unusual worksheet", messages[1])

    def test_failure_never_claims_success_or_uses_partial_unusual_counts(self):
        for state in ("FAILED", "WARNING", "KILLED"):
            messages = self.messages(state)
            self.assertEqual(len(messages), 1)
            self.assertIn(state, messages[0])
            self.assertNotIn("Unusual rows", messages[0])

    def test_invalid_counts_and_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            self.messages(unusual_rows="secret")
        with self.assertRaises(ValueError):
            self.messages(state="::injected::\nsecret")

    def test_send_checks_telegram_ok_without_logging_response(self):
        response = Mock()
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        post = Mock(return_value=response)
        self.assertEqual(send_notifications(self.messages(), "fake-token", "123", post=post), {"sent": 2})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 30))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("parse_mode", post.call_args.kwargs["json"])

    def test_api_rejection_is_failure_even_with_http_200(self):
        response = Mock()
        response.json.return_value = {"ok": False, "description": "secret"}
        with self.assertRaisesRegex(RuntimeError, "unconfirmed") as raised:
            send_notifications(["test"], "fake-token", "123", post=Mock(return_value=response))
        self.assertNotIn("secret", str(raised.exception))

    def test_timeout_is_not_retried_and_does_not_leak_token(self):
        post = Mock(side_effect=requests.Timeout("https://api.telegram.org/botPRIVATE/sendMessage"))
        with self.assertRaises(RuntimeError) as raised:
            send_notifications(["test"], "PRIVATE", "123", post=post)
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_failed_success_message_does_not_suppress_unusual_attempt(self):
        response = Mock()
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        post = Mock(side_effect=[requests.Timeout("private"), response])
        with self.assertRaisesRegex(RuntimeError, "1 message"):
            send_notifications(self.messages(), "fake-token", "123", post=post)
        self.assertEqual(post.call_count, 2)

    def test_failed_early_execution_can_notify_without_preprocess_outputs(self):
        environment = {
            "EXECUTION_STATE": "FAILED", "EXECUTION_ID": "offline", "FLOW_ID": "test.flow",
            "CLUSTER_ID": "412110", "START_DATE": "2026-10-01", "END_DATE": "2026-10-01",
            "WRITE_GSHEET": "true", "SUMMARY_JSON": "{}",
            "TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "123",
        }
        with patch("finpay_pipeline.cell_notifications.send_notifications") as send:
            notify_from_environment(environment)
        self.assertEqual(len(send.call_args.args[0]), 1)

    def test_counts_use_only_submitted_calculations_and_expose_no_records(self):
        batch = [
            {"calculation": {"raw_row_count": 10, "unusual": [{"secret": "never emit"}]}},
            {"calculation": {"raw_row_count": 20, "unusual": []}},
        ]
        self.assertEqual(cohort_counts(batch), {
            "cells": 2, "rows": 30, "unusual_rows": 1, "unusual_cells": 1,
        })


if __name__ == "__main__":
    unittest.main()
