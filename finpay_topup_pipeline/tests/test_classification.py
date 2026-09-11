import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from finpay_topup_pipeline.classification import (
    bulk_classify,
    classify_for_cluster,
    kredit_report_page,
    unclassified_kredit,
    unclassified_kredit_counts,
    upsert_classification,
)


class TestClassification(unittest.TestCase):
    def test_upsert_shape(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        upsert_classification(conn, 1, "BAHODOPI", note="agent", by="tester")
        self.assertTrue(cursor.execute.called)

    def test_bulk_shape(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        bulk_classify(conn, "cluster_id = %s", ["411311"], "POSO", by="tester")
        self.assertTrue(cursor.execute.called)

    def test_guarded_classification_checks_scope_type_outlet_and_absence(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            (7, "OUTLET_1", "Outlet 1"),
            (7,),
        ]

        result = classify_for_cluster(conn, 7, "421318", "OUTLET_1", "finance")

        self.assertEqual(result["label"], "Outlet 1")
        first_sql, first_params = cursor.execute.call_args_list[0].args
        self.assertIn("t.cluster_id = %s", first_sql)
        self.assertIn("t.transaction_type = 'Kredit'", first_sql)
        self.assertIn("o.active", first_sql)
        self.assertIn("c.txn_id IS NULL", first_sql)
        self.assertEqual(first_params, ["OUTLET_1", 7, "421318"])

    def test_guarded_classification_does_not_write_stale_callback(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = None

        self.assertIsNone(classify_for_cluster(conn, 7, "421318", "OUTLET_1", "finance"))
        self.assertEqual(cursor.execute.call_count, 1)
        conn.commit.assert_called_once_with()

    def test_unclassified_queries_parameterize_cluster(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.description = []
        cursor.fetchall.return_value = []
        cursor.fetchone.side_effect = [(4,), (2,)]

        self.assertEqual(
            unclassified_kredit(
                conn,
                start_date="2026-08-20",
                end_date="2026-08-25",
                cluster_id="421318",
            ),
            [],
        )
        first_sql, first_params = cursor.execute.call_args_list[0].args
        self.assertIn("t.cluster_id = %s", first_sql)
        self.assertEqual(first_params, ["421318", "2026-08-20", "2026-08-25", 50])

        self.assertEqual(unclassified_kredit_counts(conn, cluster_id="421318"), (4, 2))
        total_sql, total_params = cursor.execute.call_args_list[1].args
        recent_sql, recent_params = cursor.execute.call_args_list[2].args
        self.assertIn("t.cluster_id = %s", total_sql)
        self.assertIn("t.cluster_id = %s", recent_sql)
        self.assertEqual(total_params, ["421318"])
        self.assertEqual(recent_params, ["421318"])

    def test_kredit_report_returns_classification_and_pagination(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.description = [
            SimpleNamespace(name=name)
            for name in ("txn_id", "cluster_id", "transaction_date", "amount", "outlet_code", "outlet_label")
        ]
        cursor.fetchall.return_value = [
            (8, "421318", datetime(2026, 8, 25, 9), 150000, "OUTLET_2", "Outlet 2"),
            (9, "421318", datetime(2026, 8, 25, 10), 200000, None, None),
        ]

        result = kredit_report_page(
            conn,
            start_date="2026-08-25",
            end_date="2026-08-25",
            cluster_id="421318",
            limit=1,
            offset=0,
        )

        self.assertTrue(result["has_more"])
        self.assertEqual(result["rows"][0]["outlet_label"], "Outlet 2")
        sql, params = cursor.execute.call_args.args
        self.assertIn("t.transaction_type = 'Kredit'", sql)
        self.assertEqual(params, ["421318", "2026-08-25", "2026-08-25", 2, 0])
