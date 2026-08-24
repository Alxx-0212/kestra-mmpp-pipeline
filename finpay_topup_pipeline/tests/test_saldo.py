import unittest
from unittest.mock import MagicMock
from finpay_topup_pipeline.saldo import current_saldo, summary_by_cluster


class _Desc:
    def __init__(self, name):
        self.name = name


class TestSaldo(unittest.TestCase):
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
            ("2026-08-01 10:00:00", "Kredit", 500),
            ("2026-08-01 11:00:00", "Debit", 200),
        ]
        self.assertEqual(current_saldo(conn, "411311"), 1300)

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