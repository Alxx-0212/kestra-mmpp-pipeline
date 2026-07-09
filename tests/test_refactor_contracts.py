import unittest
from unittest.mock import Mock, patch

try:
    import pandas as pd
    from finpay_pipeline import classification, detail_exports, unusual_sheets
except ModuleNotFoundError as exc:
    pd = None
    classification = None
    detail_exports = None
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


if __name__ == "__main__":
    unittest.main()
