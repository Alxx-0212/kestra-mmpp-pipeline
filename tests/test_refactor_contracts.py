import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from finpay_pipeline import (
    classification,
    detail_exports,
    loading,
    monthly,
    sheets_common,
    summary_sheets,
    unusual_sheets,
)


class FinPayInputFormatContractTest(unittest.TestCase):
    def test_current_csv_and_xlsx_extensions_are_accepted(self):
        self.assertEqual(loading._detect_file_type("finpay-export.csv"), ".csv")
        self.assertEqual(loading._detect_file_type("finpay-export.xlsx"), ".xlsx")

    def test_legacy_xls_extension_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            r"Convert the file to \.xlsx or \.csv",
        ):
            loading._detect_file_type("finpay-411311(01-01-2026to01-01-2026).xls")

    def test_legacy_xls_magic_is_rejected_without_xls_extension(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "finpay-export.bin"
            path.write_bytes(loading.LEGACY_XLS_MAGIC + b"legacy workbook")

            with self.assertRaisesRegex(
                ValueError,
                r"Convert the file to \.xlsx or \.csv",
            ):
                loading._detect_file_type(str(path))


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
                "Biaya Pembelian recharge sejumlah 100 rupiah, "
                "dari 081234567890 ke 411311"
                if idx == 1
                else "platform fee recharge rp. 20,-"
            ),
        })
    return pd.DataFrame(rows)


def _non_reversal_rows(transactions):
    known_remarks = {
        "QRISDUWIT": (
            "Disburse Qris Duwit atas Transaksi pembayaran QRIS pada tanggal "
            "03-06-2026 sejumlah Rp 15000,00"
        ),
        "RECHARGE": (
            "Biaya Pembelian recharge sejumlah 100000 rupiah, "
            "dari 081234567890 ke 411311"
        ),
        "SELLTHRU": "Sellthru Sales Fee",
    }
    rows = []
    for idx, transaction in enumerate(transactions, start=1):
        rows.append({
            "No": idx,
            "Transaction Date": pd.Timestamp("2026-06-04 10:00:00"),
            "Transaction ID": f"TXN{idx}",
            "Saldo Awal": 1000,
            "Kredit": 100,
            "Debet": 0,
            "Saldo Akhir": 1100,
            "Transaction Type": "CREDIT",
            "Transaction": transaction,
            "raw_transaction_label": transaction,
            "processed_transaction_label": transaction,
            "Nomor RS": "",
            "Remarks": known_remarks.get(transaction, ""),
        })
    return pd.DataFrame(rows)


def _sellthru_family_rows(
    transaction_date="2026-09-02 10:00:00",
    *,
    fee_amount=100,
    include_sales_fee=False,
    standalone=False,
):
    rows = [{
        "No": 1,
        "Transaction Date": pd.Timestamp(transaction_date),
        "Transaction ID": "STFAMILY",
        "Saldo Awal": 1000,
        "Kredit": 1000,
        "Debet": 0,
        "Saldo Akhir": 2000,
        "Transaction Type": "CREDIT",
        "Transaction": "RECHARGE" if standalone else "SELLTHRU",
        "raw_transaction_label": "RECHARGE" if standalone else "SELLTHRU",
        "processed_transaction_label": "RECHARGE" if standalone else "SELLTHRU",
        "Nomor RS": "",
        "Remarks": "TRANSAKSI SELLTHRU" if standalone else "Sellthru Sales Fee",
    }]
    if not standalone:
        rows.append({
            **rows[0],
            "No": 2,
            "Transaction ID": "STFAMILYFEE",
            "Kredit": 0,
            "Debet": fee_amount,
            "Saldo Akhir": 900,
            "Transaction": "SELLTHRUFEE",
            "raw_transaction_label": "SELLTHRUFEE",
            "processed_transaction_label": "SELLTHRUFEE",
            "Remarks": "Platform fee sellthru rp. 100,-",
        })
        if include_sales_fee:
            rows.append({
                **rows[0],
                "No": 3,
                "Transaction ID": "STFAMILYSALESFEE",
                "Kredit": 0,
                "Debet": 10,
                "Saldo Akhir": 890,
                "Transaction": "SELLTHRUSALESFEE",
                "raw_transaction_label": "SELLTHRUSALESFEE",
                "processed_transaction_label": "SELLTHRUSALESFEE",
                "Remarks": "Sales hold transaksi sellthru sejumlah 10 rupiah, dari 411311",
            })
    return pd.DataFrame(rows)


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


