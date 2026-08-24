import unittest
from datetime import datetime
from finpay_topup_pipeline.loader import load_rows, row_hash


class _FakeCursor:
    def __init__(self, rowcounts):
        self.rowcounts = list(rowcounts)
        self.idx = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        rc = self.rowcounts[self.idx] if self.idx < len(self.rowcounts) else 0
        self.idx += 1
        self.rowcount = rc


class _FakeConn:
    def __init__(self, rowcounts):
        self.rowcounts = rowcounts

    def cursor(self):
        return _FakeCursor(self.rowcounts)

    def commit(self):
        pass


class TestLoadRows(unittest.TestCase):
    def test_insert_then_idempotent(self):
        rows = [
            {"transaction_date": datetime(2026, 8, 1, 8), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 1000, "currency": "IDR", "remarks": ""},
            {"transaction_date": datetime(2026, 8, 1, 9), "sender": "", "receiver": "", "transaction_type": "Kredit", "amount": 500, "currency": "IDR", "remarks": ""},
        ]
        first = load_rows(_FakeConn([1, 1]), rows, "411311", "src.csv")
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(first["seen"], 2)
        second = load_rows(_FakeConn([0, 0]), rows, "411311", "src.csv")
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["seen"], 2)
