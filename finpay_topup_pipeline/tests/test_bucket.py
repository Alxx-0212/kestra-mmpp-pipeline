import unittest

from finpay_topup_pipeline.bucket import parse_bucket_topup


class TestBucketTopupParsing(unittest.TestCase):
    def test_parse_bucket_topup_value(self):
        value, text = parse_bucket_topup("<div>BUCKET TOP UP Rp 1.234.567</div>")
        self.assertEqual(value, 1234567)
        self.assertIn("BUCKET TOP UP", text)

    def test_parse_comma_thousands(self):
        value, _ = parse_bucket_topup("<div>BUCKET TOP UP IDR 1,234,567</div>")
        self.assertEqual(value, 1234567)

    def test_missing_bucket_topup_raises(self):
        with self.assertRaises(ValueError):
            parse_bucket_topup("<div>saldo lain Rp 1.000</div>")
