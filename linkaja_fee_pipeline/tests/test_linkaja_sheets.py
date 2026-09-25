from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from linkaja_fee_pipeline.sheets import (
    _protection_editor_emails,
    write_linkaja_monthly_to_gsheet,
)


class LinkAjaMonthlySheetTest(unittest.TestCase):
    def test_protection_editor_list_keeps_gspread_http_client_service_account(self):
        client = Mock()
        client.auth = None
        client.http_client.auth.service_account_email = "svc@example.invalid"

        with patch.dict(
            "os.environ",
            {"FINPAY_PROTECTION_EDITOR_EMAILS": "", "LINKAJA_PROTECTION_EDITOR_EMAILS": ""},
        ):
            editors = _protection_editor_emails(client)

        self.assertEqual(editors, ["svc@example.invalid"])

    def test_monthly_preview_sheet_displays_cutoff_and_unresolved_reversals(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.id = 7
        worksheet.row_count = 1000
        worksheet.col_count = 20
        spreadsheet.worksheet.return_value = worksheet
        spreadsheet.fetch_sheet_metadata.return_value = {
            "sheets": [{"properties": {"sheetId": 7}, "protectedRanges": []}]
        }
        client = Mock()
        client.auth.service_account_email = "writer@example.invalid"
        result = {
            "cluster_id": "411311",
            "report_month": "2026-08-01",
            "calculation_cutoff": "2026-09-21 00:00:00",
            "published": False,
            "transaction_rows": 1200,
            "unresolved_reversal_count": 1,
            "unresolved_reversal_rows": [{
                "REPORT DATE": "2026-08-01",
                "REVERSAL TRANSACTION ID": "REV-1",
                "ORIGINAL TRANSACTION ID": "ORIG-MISSING",
                "UNUSUAL REASON CODE": "UNRESOLVED_MISSING_ORIGINAL",
            }],
            "incomplete_fee_rows": 1,
            "monthly_payable_complete": False,
            "fee_rows": [{
                "FEE CATEGORY": "DIGIPOS B2B TRANSFER FEE",
                "GROSS TRANSACTION COUNT": 100,
                "REVERSED TRANSACTION COUNT": 2,
                "ACTIVE TRANSACTION COUNT": 98,
                "GROSS FEE": 20000,
                "REVERSED FEE": 400,
                "NET FEE": 19600,
                "MONTHLY PAYABLE FEE": 19600,
                "GROSS MISSING FEE COUNT": 0,
                "REVERSED MISSING FEE COUNT": 0,
                "ACTIVE MISSING FEE COUNT": 0,
                "CALCULATION STATUS": "COMPLETE",
            }],
        }

        with patch(
            "linkaja_fee_pipeline.sheets._open_or_create_spreadsheet",
            return_value=spreadsheet,
        ):
            output = write_linkaja_monthly_to_gsheet(
                client,
                "Salinan dari Monitoring Finpay & LinkAja",
                result,
            )

        self.assertEqual(output["worksheet"], "LINKAJA MONTHLY 411311 2026-08")
        self.assertEqual(output["publication_status"], "PREVIEW - NOT FINAL")
        worksheet.clear.assert_called_once()
        worksheet.update.assert_called_once()
        updated_values = worksheet.update.call_args.kwargs["values"]
        rendered = str(updated_values)
        self.assertIn("PREVIEW - NOT FINAL", rendered)
        self.assertIn("ORIG-MISSING", rendered)
        self.assertIn("UNRESOLVED REVERSALS", rendered)
        requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        self.assertTrue(any("updateSheetProperties" in request for request in requests))
        self.assertTrue(any("addProtectedRange" in request for request in requests))


if __name__ == "__main__":
    unittest.main()
