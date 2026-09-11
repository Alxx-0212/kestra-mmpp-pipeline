import unittest
from unittest.mock import MagicMock

from finpay_topup_pipeline.manual_adjustment import (
    approve_adjustment,
    approve_checkpoint_override,
    propose_adjustment,
    propose_checkpoint_override,
    void_adjustment,
)


class TestManualAdjustment(unittest.TestCase):
    def test_proposal_requires_a_reason_and_stays_proposed(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (7, "PROPOSED")

        result = propose_adjustment(
            conn,
            "421318",
            "2026-08-10T12:00:00+08:00",
            "Kredit",
            "1500.00",
            "Finance manual reconciliation",
            "finance_a",
        )

        self.assertEqual(result["status"], "PROPOSED")
        self.assertEqual(len(result["adjustment_hash"]), 64)
        self.assertIn("adjustment_hash", cur.execute.call_args.args[0])

    def test_adjustment_lifecycle_is_explicit(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (7, "APPROVED")
        self.assertEqual(approve_adjustment(conn, 7, "reviewer"), (7, "APPROVED"))
        cur.fetchone.return_value = (7, "VOID")
        self.assertEqual(void_adjustment(conn, 7, "reviewer"), (7, "VOID"))

    def test_checkpoint_override_cannot_be_approved_backward(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            (4, "2026-08-20"),
            ("2026-08-21",),
        ]
        with self.assertRaises(ValueError):
            approve_checkpoint_override(conn, 4, "reviewer")

    def test_checkpoint_override_proposal_requires_reason(self):
        with self.assertRaises(ValueError):
            propose_checkpoint_override(MagicMock(), "421318", "2026-08-11", 10, "", "finance")