class UnknownTransactionContractTest(unittest.TestCase):
    def test_known_summary_transaction_allowlist_is_explicit(self):
        self.assertEqual(
            classification.KNOWN_SUMMARY_TRANSACTION_LABELS,
            {
                "CASHOUT APOLLO",
                "QRISDUWIT",
                "DISBURSEMENT",
                "FeeTransaksi",
                "RECHARGE",
                "RECHARGEFEE",
                classification.PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
                "RECHARGE OUT CLUSTER",
                "RECHARGE OUT CLUSTER FEE",
                classification.REVERSAL_NGRS_CATEGORY,
                classification.REVERSAL_NGRS_FEE_CATEGORY,
                classification.REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
                classification.REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
                classification.REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY,
                "SELLTHRU",
                "SELLTHRUFEE",
                "SELLTHRUSALESFEE",
            },
        )

    def test_every_known_summary_transaction_has_remark_patterns(self):
        self.assertLessEqual(
            classification.KNOWN_SUMMARY_TRANSACTION_LABELS,
            set(classification.KNOWN_TRANSACTION_REMARK_PATTERNS),
        )

    def test_unknown_transaction_is_reported_as_unusual(self):
        df = _non_reversal_rows(["QRISDUWIT", "NEWPAY"])

        unusual = classification.flag_unusual_transactions(df)

        self.assertEqual(len(unusual), 1)
        self.assertEqual(unusual.iloc[0]["Transaction"], "NEWPAY")
        self.assertEqual(
            unusual.iloc[0]["unusual_reason"],
            "unknown transaction label: NEWPAY; excluded from summary",
        )

    def test_unknown_transaction_is_excluded_from_summary_ready_rows(self):
        df = _non_reversal_rows(["QRISDUWIT", "NEWPAY"])

        summary_ready, unusual = classification.prepare_reversal_summary_transactions(df)

        self.assertEqual(summary_ready["Transaction"].tolist(), ["QRISDUWIT"])
        self.assertEqual(len(unusual), 1)
        self.assertEqual(unusual.iloc[0]["Transaction"], "NEWPAY")
        self.assertEqual(
            unusual.iloc[0]["unusual_reason"],
            "unknown transaction label: NEWPAY; excluded from summary",
        )

    def test_known_transaction_with_unknown_remarks_is_reported_and_excluded(self):
        df = _non_reversal_rows(["QRISDUWIT"])
        df.loc[0, "Remarks"] = "Unexpected QRIS settlement wording"

        unusual = classification.flag_unusual_transactions(df)
        summary_ready, summary_unusual = (
            classification.prepare_reversal_summary_transactions(df)
        )

        self.assertEqual(len(unusual), 1)
        self.assertEqual(
            unusual.iloc[0]["unusual_reason"],
            "unknown remarks pattern for transaction: QRISDUWIT; "
            "excluded from summary",
        )
        self.assertEqual(
            unusual.iloc[0]["Remarks"],
            "Unexpected QRIS settlement wording",
        )
        self.assertTrue(summary_ready.empty)
        self.assertEqual(len(summary_unusual), 1)

    def test_variable_values_in_known_remarks_are_ignored(self):
        df = _non_reversal_rows(["QRISDUWIT", "QRISDUWIT"])
        df.loc[0, "Remarks"] = (
            "Disburse Qris Duwit atas Transaksi pembayaran QRIS pada tanggal "
            "01-05-2026 sejumlah Rp 180,00"
        )
        df.loc[1, "Remarks"] = (
            "Disburse Qris Duwit atas Transaksi pembayaran QRIS pada tanggal "
            "31-12-2027 sejumlah Rp 9876543,25"
        )

        unusual = classification.flag_unusual_transactions(df)
        summary_ready, summary_unusual = (
            classification.prepare_reversal_summary_transactions(df)
        )

        self.assertTrue(unusual.empty)
        self.assertEqual(len(summary_ready), 2)
        self.assertTrue(summary_unusual.empty)


