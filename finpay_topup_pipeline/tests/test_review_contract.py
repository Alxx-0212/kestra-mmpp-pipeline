import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.review import approve_refresh


class TestReviewCheckpointContract(unittest.TestCase):
    def _connection(self, requested_end, requested_at):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            ("PENDING_REVIEW", requested_end, requested_at, ["421318_A"]),
            (0,),
            (datetime(2026, 8, 28, 16, tzinfo=timezone.utc),),
        ]
        cursor.fetchall.side_effect = [
            [("421318", "VERIFIED")],
            [],
            [],
        ]
        return conn

    def test_current_day_refresh_advances_to_same_day_boundary(self):
        conn = self._connection(
            date(2026, 8, 28),
            datetime(2026, 8, 28, 15, tzinfo=timezone.utc),
        )

        with patch("finpay_topup_pipeline.review.update_opening_balance_before_date") as update:
            result = approve_refresh(conn, 42, "reviewer")

        self.assertEqual(result["status"], "COMMITTED")
        update.assert_called_once_with(
            conn,
            date(2026, 8, 28),
            cluster_ids=["421318"],
            commit=False,
        )

    def test_historical_refresh_advances_only_its_cluster(self):
        conn = self._connection(
            date(2026, 8, 27),
            datetime(2026, 8, 28, 8, tzinfo=timezone.utc),
        )

        with patch("finpay_topup_pipeline.review.update_opening_balance_before_date") as update:
            approve_refresh(conn, 42, "reviewer")

        update.assert_called_once_with(
            conn,
            date(2026, 8, 28),
            cluster_ids=["421318"],
            commit=False,
        )


if __name__ == "__main__":
    unittest.main()
