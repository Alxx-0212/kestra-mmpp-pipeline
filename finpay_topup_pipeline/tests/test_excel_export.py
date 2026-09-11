import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock

from openpyxl import load_workbook

from finpay_topup_pipeline.excel_export import HEADERS, build_finance_workbook


class TestFinanceExcelExport(unittest.TestCase):
    def test_builds_finance_style_rows_with_running_saldo(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (Decimal("1000"), date(2026, 8, 20))
        cur.fetchall.return_value = [
            (
                1,
                datetime(2026, 8, 20, 1, tzinfo=timezone.utc),
                "sender",
                "receiver",
                "Kredit",
                Decimal("100"),
                "IDR",
                "setor",
                "source.csv",
                "hash-1",
                "DigiPOS",
                "BUNGKU",
            ),
            (
                2,
                datetime(2026, 8, 20, 2, tzinfo=timezone.utc),
                "sender",
                "receiver",
                "Debit",
                Decimal("50"),
                "IDR",
                "topup",
                "source.csv",
                "hash-2",
                "DigiPOS",
                None,
            ),
        ]

        result = build_finance_workbook(
            conn,
            "2026-08-20",
            "2026-08-20",
            ["421318"],
        )

        workbook = load_workbook(BytesIO(result["content"]), data_only=True)
        sheet = workbook["Morowali"]
        self.assertEqual(tuple(cell.value for cell in sheet[1]), HEADERS)
        self.assertEqual(sheet["E2"].value, 100)
        self.assertIsNone(sheet["F2"].value)
        self.assertEqual(sheet["G2"].value, 1100)
        self.assertEqual(sheet["F3"].value, 50)
        self.assertEqual(sheet["G3"].value, 1050)
        self.assertEqual(sheet["J2"].value, "BUNGKU")
        self.assertEqual(sheet["K2"].value, "DigiPOS")
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.auto_filter.ref, "A1:K3")
        self.assertEqual(result["row_count"], 2)
        query = cur.execute.call_args_list[1].args[0]
        self.assertIn("ORDER BY u.transaction_date ASC", query)
        self.assertNotIn("ORDER BY u.transaction_date ASC,", query)

    def test_rejects_unbounded_or_unknown_exports(self):
        with self.assertRaises(ValueError):
            build_finance_workbook(MagicMock(), "2025-01-01", "2026-01-02", ["421318"])
        with self.assertRaises(ValueError):
            build_finance_workbook(MagicMock(), "2026-01-01", "2026-01-02", ["999999"])
