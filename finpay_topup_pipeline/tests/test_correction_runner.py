import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.correction_runner import (
    apply_operator_correction,
    verify_operator_correction,
)


class TestCorrectionRunner(unittest.TestCase):
    @patch("finpay_topup_pipeline.correction_runner.restage_refresh_cluster")
    @patch("finpay_topup_pipeline.correction_runner.run_extract")
    def test_restage_is_source_only_until_verification(self, run_extract, restage):
        run_extract.return_value = {"returncode": 0, "stdout": "{}", "stderr": ""}
        restage.return_value = {"correction_id": 8, "status": "STAGED", "staged": 3}

        result = apply_operator_correction(
            MagicMock(),
            operation="RESTAGE",
            refresh_id=7,
            cluster_id="411311",
            actor="ops",
            reason="DigiPOS missing 2026-09-05",
            source_reference="refresh:7 ticket:FIN-42",
            source_start="2026-09-05",
            source_end="2026-09-05",
            inbox_dir="/tmp/correction",
            password="secret",
        )

        self.assertTrue(result["ready_for_verification"])
        self.assertEqual(result["correction_id"], 8)
        run_extract.assert_called_once()
        restage.assert_called_once()

    @patch("finpay_topup_pipeline.correction_runner.propose_manual_adjustment_for_refresh")
    def test_adjustment_proposal_is_not_ready_for_verification(self, propose):
        propose.return_value = {"adjustment_id": 12, "status": "PROPOSED"}

        result = apply_operator_correction(
            MagicMock(),
            operation="PROPOSE_ADJUSTMENT",
            refresh_id=7,
            cluster_id="411311",
            actor="ops",
            reason="Finance confirmed a missing debit",
            source_reference="refresh:7 ticket:FIN-43",
            effective_at="2026-09-05T12:00:00+08:00",
            transaction_type="Debit",
            amount="1000",
        )

        self.assertEqual(result["status"], "PROPOSED")
        self.assertFalse(result["ready_for_verification"])

    @patch("finpay_topup_pipeline.correction_runner.verify_live_bucket_topup")
    def test_verification_returns_refresh_boundary_for_reporting(self, verify):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (
            date(2026, 9, 4),
            date(2026, 9, 10),
            date(2026, 9, 4),
        )
        verify.return_value = {"status": "MISMATCH", "clusters": {"411311": {}}}

        result = verify_operator_correction(conn, 7, "411311", "secret")

        self.assertEqual(result["requested_start"], "2026-09-04")
        self.assertEqual(result["requested_end"], "2026-09-10")
        self.assertEqual(result["checkpoint_as_of_date"], "2026-09-04")
        self.assertEqual(result["clusters"]["411311"]["checkpoint_as_of_date"], "2026-09-04")
        verify.assert_called_once()

    def test_invalid_operation_is_rejected_before_database_use(self):
        with self.assertRaises(ValueError):
            apply_operator_correction(
                MagicMock(),
                operation="COMMIT",
                refresh_id=7,
                cluster_id="411311",
                actor="ops",
                reason="invalid",
                source_reference="refresh:7",
            )


if __name__ == "__main__":
    unittest.main()
