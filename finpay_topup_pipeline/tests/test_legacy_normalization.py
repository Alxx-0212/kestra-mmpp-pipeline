import unittest

from finpay_topup_pipeline.legacy_outlet import normalize_outlet, outlet_code, row_outlet_candidates


class TestLegacyOutletNormalization(unittest.TestCase):
    def test_exact_and_annotated_labels(self):
        self.assertEqual(normalize_outlet("BUNGKU", "421318"), ("BUNGKU", "exact"))
        self.assertEqual(
            normalize_outlet("BUNGKU KURANG 1.500", "421318"),
            ("BUNGKU", "derived"),
        )
        self.assertEqual(normalize_outlet("BCAN-OBI", "421307"), ("BACAN-OBI", "derived"))

    def test_non_outlet_accounting_notes_are_unresolved(self):
        self.assertEqual(normalize_outlet("HO", "421318"), (None, "ambiguous"))
        self.assertEqual(normalize_outlet("BAHODOPI - BUNGKU", "421318"), (None, "ambiguous"))
        self.assertEqual(normalize_outlet("AMPANA", "411311"), (None, "ambiguous"))

    def test_row_candidates_keep_only_known_outlet_values(self):
        row = ("2026-08-10", "remarks", "BUNGKU", "HO")
        self.assertEqual(
            row_outlet_candidates(row, 1, "421318"),
            [("BUNGKU", "exact", "BUNGKU")],
        )
        self.assertEqual(outlet_code("421318", "BUNGKU"), "LEGACY_421318_BUNGKU")
