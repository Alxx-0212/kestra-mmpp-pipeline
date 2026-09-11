import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from finpay_topup_pipeline.saldo import (
    current_saldo,
    current_saldo_at,
    latest_checkpoints,
    running_saldo_trace,
    summary_by_cluster,
)


class _Desc:
    def __init__(self, name):
        self.name = name


class TestSaldo(unittest.TestCase):
    @staticmethod
    def _row(txn_id, timestamp, transaction_type, amount):
        return (
            txn_id,
            "411311",
            timestamp,
            "sender",
            "receiver",
            transaction_type,
            amount,
            "IDR",
            "remarks",
            "unit.csv",
            f"hash-{txn_id}",
        )

    def test_latest_checkpoints_returns_one_row_per_requested_cluster(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            ("411311", "2026-08-20", 1000),
            ("421318", "2026-08-11", 68837100),
        ]

        result = latest_checkpoints(conn, ["411311", "421318"])

        self.assertEqual(result["421318"]["as_of_date"], "2026-08-11")
        self.assertEqual(result["411311"]["opening_balance"], 1000)

    def test_current_saldo_none(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None  # No opening balance
        cur.fetchall.return_value = []    # No transactions
        self.assertIsNone(current_saldo(conn, "411311"))

    def test_current_saldo_with_opening_balance(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1000, "2026-08-01")  # Opening balance of 1000, as_of_date
        cur.fetchall.return_value = []       # No transactions
        self.assertEqual(current_saldo(conn, "411311"), 1000)

    def test_current_saldo_with_transactions(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1000, "2026-08-01")  # Opening balance of 1000, as_of_date
        # Transactions: Kredit 500, Debit 200 (on 2026-08-01, after opening date)
        cur.fetchall.return_value = [
            self._row(1, "2026-08-01 10:00:00", "Kredit", 500),
            self._row(2, "2026-08-01 11:00:00", "Debit", 200),
        ]
        self.assertEqual(current_saldo(conn, "411311"), 1300)

    def test_current_saldo_at_uses_inclusive_timestamp(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1000, "2026-08-01")
        cur.fetchall.return_value = [
            self._row(1, datetime(2026, 8, 1, 10, tzinfo=timezone.utc), "Kredit", 500),
            self._row(2, datetime(2026, 8, 1, 11, tzinfo=timezone.utc), "Debit", 200),
        ]
        self.assertEqual(
            current_saldo_at(
                conn,
                "411311",
                datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            ),
            1300,
        )

    def test_current_saldo_at_rejects_naive_cutoff(self):
        with self.assertRaises(ValueError):
            current_saldo_at(MagicMock(), "411311", datetime(2026, 8, 1, 11))

    def test_trace_preserves_decimal_precision_for_target_matching(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (Decimal("90071992547409.90"), "2026-08-01")
        cur.fetchall.return_value = [
            self._row(1, "2026-08-01 10:00:00", "Kredit", Decimal("0.01")),
        ]

        result = running_saldo_trace(
            conn,
            "411311",
            target_value="90071992547409.91",
            tolerance="0.00",
        )

        self.assertEqual(str(result["computed_saldo"]), "90071992547409.91")
        self.assertEqual(result["matching_row_number"], 1)

    def test_trace_uses_transaction_date_only_and_returns_last_matching_row(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        first = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        last = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        cur.fetchone.return_value = (1000, "2026-08-01")
        cur.fetchall.return_value = [
            self._row(1, first, "Kredit", 100),
            self._row(2, datetime(2026, 8, 1, 11, tzinfo=timezone.utc), "Debit", 100),
            self._row(3, last, "Kredit", 100),
        ]

        result = running_saldo_trace(conn, "411311", target_value=1100)

        self.assertEqual(result["computed_saldo"], 1100)
        self.assertEqual(result["calculation_row_count"], 3)
        self.assertEqual(result["matching_row_number"], 3)
        self.assertEqual(result["matching_type"], "TRANSACTION")
        self.assertEqual(result["matching_transaction_id"], 3)
        self.assertEqual(result["matching_transaction_at"], last)
        self.assertEqual(result["source_transaction_min_at"], first)
        self.assertEqual(result["matching_row"]["row_hash"], "hash-3")
        query = cur.execute.call_args_list[1].args[0]
        self.assertIn("ORDER BY transaction_date ASC", query)
        self.assertNotIn("ORDER BY transaction_date ASC,", query)
        self.assertNotIn("ORDER BY transaction_date, txn_id", query)

    def test_trace_reports_checkpoint_match_without_a_transaction(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1000, "2026-08-01")
        cur.fetchall.return_value = []

        result = running_saldo_trace(conn, "411311", target_value=1000)

        self.assertEqual(result["matching_type"], "CHECKPOINT")
        self.assertEqual(result["matching_row_number"], 0)
        self.assertIsNone(result["matching_transaction_id"])
        self.assertEqual(result["matching_running_saldo"], 1000)

    def test_trace_does_not_expose_intermediate_match_when_final_saldo_mismatches(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1000, "2026-08-01")
        cur.fetchall.return_value = [
            self._row(1, "2026-08-01 10:00:00", "Kredit", 100),
            self._row(2, "2026-08-01 11:00:00", "Kredit", 50),
        ]

        result = running_saldo_trace(conn, "411311", target_value=1100)

        self.assertEqual(result["computed_saldo"], 1150)
        self.assertIsNone(result["matching_type"])
        self.assertIsNone(result["matching_row"])

    def test_summary_shape(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        # First call: summary query
        # Second call: _compute_running_saldo for each cluster (fetchone + fetchall)
        cur.description = [
            _Desc("cluster_id"),
            _Desc("transaction_count"),
            _Desc("total_kredit"),
            _Desc("total_debit"),
            _Desc("first_transaction"),
            _Desc("last_transaction"),
            _Desc("unclassified_count"),
            _Desc("last_loaded_at"),
        ]
        cur.fetchall.side_effect = [
            [("411311", 2, 1000, 500, "2026-08-01", "2026-08-02", 0, "2026-08-02")],  # summary query
            [],  # _compute_running_saldo fetchone (no opening) - returns None
            [("2026-08-01 10:00:00", "Kredit", 1000), ("2026-08-01 11:00:00", "Debit", 500)],  # _compute_running_saldo fetchall
        ]
        rows = summary_by_cluster(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cluster_id"], "411311")
        self.assertEqual(rows[0]["rows"], 2)
        self.assertEqual(rows[0]["total_kredit"], 1000)
        self.assertEqual(rows[0]["total_debit"], 500)


if __name__ == "__main__":
    unittest.main()
