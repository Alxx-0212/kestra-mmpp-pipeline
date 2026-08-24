import unittest
from finpay_topup_pipeline.io_csv import parse_amount, parse_transaction_date, parse_topup_csv
from finpay_topup_pipeline.io_xlsx import parse_morowali_xlsx
from finpay_topup_pipeline.loader import row_hash
import tempfile
import csv


class TestParseAmount(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(parse_amount("124000"), 124000)
        self.assertEqual(parse_amount("1,234.50"), 1234.50)

    def test_empty(self):
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount(None))

    def test_dirty(self):
        self.assertEqual(parse_amount("IDR 1,000"), 1000)


class TestParseTransactionDate(unittest.TestCase):
    def test_common(self):
        self.assertIsNotNone(parse_transaction_date("2026-08-13 17:15:05"))
        self.assertIsNotNone(parse_transaction_date("2025-08-07 00:00:0"))

    def test_empty(self):
        self.assertIsNone(parse_transaction_date(""))
        self.assertIsNone(parse_transaction_date(None))


class TestRowHash(unittest.TestCase):
    def test_stable(self):
        a = {"transaction_date": None, "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 100, "remarks": ""}
        b = {"transaction_date": None, "sender": None, "receiver": None, "transaction_type": "Debit", "amount": 100, "remarks": None}
        self.assertEqual(row_hash("411311", a), row_hash("411311", b))

    def test_differs(self):
        a = {"transaction_date": None, "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 100, "remarks": ""}
        b = {"transaction_date": None, "sender": "", "receiver": "", "transaction_type": "Kredit", "amount": 100, "remarks": ""}
        self.assertNotEqual(row_hash("411311", a), row_hash("411311", b))


class TestParseTopupCsv(unittest.TestCase):
    def test_round_trip_like(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["No", "Transaction Date", "Sender", "Receiver", "Transaction Type", "Amount", "Currency", "Remarks"])
            writer.writeheader()
            writer.writerow({"No": 1, "Transaction Date": "2026-08-13 17:15:05", "Sender": "628115209723", "Receiver": "082250519745", "Transaction Type": "Debit", "Amount": "255150", "Currency": "IDR", "Remarks": "Top Up balance via SF 255150"})
            path = f.name
        try:
            rows = parse_topup_csv(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["transaction_type"], "Debit")
            self.assertEqual(rows[0]["amount"], 255150)
        finally:
            import os
            os.unlink(path)


class TestParseMorowaliXlsx(unittest.TestCase):
    def test_reads_sheets(self):
        path = "data/FINPAY MOROWALI.xlsx"
        try:
            parsed = parse_morowali_xlsx(path)
        except FileNotFoundError:
            self.skipTest("legacy xlsx missing")
        self.assertIn("txns", parsed)
        self.assertIn("openings", parsed)
        self.assertGreaterEqual(len(parsed["txns"]), 1)
        self.assertEqual(parsed["txns"][0]["currency"], "IDR")
