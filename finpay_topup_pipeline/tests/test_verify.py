import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.bucket import BucketTopupSnapshot
from finpay_topup_pipeline.verify import bucket_cutoff_for_refresh, verify_bucket_topup_values


class TestVerifyBucketTopup(unittest.TestCase):
    def test_today_refresh_uses_yesterday_closing_bucket_cutoff(self):
        self.assertEqual(
            bucket_cutoff_for_refresh(date(2026, 8, 19), date(2026, 8, 19)),
            date(2026, 8, 19),
        )

    def test_historical_refresh_uses_latest_downloaded_saldo(self):
        self.assertIsNone(bucket_cutoff_for_refresh(date(2026, 8, 18), date(2026, 8, 19)))

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.current_saldo_before_date")
    def test_cutoff_date_uses_closed_day_saldo(self, saldo_before, upsert, update):
        conn = MagicMock()
        saldo_before.return_value = 1000
        snapshot = BucketTopupSnapshot(
            "421318_A",
            "421318",
            1000,
            "Bucket Top Up Rp 1,000",
            datetime.now(timezone.utc),
            "https://digipos-cms.finpay.id/home",
        )

        result = verify_bucket_topup_values(conn, 7, [snapshot], cutoff_date="2026-08-18")

        self.assertEqual(result["status"], "VERIFIED")
        saldo_before.assert_called_once_with(conn, "421318", "2026-08-18")
        self.assertEqual(upsert.call_args.kwargs["bucket_reference_date"], "2026-08-18")
        update.assert_called_once_with(conn, 7, "VERIFIED", None)

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.latest_saldo_snapshot")
    def test_live_snapshot_uses_latest_downloaded_saldo(self, latest, upsert, update):
        conn = MagicMock()
        latest.return_value = {
            "saldo": 64604585,
            "transaction_date": datetime(2026, 8, 18, 13, 5, 54, tzinfo=timezone.utc),
        }
        snapshot = BucketTopupSnapshot(
            "421318_A",
            "421318",
            64604585,
            "Bucket Top Up Rp 64,604,585",
            datetime.now(timezone.utc),
            "https://digipos-cms.finpay.id/home",
        )

        result = verify_bucket_topup_values(conn, 8, [snapshot])

        self.assertEqual(result["status"], "VERIFIED")
        latest.assert_called_once_with(conn, "421318")
        self.assertEqual(upsert.call_args.kwargs["computed_saldo"], 64604585)
        self.assertEqual(upsert.call_args.kwargs["bucket_reference_date"].isoformat(), "2026-08-18")
        self.assertEqual(upsert.call_args.kwargs["verification_attempts"], 1)
        update.assert_called_once_with(conn, 8, "VERIFIED", None)

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.latest_saldo_snapshot")
    def test_live_snapshot_mismatch_stays_visible_for_review(self, latest, upsert, update):
        conn = MagicMock()
        latest.return_value = {
            "saldo": 64904585,
            "transaction_date": datetime(2026, 8, 18, 13, 1, 51, tzinfo=timezone.utc),
        }
        snapshot = BucketTopupSnapshot(
            "421318_A", "421318", 64604585, "Bucket Top Up Rp 64,604,585",
            datetime.now(timezone.utc), "https://digipos-cms.finpay.id/home",
        )

        result = verify_bucket_topup_values(conn, 9, [snapshot], verification_attempt=2)

        self.assertEqual(result["status"], "VERIFY_FAILED")
        self.assertEqual(upsert.call_args.kwargs["verification_diff"], 300000.0)
        self.assertEqual(upsert.call_args.kwargs["verification_attempts"], 2)
        update.assert_called_once_with(conn, 9, "VERIFY_FAILED", "BUCKET TOP UP mismatch for clusters: 421318")