class StandaloneSellthruClassificationContractTest(unittest.TestCase):
    def test_recharge_with_transaksi_sellthru_remarks_becomes_st(self):
        df = _non_reversal_rows(["RECHARGE"])
        df.loc[0, "Remarks"] = "Transaksi Sellthru"
        original_remarks = df.loc[0, "Remarks"]

        preprocessed = classification.preprocess_transaction_labels(df)
        preprocessed["raw_transaction_label"] = df["Transaction"]
        preprocessed["processed_transaction_label"] = preprocessed["Transaction"]
        unusual = classification.flag_unusual_transactions(preprocessed)
        summary_ready, summary_unusual = (
            classification.prepare_reversal_summary_transactions(preprocessed)
        )

        self.assertEqual(preprocessed.loc[0, "Transaction"], "SELLTHRU")
        self.assertEqual(preprocessed.loc[0, "raw_transaction_label"], "RECHARGE")
        self.assertEqual(preprocessed.loc[0, "Remarks"], original_remarks)
        self.assertTrue(unusual.empty)
        self.assertTrue(summary_unusual.empty)
        self.assertEqual(summary_ready["Transaction"].tolist(), ["SELLTHRU"])

    def test_other_recharge_with_sellthru_remarks_is_unusual(self):
        df = _non_reversal_rows(["RECHARGE"])
        df.loc[0, "Remarks"] = "Sellthru Sales Fee"
        preprocessed = classification.preprocess_transaction_labels(df)
        preprocessed["processed_transaction_label"] = preprocessed["Transaction"]

        unusual = classification.flag_unusual_transactions(preprocessed)
        summary_ready, _ = classification.prepare_reversal_summary_transactions(
            preprocessed,
        )

        self.assertEqual(preprocessed.loc[0, "Transaction"], "RECHARGE")
        self.assertEqual(len(unusual), 1)
        self.assertEqual(
            unusual.iloc[0]["unusual_reason"],
            "unknown remarks pattern for transaction: RECHARGE; excluded from summary",
        )
        self.assertTrue(summary_ready.empty)

    def test_standalone_sellthru_remains_fee_free_after_cutover(self):
        df = _sellthru_family_rows(standalone=True)
        df["Transaction"] = "recharge"
        df["raw_transaction_label"] = "recharge"
        preprocessed = classification.preprocess_transaction_labels(df)

        unusual = classification.flag_unusual_transactions(preprocessed)
        summary_ready, summary_unusual = (
            classification.prepare_reversal_summary_transactions(preprocessed)
        )

        self.assertTrue(unusual.empty)
        self.assertTrue(summary_unusual.empty)
        self.assertEqual(summary_ready["Transaction"].tolist(), ["SELLTHRU"])

    def test_standalone_sellthru_with_companion_is_quarantined(self):
        standalone = _sellthru_family_rows(standalone=True)
        companion = _sellthru_family_rows(standalone=False).iloc[[1]].copy()
        companion["No"] = 2
        df = pd.concat([standalone, companion], ignore_index=True)
        preprocessed = classification.preprocess_transaction_labels(df)

        unusual = classification.flag_unusual_transactions(preprocessed)
        summary_ready, _ = classification.prepare_reversal_summary_transactions(
            preprocessed,
        )

        self.assertEqual(len(unusual), 2)
        self.assertTrue(
            unusual["unusual_reason"].str.contains(
                "standalone SELLTHRU must not have fee",
                regex=False,
            ).all()
        )
        self.assertTrue(summary_ready.empty)

    def test_direct_sellthru_does_not_receive_recharge_exception(self):
        df = _sellthru_family_rows(standalone=True)
        df["Transaction"] = "SELLTHRU"
        df["raw_transaction_label"] = "SELLTHRU"
        df["processed_transaction_label"] = "SELLTHRU"
        preprocessed = classification.preprocess_transaction_labels(df)

        unusual = classification.flag_unusual_transactions(preprocessed)
        summary_ready, _ = classification.prepare_reversal_summary_transactions(
            preprocessed,
        )

        self.assertEqual(len(unusual), 1)
        self.assertIn("missing SELLTHRUFEE", unusual.iloc[0]["unusual_reason"])
        self.assertEqual(len(summary_ready), 1)


