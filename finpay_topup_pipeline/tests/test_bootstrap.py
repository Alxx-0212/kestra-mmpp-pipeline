import unittest
from unittest.mock import MagicMock, patch

from finpay_topup_pipeline.bootstrap import bootstrap_database


class TestBootstrap(unittest.TestCase):
    @patch("finpay_topup_pipeline.bootstrap.ensure_schema")
    def test_bootstrap_requires_finpay_database(self, ensure_schema):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("kestra",)

        with self.assertRaisesRegex(RuntimeError, "database"):
            bootstrap_database(conn)

        ensure_schema.assert_not_called()

    @patch("finpay_topup_pipeline.bootstrap.ensure_schema")
    def test_bootstrap_rejects_unknown_active_outlet_mapping(self, ensure_schema):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            ("finpay",),
        ]
        cur.fetchall.return_value = [("411311", "UNKNOWN", "Unknown")]

        with self.assertRaisesRegex(RuntimeError, "outlet foundation mismatch"):
            bootstrap_database(conn)

        ensure_schema.assert_called_once_with(conn)
