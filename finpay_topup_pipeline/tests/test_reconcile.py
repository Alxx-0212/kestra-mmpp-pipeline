import unittest
from unittest.mock import MagicMock
from finpay_topup_pipeline.reconcile import reconcile_xlsx


class TestReconcile(unittest.TestCase):
    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            reconcile_xlsx(MagicMock(), "missing.xlsx")
