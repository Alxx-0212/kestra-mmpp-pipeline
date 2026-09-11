import re
import unittest
import urllib.error
from base64 import b64decode, b64encode
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
from finpay_topup_pipeline.audit import start_refresh


def multipart_fields(request):
    text = request.data.decode("utf-8")
    return dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n(?=--)', text, re.DOTALL))


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

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_multipart_defaults_keep_refresh_source_and_verification(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"id":"exec-1"}'
        urlopen.return_value.__enter__.return_value = response

        submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=list(DEFAULT_USERS),
        )

        fields = multipart_fields(urlopen.call_args.args[0])
        self.assertEqual(fields["trigger_source"], "refresh_service")
        self.assertEqual(fields["verify_bucket_topup"], "true")
        self.assertEqual(fields["users"], " ".join(DEFAULT_USERS))
        self.assertEqual(fields["dry_run"], "false")
        self.assertEqual(fields["refresh_mode"], "FULL")

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_telegram_override_and_unverified_dry_run_fields(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"id":"exec-2"}'
        urlopen.return_value.__enter__.return_value = response

        submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=["421318_A"],
            dry_run=True,
            requested_by="telegram_user",
            trigger_source="telegram-bot",
            verify_bucket_topup=False,
        )

        fields = multipart_fields(urlopen.call_args.args[0])
        self.assertEqual(fields["trigger_source"], "telegram-bot")
        self.assertEqual(fields["verify_bucket_topup"], "false")
        self.assertEqual(fields["dry_run"], "true")
        self.assertEqual(fields["requested_by"], "telegram_user")

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_automatic_cluster_mode_and_checkpoint_dates_are_forwarded(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"id":"exec-cluster"}'
        urlopen.return_value.__enter__.return_value = response

        submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=["421318_A"],
            refresh_mode="AUTO_CLUSTER",
            checkpoint_dates={"421318": "2026-08-11"},
        )

        self.assertEqual(
            multipart_fields(urlopen.call_args.args[0])["refresh_mode"],
            "AUTO_CLUSTER",
        )
        self.assertEqual(
            multipart_fields(urlopen.call_args.args[0])["checkpoint_dates"],
            '{"421318":"2026-08-11"}',
        )

    def test_invalid_refresh_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            submit_kestra_refresh(
                base_url="http://kestra:8080",
                namespace="finance.finpay",
                flow_id="finpay_topup_pipeline_v1",
                start="2026-08-18",
                end="2026-08-19",
                users=["421318_A"],
                refresh_mode="arbitrary",
            )

        with self.assertRaises(ValueError):
            submit_kestra_refresh(
                base_url="http://kestra:8080",
                namespace="finance.finpay",
                flow_id="finpay_topup_pipeline_v1",
                start="2026-08-18",
                end="2026-08-19",
                users=["421318_A"],
                refresh_mode="AUTO_ALL",
            )

        with self.assertRaises(ValueError):
            submit_kestra_refresh(
                base_url="http://kestra:8080",
                namespace="finance.finpay",
                flow_id="finpay_topup_pipeline_v1",
                start="2026-08-18",
                end="2026-08-19",
                users=["421318_A"],
                refresh_mode="DEBUG_CLUSTER",
            )

    def test_auto_refresh_audit_records_checkpoint_date(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        cursor.execute.return_value.fetchone.return_value = (42,)

        refresh_id = start_refresh(
            conn,
            "2026-08-11",
            "2026-08-28",
            ["421318_A"],
            refresh_mode="AUTO_CLUSTER",
            checkpoint_dates={"421318": "2026-08-11"},
        )

        self.assertEqual(refresh_id, 42)
        self.assertTrue(
            any("checkpoint_as_of_date" in call.args[0] for call in cursor.execute.call_args_list)
        )

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_url_error_maps_to_status_zero_without_credential_leakage(self, urlopen):
        urlopen.side_effect = urllib.error.URLError(
            "https://admin@example.com:s3cret-password@kestra:8080/refused"
        )

        result = submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=list(DEFAULT_USERS),
            basic_user="admin@example.com",
            basic_password="s3cret-password",
        )

        self.assertEqual(result["status_code"], 0)
        self.assertIsNone(result["json"])
        self.assertTrue(result["body"].startswith("Kestra unreachable:"))
        self.assertNotIn("s3cret-password", result["body"])
        self.assertNotIn("admin@example.com", result["body"])
        credential_pair = "admin@example.com:s3cret-password"
        encoded = b64decode(b64encode(credential_pair.encode())).decode()
        self.assertNotIn(encoded, result["body"])
        self.assertNotIn(credential_pair, result["body"])

    @patch("finpay_topup_pipeline.refresh_service.urllib.request.urlopen")
    def test_timeout_oserror_maps_to_status_zero(self, urlopen):
        urlopen.side_effect = TimeoutError("timed out")

        result = submit_kestra_refresh(
            base_url="http://kestra:8080",
            namespace="finance.finpay",
            flow_id="finpay_topup_pipeline_v1",
            start="2026-08-18",
            end="2026-08-19",
            users=list(DEFAULT_USERS),
        )

        self.assertEqual(result["status_code"], 0)
        self.assertIsNone(result["json"])
        self.assertIn("Kestra unreachable:", result["body"])
