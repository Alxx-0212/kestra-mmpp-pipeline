import unittest
from unittest.mock import MagicMock
from finpay_topup_pipeline.classification import upsert_classification, bulk_classify


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
