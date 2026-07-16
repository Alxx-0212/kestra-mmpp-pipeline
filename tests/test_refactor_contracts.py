import unittest
from unittest.mock import Mock, patch

try:
    import pandas as pd
    from finpay_pipeline import (
        classification,
        detail_exports,
        summary_sheets,
        unusual_sheets,
    )
except ModuleNotFoundError as exc:
    pd = None
    classification = None
    detail_exports = None
    summary_sheets = None
    unusual_sheets = None
    RUNTIME_IMPORT_ERROR = exc
else:
    RUNTIME_IMPORT_ERROR = None


requires_runtime_dependencies = unittest.skipIf(
    RUNTIME_IMPORT_ERROR is not None,
    f"project runtime dependencies are not installed: {RUNTIME_IMPORT_ERROR}",
)


def _base_rows(transactions):
    rows = []
    for idx, transaction in enumerate(transactions, start=1):
        rows.append({
            "No": idx,
            "Transaction Date": pd.Timestamp("2026-06-04 10:00:00"),
            "Transaction ID": "ABC123" if idx == 1 else "ABC123FEE",
            "Saldo Awal": 1000,
            "Kredit": 100 if idx == 1 else 20,
            "Debet": 0,
            "Saldo Akhir": 1120,
            "Transaction Type": "CREDIT",
            "Transaction": transaction,
            "raw_transaction_label": "REVERSAL",
            "processed_transaction_label": transaction,
            "Nomor RS": "",
            "Remarks": (
                "biaya pembelian recharge"
                if idx == 1
                else "platform fee recharge rp. 20,-"
            ),
        })
    return pd.DataFrame(rows)


@requires_runtime_dependencies
class PreprocessedReversalContractTest(unittest.TestCase):
    def test_reversal_summary_trusts_preprocessed_labels(self):
        df = _base_rows([
            classification.REVERSAL_NGRS_CATEGORY,
            classification.REVERSAL_NGRS_FEE_CATEGORY,
        ])

        with patch(
            "finpay_pipeline.classification.relabel_reversal_transactions",
            side_effect=AssertionError("should not relabel preprocessed rows"),
        ):
            summary_ready, unusual = classification.prepare_reversal_summary_transactions(df)

        self.assertEqual(len(summary_ready), 2)
        self.assertTrue(unusual.empty)
        self.assertEqual(
            summary_ready["Transaction"].tolist(),
            [
                classification.REVERSAL_NGRS_CATEGORY,
                classification.REVERSAL_NGRS_FEE_CATEGORY,
            ],
        )

    def test_reversal_detail_trusts_preprocessed_labels(self):
        df = _base_rows([
            classification.REVERSAL_NGRS_CATEGORY,
            classification.REVERSAL_NGRS_FEE_CATEGORY,
        ])

        with patch(
            "finpay_pipeline.classification.relabel_reversal_transactions",
            side_effect=AssertionError("should not relabel preprocessed rows"),
        ):
            detail = detail_exports.prepare_reversal_detail_export(df)

        self.assertEqual(len(detail), 2)
        self.assertEqual(
            detail["Transaction"].tolist(),
            [
                classification.REVERSAL_NGRS_CATEGORY,
                classification.REVERSAL_NGRS_FEE_CATEGORY,
            ],
        )