class SellthruEffectiveDateContractTest(unittest.TestCase):
    def _evaluate(self, df):
        preprocessed = classification.preprocess_transaction_labels(df)
        return (
            classification.flag_unusual_transactions(preprocessed),
            *classification.prepare_reversal_summary_transactions(preprocessed),
        )

    def test_legacy_sellthru_requires_sales_fee(self):
        df = _sellthru_family_rows(
            transaction_date="2026-08-31 23:59:59",
            include_sales_fee=True,
        )

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertTrue(unusual.empty)
        self.assertTrue(summary_unusual.empty)
        self.assertEqual(len(summary_ready), 3)

    def test_current_sellthru_requires_only_rp100_fee(self):
        df = _sellthru_family_rows(
            transaction_date="2026-09-01 00:00:00",
            include_sales_fee=False,
        )

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertTrue(unusual.empty)
        self.assertTrue(summary_unusual.empty)
        self.assertEqual(summary_ready["Transaction"].tolist(), ["SELLTHRU", "SELLTHRUFEE"])

    def test_current_sellthru_sales_fee_quarantines_whole_family(self):
        df = _sellthru_family_rows(
            transaction_date="2026-09-02 10:00:00",
            include_sales_fee=True,
        )

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertEqual(len(unusual), 3)
        self.assertTrue(summary_ready.empty)
        self.assertEqual(len(summary_unusual), 3)
        self.assertTrue(
            summary_unusual["unusual_reason"].str.contains(
                "SELLTHRUSALESFEE retired",
                regex=False,
            ).all()
        )

    def test_current_invalid_fee_is_flagged_but_remains_in_summary(self):
        df = _sellthru_family_rows(
            transaction_date="2026-09-02 10:00:00",
            fee_amount=80,
        )

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertEqual(len(unusual), 2)
        self.assertEqual(len(summary_ready), 2)
        self.assertEqual(len(summary_unusual), 2)
        self.assertTrue(
            summary_unusual["unusual_reason"].str.contains(
                "SELLTHRUFEE Debet=80",
                regex=False,
            ).all()
        )

    def test_unknown_remarks_quarantine_the_complete_family(self):
        df = _sellthru_family_rows(
            transaction_date="2026-09-02 10:00:00",
            fee_amount=100,
        )
        df.loc[df["Transaction"] == "SELLTHRUFEE", "Remarks"] = "unexpected fee"

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertEqual(len(unusual), 2)
        self.assertTrue(summary_ready.empty)
        self.assertEqual(len(summary_unusual), 2)

    def test_mixed_cutover_family_is_quarantined(self):
        df = _sellthru_family_rows(
            transaction_date="2026-08-31 23:59:59",
            include_sales_fee=True,
        )
        df.loc[df["Transaction"] == "SELLTHRUFEE", "Transaction Date"] = pd.Timestamp(
            "2026-09-01 00:00:00"
        )

        unusual, summary_ready, summary_unusual = self._evaluate(df)

        self.assertEqual(len(unusual), 3)
        self.assertTrue(summary_ready.empty)
        self.assertEqual(len(summary_unusual), 3)


