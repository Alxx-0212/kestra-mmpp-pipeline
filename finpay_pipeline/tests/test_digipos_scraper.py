from datetime import date
import unittest

from finpay_pipeline.digipos_scraper import (
    CLUSTERS,
    HEADERS,
    expected_source_filename,
    parse_datatable_total,
    parse_report_date,
    validate_export_rows,
)


def sample_rows(transaction_date="28-09-2026 12:30:00", nomor_rs="628123456789"):
    return [
        ["DigiPOS CMS - Riwayat Saldo"],
        list(HEADERS),
        [1, transaction_date, "TX-1", 100, 10, 0, 110, "CREDIT", "RECHARGE", nomor_rs, "OK"],
    ]


class DigiPosExportValidationTest(unittest.TestCase):
    def test_report_date_and_canonical_finpay_filename(self):
        self.assertEqual(parse_report_date("2026-09-28"), date(2026, 9, 28))
        self.assertEqual(
            expected_source_filename("411311", "2026-09-28"),
            "finpay-411311(28-09-2026to28-09-2026).xlsx",
        )

    def test_all_six_expected_clusters_are_configured(self):
        self.assertEqual(CLUSTERS, ("411311", "421306", "421307", "421315", "421318", "421320"))

    def test_accepts_matching_header_count_date_and_identifier(self):
        self.assertEqual(validate_export_rows(sample_rows(), "2026-09-28", 1), 1)

    def test_rejects_row_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "row mismatch"):
            validate_export_rows(sample_rows(), "2026-09-28", 2)

    def test_rejects_wrong_transaction_date(self):
        with self.assertRaisesRegex(ValueError, "outside requested date"):
            validate_export_rows(sample_rows("27-09-2026 23:59:59"), "2026-09-28", 1)

    def test_rejects_header_only_export(self):
        with self.assertRaisesRegex(ValueError, "header-only"):
            validate_export_rows([list(HEADERS)], "2026-09-28", 0)

    def test_rejects_scientific_notation_nomor_rs(self):
        with self.assertRaisesRegex(ValueError, "scientific notation"):
            validate_export_rows(sample_rows(nomor_rs="6.281234E+11"), "2026-09-28", 1)

    def test_rejects_missing_or_extra_schema_columns(self):
        rows = sample_rows()
        rows[1] = rows[1][:-1]
        rows[2] = rows[2][:-1]
        with self.assertRaisesRegex(ValueError, "Unexpected FinPay XLSX columns"):
            validate_export_rows(rows, "2026-09-28", 1)

    def test_parses_datatables_thousands_separators(self):
        self.assertEqual(parse_datatable_total("Showing 1 to 10 of 3,111 entries"), 3111)


if __name__ == "__main__":
    unittest.main()