@requires_runtime_dependencies
class SharedSpreadsheetOpenContractTest(unittest.TestCase):
    def test_unusual_upload_uses_shared_spreadsheet_open(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.get_all_values.return_value = []
        worksheet.id = 123
        worksheet.row_count = 100
        spreadsheet.worksheet.return_value = worksheet

        with patch(
            "finpay_pipeline.unusual_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ) as opener:
            result = unusual_sheets.process_unusual_upload(
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY - Unusual",
                unusual_df=pd.DataFrame(),
                gspread_client=Mock(),
                report_date="2026-06-04",
            )

        self.assertTrue(result)
        opener.assert_called_once()

    def test_daily_upload_initializes_linkaja_reference_sheet(self):
        spreadsheet = Mock()
        summary_worksheet = Mock()
        summary_worksheet.acell.return_value.value = "REPORT DATE"
        linkaja_worksheet = Mock()
        linkaja_worksheet.id = 456
        linkaja_worksheet.row_count = 1000
        linkaja_worksheet.col_count = 29

        def worksheet_by_title(title):
            if title == "PKY":
                return summary_worksheet
            if title == "LinkAja":
                raise summary_sheets.gspread.WorksheetNotFound()
            raise AssertionError(title)

        spreadsheet.worksheet.side_effect = worksheet_by_title
        spreadsheet.add_worksheet.return_value = linkaja_worksheet

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ), patch(
            "finpay_pipeline.summary_sheets.append_daily_to_gsheet",
            return_value=(3, 10),
        ):
            result = summary_sheets.process_daily_upload(
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=pd.DataFrame(),
                starting_balance_date="2026-06-30",
                default_starting_balance=0,
                gspread_client=Mock(),
            )

        self.assertEqual(result, (3, 10))
        spreadsheet.add_worksheet.assert_called_once_with(
            title="LinkAja",
            rows=1000,
            cols=29,
        )
        linkaja_worksheet.get.assert_not_called()
        linkaja_worksheet.update.assert_not_called()
        batch_requests = spreadsheet.batch_update.call_args.kwargs.get("requests")
        if batch_requests is None:
            batch_requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        update_cells = next(
            request["updateCells"]
            for request in batch_requests
            if "updateCells" in request
        )
        row_values = update_cells["rows"]
        self.assertEqual(
            row_values[0]["values"][10]["userEnteredValue"]["stringValue"],
            "PKY - 411311",
        )
        self.assertEqual(
            row_values[1]["values"][10]["userEnteredValue"]["stringValue"],
            "REPORT DATE",
        )
        self.assertEqual(
            row_values[1]["values"][12]["userEnteredValue"]["stringValue"],
            "DIGIPOS B2B TRANSFER IN CLUSTER FEE",
        )
        self.assertEqual(
            sum(1 for request in batch_requests if "mergeCells" in request),
            6,
        )
        self.assertEqual(
            sum(1 for request in batch_requests if "updateBorders" in request),
            6,
        )
        title_format = next(
            request["repeatCell"]
            for request in batch_requests
            if (
                "repeatCell" in request
                and request["repeatCell"]["range"]["startRowIndex"] == 0
                and request["repeatCell"]["range"]["startColumnIndex"] == 10
            )
        )
        self.assertEqual(
            title_format["cell"]["userEnteredFormat"]["horizontalAlignment"],
            "CENTER",
        )
        self.assertEqual(
            title_format["cell"]["userEnteredFormat"]["backgroundColor"],
            summary_sheets.LINKAJA_REFERENCE_CLUSTER_PALETTES[2]["title"],
        )

    def test_daily_upload_repairs_existing_linkaja_header_format(self):
        spreadsheet = Mock()
        summary_worksheet = Mock()
        summary_worksheet.acell.return_value.value = "REPORT DATE"
        linkaja_worksheet = Mock()
        linkaja_worksheet.id = 456
        linkaja_worksheet.row_count = 1000
        linkaja_worksheet.col_count = 29
        linkaja_worksheet.get.return_value = [
            *summary_sheets._linkaja_reference_header_values(),
            ["" for _ in range(summary_sheets.LINKAJA_REFERENCE_COLUMN_COUNT)],
        ]

        def worksheet_by_title(title):
            if title == "PKY":
                return summary_worksheet
            if title == "LinkAja":
                return linkaja_worksheet
            raise AssertionError(title)

        spreadsheet.worksheet.side_effect = worksheet_by_title

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ), patch(
            "finpay_pipeline.summary_sheets.append_daily_to_gsheet",
            return_value=(3, 10),
        ):
            result = summary_sheets.process_daily_upload(
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=pd.DataFrame(),
                starting_balance_date="2026-06-30",
                default_starting_balance=0,
                gspread_client=Mock(),
            )

        self.assertEqual(result, (3, 10))
        spreadsheet.add_worksheet.assert_not_called()
        linkaja_worksheet.update.assert_not_called()
        linkaja_worksheet.get.assert_called_once_with(
            "A1:AC3",
            value_render_option="FORMATTED_VALUE",
        )
        batch_requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        self.assertFalse(
            any("updateCells" in request for request in batch_requests)
        )
        self.assertEqual(
            sum(1 for request in batch_requests if "unmergeCells" in request),
            6,
        )
        self.assertEqual(
            sum(1 for request in batch_requests if "mergeCells" in request),
            6,
        )
        self.assertEqual(
            sum(1 for request in batch_requests if "updateBorders" in request),
            6,
        )

    def test_daily_upload_skips_linkaja_format_when_data_exists(self):
        spreadsheet = Mock()
        summary_worksheet = Mock()
        summary_worksheet.acell.return_value.value = "REPORT DATE"
        linkaja_worksheet = Mock()
        linkaja_worksheet.id = 456
        linkaja_worksheet.row_count = 1000
        linkaja_worksheet.col_count = 29
        linkaja_data_row = [
            "01/07/2026",
            "100",
            "200",
            "300",
            "",
            "01/07/2026",
        ]
        linkaja_worksheet.get.return_value = [
            *summary_sheets._linkaja_reference_header_values(),
            linkaja_data_row,
        ]

        def worksheet_by_title(title):
            if title == "PKY":
                return summary_worksheet
            if title == "LinkAja":
                return linkaja_worksheet
            raise AssertionError(title)

        spreadsheet.worksheet.side_effect = worksheet_by_title

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ), patch(
            "finpay_pipeline.summary_sheets.append_daily_to_gsheet",
            return_value=(3, 10),
        ):
            result = summary_sheets.process_daily_upload(
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=pd.DataFrame(),
                starting_balance_date="2026-06-30",
                default_starting_balance=0,
                gspread_client=Mock(),
            )

        self.assertEqual(result, (3, 10))
        spreadsheet.add_worksheet.assert_not_called()
        spreadsheet.batch_update.assert_not_called()
        linkaja_worksheet.update.assert_not_called()

    def test_detail_upload_uses_shared_spreadsheet_open(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.get_all_values.return_value = []
        worksheet.id = 123
        worksheet.row_count = 100
        spreadsheet.worksheet.return_value = worksheet

        with patch(
            "finpay_pipeline.detail_exports.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ) as opener:
            result = detail_exports.process_transaction_detail_upload(
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY - Reversal",
                detail_df=pd.DataFrame(),
                gspread_client=Mock(),
                report_date="2026-06-04",
            )

        self.assertTrue(result)
        opener.assert_called_once()

    def test_summary_upload_batches_formatting_requests(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.id = 123
        worksheet.row_count = 1000
        worksheet.get_all_values.return_value = [
            [
                "REPORT DATE",
                "SECTION",
                "KETERANGAN",
                "DEBET",
                "KREDIT",
                "SALDO",
                "",
                "REPORT DATE",
                "INVOICE REPORT",
                "DEBET",
                "KREDIT",
            ],
            ["30/06/2026", "", "SALDO 30/06/2026", "", "", "0"],
        ]
        spreadsheet.worksheet.return_value = worksheet
        spreadsheet.fetch_sheet_metadata.return_value = {
            "sheets": [{
                "properties": {"sheetId": worksheet.id},
                "protectedRanges": [],
            }]
        }
        summary_df = pd.DataFrame([{
            "Transaction_Date": "2026-07-01",
            "Transaction": "RECHARGE",
            "Sum_of_Debet": 0,
            "Sum_of_Kredit": 100,
            "Transaction_Count": 1,
        }])

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ):
            result = summary_sheets.append_daily_to_gsheet(
                gspread_client=Mock(),
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=summary_df,
            )

        self.assertEqual(result[0], 3)
        worksheet.update.assert_called_once()
        worksheet.format.assert_not_called()
        spreadsheet.batch_update.assert_called_once()
        batch_requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        self.assertTrue(any(
            "updateCells" in request
            and request["updateCells"]["range"]["startRowIndex"] == 0
            and request["updateCells"]["range"]["endColumnIndex"] == 11
            for request in batch_requests
        ))
        self.assertGreater(
            sum(1 for request in batch_requests if "repeatCell" in request),
            10,
        )

    def test_summary_rerun_batches_replacement_structure(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.id = 123
        worksheet.row_count = 1000
        worksheet.get_all_values.return_value = [
            [
                "REPORT DATE",
                "SECTION",
                "KETERANGAN",
                "DEBET",
                "KREDIT",
                "SALDO",
                "",
                "REPORT DATE",
                "INVOICE REPORT",
                "DEBET",
                "KREDIT",
            ],
            ["30/06/2026", "", "SALDO 30/06/2026", "", "", "0"],
            ["01/07/2026", "DETAIL", "NGRS", "100", "", "100"],
            ["01/07/2026", "Cash", "SELISIH", "", "", ""],
        ]
        spreadsheet.worksheet.return_value = worksheet
        merged_invoice_range = {
            "sheetId": worksheet.id,
            "startRowIndex": 1,
            "endRowIndex": 5,
            "startColumnIndex": 7,
            "endColumnIndex": 11,
        }
        spreadsheet.fetch_sheet_metadata.side_effect = [
            {
                "sheets": [{
                    "properties": {"sheetId": worksheet.id},
                    "merges": [merged_invoice_range],
                }]
            },
            {
                "sheets": [{
                    "properties": {"sheetId": worksheet.id},
                    "protectedRanges": [],
                }]
            },
        ]
        summary_df = pd.DataFrame([{
            "Transaction_Date": "2026-07-01",
            "Transaction": "RECHARGE",
            "Sum_of_Debet": 0,
            "Sum_of_Kredit": 100,
            "Transaction_Count": 1,
        }])

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ):
            result = summary_sheets.append_daily_to_gsheet(
                gspread_client=Mock(),
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=summary_df,
            )

        self.assertEqual(result[0], 3)
        worksheet.get_all_values.assert_called_once()
        worksheet.update.assert_called_once()
        worksheet.format.assert_not_called()
        self.assertEqual(spreadsheet.batch_update.call_count, 2)
        structure_requests = spreadsheet.batch_update.call_args_list[0].args[0][
            "requests"
        ]
        self.assertTrue(
            any("unmergeCells" in request for request in structure_requests)
        )
        unmerge_request = next(
            request["unmergeCells"]
            for request in structure_requests
            if "unmergeCells" in request
        )
        self.assertEqual(unmerge_request["range"], merged_invoice_range)
        self.assertTrue(
            any("deleteDimension" in request for request in structure_requests)
        )
        self.assertTrue(
            any("insertDimension" in request for request in structure_requests)
        )
        final_requests = spreadsheet.batch_update.call_args_list[1].args[0][
            "requests"
        ]
        self.assertGreater(
            sum(1 for request in final_requests if "repeatCell" in request),
            10,
        )


@requires_runtime_dependencies
class QrisduwitInvoiceRowsContractTest(unittest.TestCase):
    def test_linkaja_reference_headers_match_static_invoice_columns(self):
        rows = summary_sheets._linkaja_reference_header_values()

        self.assertEqual(rows[0][0], "MRT - 421306")
        self.assertEqual(rows[1][0:4], [
            "REPORT DATE",
            "EXPECTED RECHARGE OUT CLUSTER FEE",
            "DIGIPOS B2B TRANSFER IN CLUSTER FEE",
            "TOTAL FEE",
        ])
        self.assertEqual(rows[0][10], "PKY - 411311")
        self.assertEqual(rows[1][10:14], rows[1][0:4])
        self.assertEqual(rows[0][25], "TNT - 421320")

    def test_qrisduwit_invoice_rows_group_by_disbursement_date(self):
        rows = summary_sheets._build_qrisduwit_invoice_rows(pd.DataFrame({
            "Disbursement Date": ["05/06/2026", "04/06/2026", "04/06/2026"],
            "Kredit": [200, 100, 50],
        }))

        self.assertEqual(rows, [
            ("QRISDUWIT - 04/06/2026", 150, ""),
            ("QRISDUWIT - 05/06/2026", 200, ""),
        ])

    def test_qrisduwit_invoice_rows_group_missing_disbursement_date(self):
        rows = summary_sheets._build_qrisduwit_invoice_rows(pd.DataFrame({
            "Disbursement Date": ["", None],
            "Kredit": [100, 25],
        }))

        self.assertEqual(rows, [
            ("QRISDUWIT - MISSING DISBURSEMENT DATE", 125, ""),
        ])

    def test_qrisduwit_invoice_rows_allow_missing_optional_dataframe(self):
        self.assertEqual(summary_sheets._build_qrisduwit_invoice_rows(None), [])

    def test_qrisduwit_invoice_rows_embed_under_cash_parent(self):
        rows = summary_sheets._cash_invoice_rows_with_qrisduwit(
            [("NGRS", "=D1", "")],
            [("QRISDUWIT - 04/06/2026", 150, "")],
        )

        self.assertEqual(rows, [
            ("NGRS", "=D1", ""),
            ("QRISDUWIT", "", ""),
            ("QRISDUWIT - 04/06/2026", 150, ""),
        ])

    def test_linkaja_fee_invoice_rows_use_static_cluster_columns(self):
        rows = summary_sheets._build_linkaja_fee_invoice_rows("PKY", "01/07/2026")

        self.assertEqual(rows[0][0], "LINKAJA EXPECTED RECHARGE OUT CLUSTER FEE")
        self.assertEqual(rows[0][1], "")
        self.assertIn("'LinkAja'!L:L", rows[0][2])
        self.assertIn("'LinkAja'!K:K", rows[0][2])
        self.assertIn("SUMIF(", rows[0][2])
        self.assertIn("DATE(2026,7,1)", rows[0][2])
        self.assertNotIn("FILTER(", rows[0][2])
        self.assertEqual(rows[1][0], "LINKAJA DIGIPOS B2B TRANSFER IN CLUSTER FEE")
        self.assertEqual(rows[1][1], "")
        self.assertIn("'LinkAja'!M:M", rows[1][2])
        self.assertIn("'LinkAja'!K:K", rows[1][2])
        self.assertIn("DATE(2026,7,1)", rows[1][2])
        self.assertNotIn("TEXT(", rows[1][2])

    def test_linkaja_fee_invoice_rows_are_before_qrisduwit_rows(self):
        cash_rows = [
            ("NGRS", "=D1", ""),
            *summary_sheets._build_linkaja_fee_invoice_rows("TDR", "01/07/2026"),
        ]
        rows = summary_sheets._cash_invoice_rows_with_qrisduwit(
            cash_rows,
            [("QRISDUWIT - 04/06/2026", 150, "")],
        )

        labels = [row[0] for row in rows]
        self.assertEqual(labels[-2:], ["QRISDUWIT", "QRISDUWIT - 04/06/2026"])
        self.assertLess(
            labels.index("LINKAJA DIGIPOS B2B TRANSFER IN CLUSTER FEE"),
            labels.index("QRISDUWIT"),
        )

    def test_mandiri_formula_uses_row_relative_indirect_ranges(self):
        formula = summary_sheets._next_transfer_mandiri_formula()

        self.assertIn('INDIRECT("E"&ROW()+2&":E")', formula)
        self.assertIn('INDIRECT("F"&ROW()+2&":F")', formula)
        self.assertIn('INDIRECT("D"&ROW()+2&":D")', formula)
        self.assertNotRegex(formula, r"[A-Z]+\\d+:[A-Z]+")


if __name__ == "__main__":
    unittest.main()
