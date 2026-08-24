import unittest
from base64 import b64decode
from datetime import date
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.refresh_service import (
    DEFAULT_USERS,
    default_window,
    execution_id_from_response,
    kestra_execution_url,
    kestra_execution_web_url,
    normalize_window,
    submit_kestra_refresh,
)


class TestRefreshService(unittest.TestCase):
    def test_default_window_is_yesterday_to_today(self):
        start, end = default_window(date(2026, 8, 18))
        self.assertEqual(str(start), "2026-08-17")
        self.assertEqual(str(end), "2026-08-18")

    def test_kestra_execution_url(self):
        url = kestra_execution_url(
            "http://localhost:8080/",
            "finance.finpay",
            "finpay_topup_pipeline_v1",
        )
        self.assertEqual(
            url,
            "http://localhost:8080/api/v1/main/executions/finance.finpay/finpay_topup_pipeline_v1",
        )

    def test_all_six_users_default(self):
        self.assertEqual(
            DEFAULT_USERS,
            ["411311_A", "421306_A", "421307_A", "421315_A", "421318_A", "421320_A"],
        )

    def test_normalize_window_always_ends_today(self):
        start, end = normalize_window("2026-08-11", "2026-08-18", today=date(2026, 8, 19))
        self.assertEqual(start, "2026-08-11")
        self.assertEqual(end, "2026-08-19")

    def test_normalize_window_rejects_future_start(self):
        with self.assertRaises(ValueError):
            normalize_window("2026-08-20", "2026-08-20", today=date(2026, 8, 19))

    def test_execution_id_from_kestra_response(self):
        self.assertEqual(execution_id_from_response({"json": {"id": "abc123"}}), "abc123")
        self.assertIsNone(execution_id_from_response({"json": {"state": "RUNNING"}}))

    def test_kestra_execution_web_url(self):
        url = kestra_execution_web_url(
            "http://localhost:8081/",
            "finance.finpay",
            "finpay_topup_pipeline_v1",
            "abc123",
        )
        self.assertEqual(
            url,
            "http://localhost:8081/ui/executions/finance.finpay/finpay_topup_pipeline_v1/abc123",
        )

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_submit_kestra_refresh_forwards_basic_auth(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"id":"exec-1"}'
        urlopen.return_value.__enter__.return_value = response

        result = submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=["421318_A"],
            dry_run=True,
            requested_by="codex",
            basic_user="admin@example.com",
            basic_password="secret",
        )

        request = urlopen.call_args.args[0]
        auth = request.get_header("Authorization")
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(b64decode(auth.removeprefix("Basic ")).decode("utf-8"), "admin@example.com:secret")
        self.assertEqual(result["json"]["id"], "exec-1")
