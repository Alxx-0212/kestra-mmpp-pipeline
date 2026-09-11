import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.bucket import BucketTopupSnapshot
from finpay_topup_pipeline.verify import bucket_cutoff_for_refresh, verify_bucket_topup_values


def _trace(computed, *, matching=True, timestamp=None):
    timestamp = timestamp or datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
    return {
        "computed_saldo": computed,
        "calculation_row_count": 2,
        "source_transaction_min_at": timestamp,
        "source_transaction_max_at": timestamp,
        "matching_row_number": 2 if matching else None,
        "matching_type": "TRANSACTION" if matching else None,
        "matching_transaction_id": 7 if matching else None,
        "matching_transaction_at": timestamp if matching else None,
        "matching_running_saldo": computed if matching else None,
        "matching_row": {
            "txn_id": 7,
            "cluster_id": "421318",
            "transaction_date": timestamp,
            "sender": "A",
            "receiver": "B",
            "transaction_type": "Kredit",
            "amount": 100,
            "currency": "IDR",
            "remarks": "match",
            "source_file": "unit.csv",
            "row_hash": "abc",
        } if matching else None,
    }


class TestVerifyBucketTopup(unittest.TestCase):
    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    def test_empty_snapshots_fail_without_pending_review(self, update):
        result = verify_bucket_topup_values(MagicMock(), 10, [])

        self.assertEqual(result["status"], "VERIFY_FAILED")
        self.assertEqual(result["refresh_status"], "VERIFY_FAILED")
        update.assert_called_once_with(
            update.call_args.args[0],
            10,
            "VERIFY_FAILED",
            "No BUCKET TOP UP snapshots available",
        )

    def test_today_refresh_uses_snapshot_timestamp(self):
        self.assertIsNone(
            bucket_cutoff_for_refresh(date(2026, 8, 19), date(2026, 8, 19))
        )

    def test_historical_refresh_uses_exclusive_next_day_cutoff(self):
        self.assertEqual(
            bucket_cutoff_for_refresh(date(2026, 8, 18), date(2026, 8, 19)),
            date(2026, 8, 19),
        )

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.running_saldo_trace")
    def test_cutoff_date_uses_closed_day_saldo(self, trace, upsert, update):
        conn = MagicMock()
        trace.return_value = _trace(1000)
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
        trace.assert_called_once_with(
            conn,
            "421318",
            include_refresh_id=None,
            cutoff_date=date(2026, 8, 18),
            cutoff_at=None,
            target_value=1000,
            tolerance=0.5,
        )
        self.assertEqual(upsert.call_args.kwargs["bucket_reference_date"], date(2026, 8, 18))
        update.assert_called_once_with(conn, 7, "PENDING_REVIEW", None)

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.running_saldo_trace")
    def test_live_snapshot_uses_cms_capture_timestamp(self, trace, upsert, update):
        conn = MagicMock()
        snapshot_at = datetime(2026, 8, 18, 13, 5, 54, tzinfo=timezone.utc)
        trace.return_value = _trace(64604585, timestamp=snapshot_at)
        snapshot = BucketTopupSnapshot(
            "421318_A",
            "421318",
            64604585,
            "Bucket Top Up Rp 64,604,585",
            snapshot_at,
            "https://digipos-cms.finpay.id/home",
        )

        result = verify_bucket_topup_values(conn, 8, [snapshot])

        self.assertEqual(result["status"], "VERIFIED")
        trace.assert_called_once_with(
            conn,
            "421318",
            include_refresh_id=None,
            cutoff_date=None,
            cutoff_at=snapshot_at,
            target_value=64604585,
            tolerance=0.5,
        )
        self.assertEqual(upsert.call_args.kwargs["computed_saldo"], 64604585)
        self.assertEqual(upsert.call_args.kwargs["bucket_reference_date"].isoformat(), "2026-08-18")
        self.assertEqual(upsert.call_args.kwargs["calculation_cutoff_at"], snapshot_at)
        self.assertIsNotNone(upsert.call_args.kwargs["calculated_at"])
        self.assertEqual(upsert.call_args.kwargs["verification_attempts"], 1)
        self.assertEqual(upsert.call_args.kwargs["calculation_row_count"], 2)
        self.assertEqual(upsert.call_args.kwargs["matching_row_number"], 2)
        self.assertEqual(upsert.call_args.kwargs["matching_row_hash"], "abc")
        self.assertEqual(result["clusters"]["421318"]["matching_transaction_id"], 7)
        self.assertEqual(result["clusters"]["421318"]["matching_row"]["amount"], 100)
        update.assert_called_once_with(conn, 8, "PENDING_REVIEW", None)

    @patch("finpay_topup_pipeline.verify.update_refresh_status")
    @patch("finpay_topup_pipeline.verify.upsert_cluster_status")
    @patch("finpay_topup_pipeline.verify.running_saldo_trace")
    def test_live_snapshot_mismatch_stays_pending_for_review(self, trace, upsert, update):
        conn = MagicMock()
        snapshot_at = datetime(2026, 8, 18, 13, 1, 51, tzinfo=timezone.utc)
        trace.return_value = _trace(64904585, matching=False, timestamp=snapshot_at)
        snapshot = BucketTopupSnapshot(
            "421318_A", "421318", 64604585, "Bucket Top Up Rp 64,604,585",
            snapshot_at, "https://digipos-cms.finpay.id/home",
        )

        result = verify_bucket_topup_values(conn, 9, [snapshot], verification_attempt=2)

        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(upsert.call_args.kwargs["verification_diff"], 300000.0)
        self.assertEqual(upsert.call_args.kwargs["verification_attempts"], 2)
        self.assertIsNone(upsert.call_args.kwargs["matching_row_number"])
        self.assertIsNone(result["clusters"]["421318"]["matching_row"])
        update.assert_called_once_with(conn, 9, "PENDING_REVIEW", "BUCKET TOP UP mismatch for clusters: 421318")
