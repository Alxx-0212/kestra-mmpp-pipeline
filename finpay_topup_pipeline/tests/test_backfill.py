import unittest
from unittest.mock import MagicMock
from finpay_topup_pipeline.backfill import backfill_xlsx


class TestBackfillXlsx(unittest.TestCase):
    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            backfill_xlsx(MagicMock(), "missing.xlsx")