class SellthruSheetLayoutContractTest(unittest.TestCase):
    def test_sheet_keeps_hold_row_before_cutover(self):
        summary = pd.DataFrame({
            "Transaction": ["SELLTHRU"],
            "Transaction_Date": ["2026-08-31"],
        })

        self.assertTrue(
            summary_sheets._uses_legacy_st_sales_fee_layout(
                summary,
                report_date="2026-08-31",
            )
        )

    def test_sheet_removes_hold_row_after_cutover(self):
        summary = pd.DataFrame({
            "Transaction": ["SELLTHRU", "SELLTHRUFEE"],
            "Transaction_Date": ["2026-09-01", "2026-09-01"],
        })

        self.assertFalse(
            summary_sheets._uses_legacy_st_sales_fee_layout(
                summary,
                report_date="2026-09-01",
            )
        )


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

        def append_after_linkaja_reference_sheet(*args, **kwargs):
            spreadsheet.add_worksheet.assert_called_once_with(
                title="LinkAja",
                rows=1000,
                cols=29,
            )
            return 3, 10

        with patch(
            "finpay_pipeline.summary_sheets.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ), patch(
            "finpay_pipeline.summary_sheets.append_daily_to_gsheet",
            side_effect=append_after_linkaja_reference_sheet,
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
        self.assertTrue(any(
            "repeatCell" in request
            and request["repeatCell"]["range"]["startRowIndex"] == 2
            and request["repeatCell"]["range"]["startColumnIndex"] == 0
            and request["repeatCell"]["range"]["endColumnIndex"] == 11
            and request["repeatCell"]["fields"]
            == (
                "userEnteredFormat.textFormat.bold,"
                "userEnteredFormat.textFormat.foregroundColor"
            )
            and request["repeatCell"]["cell"]["userEnteredFormat"]
            ["textFormat"]["bold"] is True
            and request["repeatCell"]["cell"]["userEnteredFormat"]
            ["textFormat"]["foregroundColor"] == {"red": 0, "green": 0, "blue": 0}
            for request in batch_requests
        ))

    def test_summary_protection_only_unprotects_mandiri_d_cells(self):
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
            ["30/06/2026", "MANDIRI", "NGRS", "100", "", ""],
            ["30/06/2026", "Cash", "MANDIRI", "100", "", ""],
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
            summary_sheets.append_daily_to_gsheet(
                gspread_client=Mock(),
                target_spreadsheet="FINPAY REPORT",
                target_worksheet="PKY",
                summary_df=summary_df,
            )

        batch_requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        protected_sheet = next(
            request["addProtectedRange"]["protectedRange"]
            for request in batch_requests
            if (
                "addProtectedRange" in request
                and request["addProtectedRange"]["protectedRange"]["range"]
                == {"sheetId": worksheet.id}
            )
        )
        unprotected_ranges = protected_sheet["unprotectedRanges"]
        self.assertTrue(unprotected_ranges)
        self.assertTrue(all(
            protected_range["startColumnIndex"] == 3
            and protected_range["endColumnIndex"] == 4
            for protected_range in unprotected_ranges
        ))
        self.assertFalse(any(
            protected_range["startColumnIndex"] == 2
            and protected_range["endColumnIndex"] == 3
            for protected_range in unprotected_ranges
        ))

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


class QrisduwitInvoiceRowsContractTest(unittest.TestCase):
    def test_sellthru_sales_fee_invoice_label_is_stable(self):
        self.assertEqual(
            summary_sheets.SELLTHRU_SALES_FEE_INVOICE_LABEL,
            "BIAYA FEE BAR A ST (HOLD)",
        )

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


class FinPayMonthlyTransactionContractTest(unittest.TestCase):
    @staticmethod
    def _events(rows):
        defaults = {
            "cluster_id": "411311",
            "report_date": "2026-08-01",
            "base_id": "TXN1",
            "transaction_id_type": "MAIN",
            "transaction_type": "CREDIT",
            "saldo_awal": 1000,
            "saldo_akhir": 1100,
            "nomor_rs": "",
            "load_id": "load-1",
            "source_row_number": 1,
        }
        return pd.DataFrame([{**defaults, **row} for row in rows])

    def _preview(self, rows, expected_dates=None):
        if expected_dates is not None:
            expected_dates = [
                f"2026-08-{day:02d}" for day in range(1, 32)
            ]
        return monthly.build_finpay_monthly_preview_from_dataframe(
            self._events(rows),
            cluster_id="411311",
            report_month="2026-08",
            calculation_cutoff="2026-09-01T00:00:00",
            expected_source_dates=expected_dates,
            loaded_source_dates=expected_dates,
            source_fingerprint="fingerprint-1",
        )

    def test_complete_exact_id_reversal_cancels_original_once(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-01 10:00:00"),
                "transaction_id": "TXN1",
                "raw_transaction_label": "RECHARGE",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 100,
                "debet": 0,
            },
            {
                "transaction_date": pd.Timestamp("2026-08-03 10:00:00"),
                "transaction_id": "TXN1",
                "raw_transaction_label": "REVERSAL",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 0,
                "debet": 100,
            },
        ], ["2026-08-01", "2026-08-03"])

        self.assertEqual(result["status"], monthly.MONTHLY_STATUS_READY)
        self.assertEqual(result["unresolved_reversal_count"], 0)
        self.assertEqual(result["category_rows"][0]["gross_amount"], 100.0)
        self.assertEqual(result["category_rows"][0]["reversed_amount"], 100.0)
        self.assertEqual(result["category_rows"][0]["net_payable"], 0.0)
        self.assertFalse(result["transaction_rows"][0]["active_at_cutoff"])

    def test_missing_original_blocks_month(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-03 10:00:00"),
                "transaction_id": "MISSING",
                "raw_transaction_label": "REVERSAL",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 0,
                "debet": 100,
            },
        ], ["2026-08-03"])

        self.assertEqual(result["status"], monthly.MONTHLY_STATUS_INCOMPLETE_REVERSALS)
        self.assertEqual(result["unresolved_reversal_count"], 1)
        self.assertFalse(result["release_ready"])

    def test_ambiguous_originals_block_month(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-01 10:00:00"),
                "transaction_id": "DUPLICATE",
                "raw_transaction_label": "RECHARGE",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 100,
                "debet": 0,
            },
            {
                "transaction_date": pd.Timestamp("2026-08-02 10:00:00"),
                "transaction_id": "DUPLICATE",
                "raw_transaction_label": "RECHARGE",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 100,
                "debet": 0,
            },
            {
                "transaction_date": pd.Timestamp("2026-08-03 10:00:00"),
                "transaction_id": "DUPLICATE",
                "raw_transaction_label": "REVERSAL",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 0,
                "debet": 100,
            },
        ], ["2026-08-01", "2026-08-02", "2026-08-03"])

        self.assertEqual(result["status"], monthly.MONTHLY_STATUS_INCOMPLETE_REVERSALS)
        self.assertEqual(result["unresolved_reversal_count"], 1)

    def test_missing_source_manifest_blocks_final_readiness(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-01 10:00:00"),
                "transaction_id": "TXN1",
                "raw_transaction_label": "RECHARGE",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 100,
                "debet": 0,
            },
        ])

        self.assertEqual(result["status"], monthly.MONTHLY_STATUS_INCOMPLETE_SOURCE)
        self.assertFalse(result["release_ready"])

    def test_qris_missing_disbursement_blocks_month(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-01 10:00:00"),
                "transaction_id": "QRIS1",
                "raw_transaction_label": "QRISDUWIT",
                "remarks": "Disburse Qris Duwit atas transaksi pembayaran QRIS",
                "kredit": 100,
                "debet": 0,
            },
        ], ["2026-08-01"])

        self.assertEqual(result["qris_missing_disbursement_count"], 1)
        self.assertEqual(result["status"], monthly.MONTHLY_STATUS_INCOMPLETE_REVERSALS)

    def test_publish_rejects_incomplete_preview_before_database_write(self):
        result = self._preview([
            {
                "transaction_date": pd.Timestamp("2026-08-01 10:00:00"),
                "transaction_id": "TXN1",
                "raw_transaction_label": "RECHARGE",
                "remarks": "Biaya Pembelian recharge sejumlah 100 rupiah, dari 081234 ke 411311",
                "kredit": 100,
                "debet": 0,
            },
        ])

        with self.assertRaisesRegex(ValueError, "not ready"):
            monthly.publish_finpay_monthly_snapshot(
                "unused-dsn",
                result,
                publication_id="publication-1",
                expected_source_fingerprint="fingerprint-1",
            )

    def test_ineligible_reversal_cannot_cancel_original(self):
        events = pd.DataFrame([
            {
                "event_id": "original",
                "cluster_id": "411311",
                "transaction_id": "TXN1",
                "event_role": "ORIGINAL",
                "event_timestamp": pd.Timestamp("2026-08-01 10:00:00"),
                "signed_amount": 100.0,
                "eligible": True,
            },
            {
                "event_id": "reversal",
                "cluster_id": "411311",
                "transaction_id": "TXN1",
                "event_role": "REVERSAL",
                "event_timestamp": pd.Timestamp("2026-08-02 10:00:00"),
                "signed_amount": -100.0,
                "eligible": False,
            },
        ])

        resolved = monthly._resolve_reversals(events)

        original = resolved.loc[resolved["event_id"] == "original"].iloc[0]
        reversal = resolved.loc[resolved["event_id"] == "reversal"].iloc[0]
        self.assertTrue(original["active_at_cutoff"])
        self.assertEqual(original["reversal_count"], 0)
        self.assertEqual(reversal["reversal_resolution_status"], "INELIGIBLE_REVERSAL")

    def test_multiple_reversals_block_original(self):
        events = pd.DataFrame([
            {
                "event_id": "original",
                "cluster_id": "411311",
                "transaction_id": "TXN1",
                "event_role": "ORIGINAL",
                "event_timestamp": pd.Timestamp("2026-08-01 10:00:00"),
                "signed_amount": 100.0,
                "eligible": True,
            },
            {
                "event_id": "reversal-1",
                "cluster_id": "411311",
                "transaction_id": "TXN1",
                "event_role": "REVERSAL",
                "event_timestamp": pd.Timestamp("2026-08-02 10:00:00"),
                "signed_amount": -100.0,
                "eligible": True,
            },
            {
                "event_id": "reversal-2",
                "cluster_id": "411311",
                "transaction_id": "TXN1",
                "event_role": "REVERSAL",
                "event_timestamp": pd.Timestamp("2026-08-03 10:00:00"),
                "signed_amount": -100.0,
                "eligible": True,
            },
        ])

        resolved = monthly._resolve_reversals(events)

        original = resolved.loc[resolved["event_id"] == "original"].iloc[0]
        self.assertEqual(original["reversal_resolution_status"], "MULTIPLE_REVERSALS")
        self.assertTrue(original["is_unusual"])
        self.assertEqual(original["reversal_count"], 2)


class FinPayGoogleSheetHardeningContractTest(unittest.TestCase):
    def test_gspread_client_runs_preflight_and_sets_timeout(self):
        credentials = object()
        client = Mock()
        client.http_client.set_timeout = Mock()
        with patch(
            "finpay_pipeline.sheets_common.google_api_preflight",
            return_value={"status": 200},
        ) as preflight, patch(
            "finpay_pipeline.sheets_common.Credentials.from_service_account_file",
            return_value=credentials,
        ), patch(
            "finpay_pipeline.sheets_common._GoogleAuthorizedSession",
            return_value=Mock(),
        ), patch(
            "finpay_pipeline.sheets_common.gspread.Client",
            return_value=client,
        ):
            result = sheets_common.make_gspread_client("/tmp/service-account.json")

        preflight.assert_called_once_with()
        client.http_client.set_timeout.assert_called_once()
        self.assertIs(result, client)

    def test_monthly_sheet_uses_monitoring_sections_and_protection(self):
        spreadsheet = Mock()
        worksheet = Mock()
        worksheet.id = 123
        worksheet.row_count = 1000
        spreadsheet.worksheet.return_value = worksheet
        spreadsheet.fetch_sheet_metadata.return_value = {
            "sheets": [{
                "properties": {"sheetId": worksheet.id},
                "merges": [],
                "protectedRanges": [],
                "bandedRanges": [],
            }]
        }
        with patch(
            "finpay_pipeline.monthly.open_or_create_finpay_spreadsheet",
            return_value=spreadsheet,
        ):
            result = monthly.write_finpay_monthly_to_gsheet(
                Mock(),
                spreadsheet_title="FINPAY REPORT",
                worksheet_title="FINPAY MONTHLY - 411311",
                result={
                "cluster_id": "411311",
                "report_month": "2026-08-01",
                "status": "FINAL",
                "published": True,
                "calculation_cutoff": "2026-09-18T00:00:00",
                "publication_id": "pub-1",
                "source_fingerprint": "fingerprint",
                "release_ready": True,
                "source_day_count": 31,
                "missing_source_day_count": 0,
                "unresolved_reversal_count": 0,
                "blocking_exception_count": 0,
                "unresolved_exposure": 0,
                "qris_missing_disbursement_count": 0,
                "missing_source_dates": [],
                "category_rows": [{
                    "settlement_category": "NGRS_PRINCIPAL",
                    "gross_transaction_count": 1,
                    "reversed_transaction_count": 0,
                    "active_transaction_count": 1,
                    "gross_amount": 100,
                    "reversed_amount": 0,
                    "net_payable": 100,
                    "calculation_status": "FINAL",
                }],
                },
            )

        self.assertEqual(result["category_rows"], 1)
        worksheet.clear.assert_called_once()
        worksheet.update.assert_called_once()
        requests = spreadsheet.batch_update.call_args.args[0]["requests"]
        self.assertTrue(any("mergeCells" in request for request in requests))
        self.assertTrue(any("repeatCell" in request for request in requests))
        self.assertTrue(any("addProtectedRange" in request for request in requests))


if __name__ == "__main__":
    unittest.main()
