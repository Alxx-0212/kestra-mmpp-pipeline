import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkaja_fee_pipeline.processing import (
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    LINKAJA_SOURCE_COLUMNS,
    RAW_NORMALIZED_COLUMNS,
    REPORT_DATE_HEADER,
    TOTAL_FEE_HEADER,
    aggregate_linkaja_fee_file,
    normalize_linkaja_csv,
)
from linkaja_fee_pipeline.database import (
    DETAIL_HEADERS,
    DETAIL_REPORT_DATE_HEADER,
    MEASURE_COMPANY_CREDIT_HEADER,
    MEASURE_COMPANY_DEBIT_HEADER,
    MEASURE_EXPECTED_FEE_HEADER,
    MEASURE_IN_CLUSTER_FEE_HEADER,
    MEASURE_REVERSAL_COUNT_HEADER,
    MEASURE_REVERSAL_FEE_HEADER,
    MEASURE_SCENARIO_HEADER,
    MEASURE_SOURCE_FEE_HEADER,
    MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER,
    MEASURE_UNRESOLVED_COUNT_HEADER,
)
from linkaja_fee_pipeline.sheets import matching_date_cluster_rows
from linkaja_fee_pipeline.sheets import (
    CLUSTER_START_COLUMNS,
    COMPLETE_IN_CLUSTER_REVERSAL_LABEL,
    COMPLETE_REVERSAL_PREFIX,
    CASH_SECTION_LABEL,
    DATA_START_ROW,
    DETAIL_COLUMN_WIDTHS,
    DETAIL_SECTION_LABEL,
    EXPECTED_FEE_COLUMN_WIDTH,
    EXPECTED_FEE_LEDGER_LABEL,
    IN_CLUSTER_FEE_COLUMN_WIDTH,
    IN_CLUSTER_FEE_LEDGER_LABEL,
    KNOWN_CLUSTER_ORDER,
    LEGACY_DETAIL_HEADERS,
    MANDIRI_SECTION_LABEL,
    OUT_CLUSTER_SCENARIO,
    POSTED_FEE_SCENARIO,
    PREVIOUS_DETAIL_HEADERS,
    PPOB_SCENARIO,
    REVERSAL_COUNT_LEDGER_LABEL,
    REVERSAL_FEE_LEDGER_LABEL,
    SELISIH_LABEL,
    STATIC_SHEET_COL_COUNT,
    TOTAL_EXPECTED_MANDIRI_LABEL,
    UNRESOLVED_COUNT_LEDGER_LABEL,
    UNRESOLVED_REVERSAL_LABEL,
    WITHDRAWAL_SCENARIO,
    _add_protected_sheet_request,
    _build_static_header_values,
    _build_side_by_side_values,
    _cluster_format_requests,
    _cluster_id_from_title,
    _delete_all_protected_range_requests,
    _extract_cluster_tables,
    _format_linkaja_detail_sheet,
    _merge_detail_summary_values,
    _merge_replacement_rows,
    _protection_editor_emails,
    _write_cluster_data_blocks,
    detail_worksheet_for_cluster,
    setup_linkaja_fee_headers,
)


class LinkAjaFeePipelineTest(unittest.TestCase):
    class FakeAuth:
        service_account_email = "service-account@example.com"

    class FakeClient:
        def __init__(self):
            self.auth = LinkAjaFeePipelineTest.FakeAuth()

    class FakeWorksheet:
        id = 123
        row_count = 1000
        col_count = STATIC_SHEET_COL_COUNT

        def __init__(self, values=None):
            self.values = values or []
            self.updates = []

        def get_all_values(self):
            return self.values

        def update(self, values, range_name=None, value_input_option=None):
            self.updates.append((range_name, values, value_input_option))

    class FakeSpreadsheet:
        def __init__(self):
            self.batch_updates = []

        def batch_update(self, payload):
            self.batch_updates.append(payload)

        def fetch_sheet_metadata(self, _params):
            return {"sheets": [{"properties": {"sheetId": 123}, "protectedRanges": []}]}

    @staticmethod
    def full_source_row(**overrides):
        row = {column: "" for column in LINKAJA_SOURCE_COLUMNS}
        row.update({
            "No": "1",
            "Top Organization": "123456-TEST COMPANY",
            "Parent Organization": "123456-TEST COMPANY",
            "Organization": "123456-TEST COMPANY",
            "Transaction ID": "TX-1",
            "Finalized Date": "01/07/2026",
            "Finalized Time": "12:34:56",
            "Initiate Date": "01/07/2026",
            "Initiate Time": "12:30:00",
            "Transaction Type": "Transfer",
            "Transaction Scenario": "Organization Withdraw of Funds with Next Working Day Payment",
            "Transaction Status": "Completed",
            "Account": "123 - Organization MFS Purchase Account",
            "Debit": "1000",
            "Credit": "0",
            "Balance": "50",
        })
        row.update(overrides)
        return row

    def test_normalizes_full_csv_for_database_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "laporan-123456-TEST-1.csv"
            output = Path(temp_dir) / "normalized.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=LINKAJA_SOURCE_COLUMNS)
                writer.writeheader()
                writer.writerows([
                    self.full_source_row(),
                    self.full_source_row(
                        **{
                            "No": "2",
                            "Organization": "654321-AGENT",
                            "Transaction ID": "TX-2",
                            "Transaction Scenario": "Digipos B2B Transfer In Cluster",
                            "Debit": "2000",
                            "Fee": "20",
                        }
                    ),
                ])

            manifest = normalize_linkaja_csv(
                source,
                output,
                filename_hint=source.name,
                load_id="load-1",
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(manifest.cluster_id, "123456")
        self.assertEqual(manifest.source_rows, 2)
        self.assertEqual(manifest.report_dates, ["2026-07-01"])
        self.assertEqual(list(rows[0]), RAW_NORMALIZED_COLUMNS)
        self.assertEqual(rows[0]["finalized_date"], "2026-07-01")
        self.assertEqual(rows[0]["finalized_time"], "12:34:56")
        self.assertEqual(rows[0]["debit"], "1000")
        self.assertEqual(rows[0]["fee"], "")
        self.assertEqual(rows[1]["fee"], "20")

    def test_normalizer_rejects_non_csv_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source_file"
            source.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported LinkAja input"):
                normalize_linkaja_csv(
                    source,
                    Path(temp_dir) / "normalized.csv",
                    filename_hint="laporan-123456-TEST.xlsx",
                    load_id="load-1",
                )

    def test_normalizer_requires_exact_source_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "laporan-123456-TEST.csv"
            source.write_text("Finalized Date,Transaction ID\n01/07/2026,TX-1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid LinkAja CSV header"):
                normalize_linkaja_csv(
                    source,
                    Path(temp_dir) / "normalized.csv",
                    filename_hint=source.name,
                    load_id="load-1",
                )

    def test_replaces_complete_detail_dates_and_preserves_other_dates(self):
        existing = [
            DETAIL_HEADERS,
            ["01/07/2026", DETAIL_SECTION_LABEL, "OLD SCENARIO", "", "", 10],
            [
                "01/07/2026",
                MANDIRI_SECTION_LABEL,
                TOTAL_EXPECTED_MANDIRI_LABEL,
                10,
                "",
                "",
            ],
            ["02/07/2026", DETAIL_SECTION_LABEL, "KEEP SCENARIO", "", "", 30],
            [
                "02/07/2026",
                MANDIRI_SECTION_LABEL,
                TOTAL_EXPECTED_MANDIRI_LABEL,
                30,
                "",
                "",
            ],
        ]
        new_rows = [{
            DETAIL_REPORT_DATE_HEADER: "01/07/2026",
            MEASURE_SCENARIO_HEADER: WITHDRAWAL_SCENARIO,
            MEASURE_COMPANY_DEBIT_HEADER: 1000,
            MEASURE_COMPANY_CREDIT_HEADER: 0,
            MEASURE_SOURCE_FEE_HEADER: 0,
            MEASURE_EXPECTED_FEE_HEADER: 0,
            MEASURE_REVERSAL_COUNT_HEADER: 0,
            MEASURE_REVERSAL_FEE_HEADER: 0,
        }]

        values, replaced = _merge_detail_summary_values(
            existing,
            new_rows,
            ["01/07/2026"],
        )

        self.assertEqual(replaced, 2)
        self.assertEqual(values[0], DETAIL_HEADERS)
        self.assertNotIn("OLD SCENARIO", [row[2] for row in values])
        self.assertIn("KEEP SCENARIO", [row[2] for row in values])
        withdrawal_row = next(row for row in values if row[2] == WITHDRAWAL_SCENARIO)
        self.assertEqual(withdrawal_row[3:], [1000, "", ""])
        self.assertEqual(len(withdrawal_row), 6)

    def test_builds_fee_count_and_mandiri_rows_in_six_column_ledger(self):
        new_rows = [
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: "Digipos B2B Transfer",
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 1000,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 200,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            },
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: "Digipos B2B Transfer In Cluster",
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 500,
                MEASURE_SOURCE_FEE_HEADER: 20,
                MEASURE_IN_CLUSTER_FEE_HEADER: 20,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            },
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: PPOB_SCENARIO,
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 0,
                MEASURE_SOURCE_FEE_HEADER: 2400,
                MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            },
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: POSTED_FEE_SCENARIO,
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 200,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            },
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: "Buy Goods Reversal for General Merchant",
                MEASURE_COMPANY_DEBIT_HEADER: 100,
                MEASURE_COMPANY_CREDIT_HEADER: 0,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 2,
                MEASURE_REVERSAL_FEE_HEADER: 40,
            },
            {
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: WITHDRAWAL_SCENARIO,
                MEASURE_COMPANY_DEBIT_HEADER: 1200,
                MEASURE_COMPANY_CREDIT_HEADER: 0,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            },
        ]

        values, _replaced = _merge_detail_summary_values(
            [],
            new_rows,
            ["01/07/2026"],
        )

        self.assertTrue(all(len(row) == 6 for row in values))
        first_date_row = next(
            row for row in values[1:] if row[0] == "01/07/2026"
        )
        self.assertEqual(
            first_date_row[:3],
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                WITHDRAWAL_SCENARIO,
            ],
        )
        self.assertEqual(first_date_row[3:], [1200, "", ""])
        out_cluster_row = next(
            row for row in values if row[2] == OUT_CLUSTER_SCENARIO
        )
        self.assertEqual(out_cluster_row[3:], [1000, 0, ""])
        posted_fee_rows = [row for row in values if row[2] == POSTED_FEE_SCENARIO]
        self.assertEqual(len(posted_fee_rows), 1)
        self.assertEqual(posted_fee_rows[0][3:], [200, "", ""])
        ppob_row = next(row for row in values if row[2] == PPOB_SCENARIO)
        self.assertEqual(ppob_row[3:], [2400, "", ""])
        expected_rows = [row for row in values if row[2] == EXPECTED_FEE_LEDGER_LABEL]
        self.assertEqual(expected_rows[0][3:], ["", "", 200])
        self.assertEqual(expected_rows[0][1], DETAIL_SECTION_LABEL)
        self.assertFalse(any(
            row[1] == MANDIRI_SECTION_LABEL
            and row[2] in {
                EXPECTED_FEE_LEDGER_LABEL,
                POSTED_FEE_SCENARIO,
                PPOB_SCENARIO,
            }
            for row in values
        ))
        in_cluster_fee_rows = [
            row for row in values if row[2] == IN_CLUSTER_FEE_LEDGER_LABEL
        ]
        self.assertEqual(in_cluster_fee_rows[0][3:], ["", 20, ""])
        reversal_count_row = next(
            row for row in values
            if row[2] == REVERSAL_COUNT_LEDGER_LABEL
        )
        self.assertEqual(reversal_count_row[5], 2)
        reversal_fee_rows = [
            row for row in values if row[2] == REVERSAL_FEE_LEDGER_LABEL
        ]
        self.assertEqual(reversal_fee_rows[0][3:], [40, "", ""])
        self.assertEqual(
            [row[2] for row in values].count(TOTAL_EXPECTED_MANDIRI_LABEL),
            1,
        )
        mandiri_value_row = next(
            row for row in values
            if row[1] == CASH_SECTION_LABEL
            and row[2] == MANDIRI_SECTION_LABEL
        )
        self.assertIn(WITHDRAWAL_SCENARIO.upper(), mandiri_value_row[3])
        self.assertIn('INDIRECT("D"&ROW()+2&":D")', mandiri_value_row[3])
        self.assertFalse(any(row[2] == "RUNNING TOTAL" for row in values))
        selisih_row = next(row for row in values if row[2] == SELISIH_LABEL)
        self.assertEqual(selisih_row[1], CASH_SECTION_LABEL)
        total_row_number = values.index(next(
            row for row in values if row[2] == TOTAL_EXPECTED_MANDIRI_LABEL
        )) + 1
        self.assertIn(f"-D{total_row_number}", selisih_row[3])
        self.assertIn("PENDING WITHDRAWAL", selisih_row[5])

    def test_inserts_blank_withdrawal_row_when_a_date_has_no_withdrawal(self):
        values, _replaced = _merge_detail_summary_values(
            [],
            [{
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: OUT_CLUSTER_SCENARIO,
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 1000,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 200,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            }],
            ["01/07/2026"],
        )

        first_date_row = next(
            row for row in values[1:] if row[0] == "01/07/2026"
        )
        self.assertEqual(
            first_date_row,
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                WITHDRAWAL_SCENARIO,
                "",
                "",
                "",
            ],
        )

    def test_unresolved_reversal_is_separate_and_overrides_payment_status(self):
        values, _replaced = _merge_detail_summary_values(
            [],
            [
                {
                    DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                    MEASURE_SCENARIO_HEADER: UNRESOLVED_REVERSAL_LABEL,
                    MEASURE_COMPANY_DEBIT_HEADER: 100,
                    MEASURE_COMPANY_CREDIT_HEADER: 0,
                    MEASURE_SOURCE_FEE_HEADER: 0,
                    MEASURE_EXPECTED_FEE_HEADER: 0,
                    MEASURE_REVERSAL_COUNT_HEADER: 0,
                    MEASURE_REVERSAL_FEE_HEADER: 0,
                    MEASURE_UNRESOLVED_COUNT_HEADER: 1,
                }
            ],
            ["01/07/2026"],
        )

        reversal_row = next(
            row for row in values
            if row[1] == DETAIL_SECTION_LABEL
            and row[2] == UNRESOLVED_REVERSAL_LABEL
        )
        self.assertEqual(reversal_row[3:], [0, 100, ""])
        count_row = next(
            row for row in values
            if row[1] == DETAIL_SECTION_LABEL
            and row[2] == UNRESOLVED_COUNT_LEDGER_LABEL
        )
        self.assertEqual(count_row[5], 1)
        self.assertTrue(any(
            row[1] == MANDIRI_SECTION_LABEL
            and row[2] == UNRESOLVED_REVERSAL_LABEL
            for row in values
        ))
        status_formula = next(
            row[5] for row in values if row[2] == SELISIH_LABEL
        )
        self.assertIn("UNUSUAL - UNRESOLVED REVERSAL", status_formula)
        self.assertNotIn("IF(=F", status_formula)

    def test_complete_fee_reversal_is_detail_only(self):
        complete_posted_fee_label = f"{COMPLETE_REVERSAL_PREFIX}{POSTED_FEE_SCENARIO}"
        values, _replaced = _merge_detail_summary_values(
            [],
            [
                {
                    DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                    MEASURE_SCENARIO_HEADER: COMPLETE_IN_CLUSTER_REVERSAL_LABEL,
                    MEASURE_COMPANY_DEBIT_HEADER: 1000,
                    MEASURE_COMPANY_CREDIT_HEADER: 0,
                    MEASURE_SOURCE_FEE_HEADER: 0,
                    MEASURE_EXPECTED_FEE_HEADER: 0,
                    MEASURE_REVERSAL_COUNT_HEADER: 1,
                    MEASURE_REVERSAL_FEE_HEADER: 20,
                },
                {
                    DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                    MEASURE_SCENARIO_HEADER: complete_posted_fee_label,
                    MEASURE_COMPANY_DEBIT_HEADER: 200,
                    MEASURE_COMPANY_CREDIT_HEADER: 0,
                    MEASURE_SOURCE_FEE_HEADER: 0,
                    MEASURE_EXPECTED_FEE_HEADER: 0,
                    MEASURE_REVERSAL_COUNT_HEADER: 0,
                    MEASURE_REVERSAL_FEE_HEADER: 0,
                },
            ],
            ["01/07/2026"],
        )

        posted_fee_reversal = next(
            row for row in values
            if row[1] == DETAIL_SECTION_LABEL
            and row[2] == complete_posted_fee_label
        )
        self.assertEqual(posted_fee_reversal[3:], [0, 200, ""])
        self.assertTrue(any(
            row[1] == MANDIRI_SECTION_LABEL
            and row[2] == COMPLETE_IN_CLUSTER_REVERSAL_LABEL
            for row in values
        ))
        self.assertFalse(any(
            row[1] == MANDIRI_SECTION_LABEL
            and row[2] == complete_posted_fee_label
            for row in values
        ))

    def test_ppob_missing_fee_does_not_silently_become_zero(self):
        values, _replaced = _merge_detail_summary_values(
            [],
            [{
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: PPOB_SCENARIO,
                MEASURE_COMPANY_DEBIT_HEADER: 0,
                MEASURE_COMPANY_CREDIT_HEADER: 0,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_SOURCE_FEE_MISSING_COUNT_HEADER: 1,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            }],
            ["01/07/2026"],
        )

        ppob_row = next(row for row in values if row[2] == PPOB_SCENARIO)
        self.assertEqual(ppob_row[3:], ["", "", ""])

    def test_unknown_scenario_is_monitoring_only_in_other(self):
        values, _replaced = _merge_detail_summary_values(
            [],
            [{
                DETAIL_REPORT_DATE_HEADER: "01/07/2026",
                MEASURE_SCENARIO_HEADER: "Future Scenario",
                MEASURE_COMPANY_DEBIT_HEADER: 10,
                MEASURE_COMPANY_CREDIT_HEADER: 20,
                MEASURE_SOURCE_FEE_HEADER: 0,
                MEASURE_EXPECTED_FEE_HEADER: 0,
                MEASURE_REVERSAL_COUNT_HEADER: 0,
                MEASURE_REVERSAL_FEE_HEADER: 0,
            }],
            ["01/07/2026"],
        )

        future_row = next(row for row in values if row[2] == "Future Scenario")
        self.assertEqual(future_row[3:], ["", "", 30])

    def test_rerun_replaces_one_complete_ledger_block_without_duplicates(self):
        first_measure = {
            DETAIL_REPORT_DATE_HEADER: "01/07/2026",
            MEASURE_SCENARIO_HEADER: "Digipos B2B Transfer",
            MEASURE_COMPANY_DEBIT_HEADER: 0,
            MEASURE_COMPANY_CREDIT_HEADER: 1000,
            MEASURE_SOURCE_FEE_HEADER: 0,
            MEASURE_EXPECTED_FEE_HEADER: 200,
            MEASURE_REVERSAL_COUNT_HEADER: 0,
            MEASURE_REVERSAL_FEE_HEADER: 0,
        }
        first_values, _replaced = _merge_detail_summary_values(
            [],
            [first_measure],
            ["01/07/2026"],
        )
        replacement_measure = {
            **first_measure,
            MEASURE_COMPANY_CREDIT_HEADER: 1500,
            MEASURE_EXPECTED_FEE_HEADER: 400,
        }

        replacement_values, replaced = _merge_detail_summary_values(
            first_values,
            [replacement_measure],
            ["01/07/2026"],
        )

        self.assertEqual(replaced, len(first_values) - 1)
        self.assertTrue(any(
            row[1] == DETAIL_SECTION_LABEL
            for row in replacement_values[1:]
        ))
        self.assertEqual(
            [row[2] for row in replacement_values].count(
                TOTAL_EXPECTED_MANDIRI_LABEL
            ),
            1,
        )
        detail_transfer = next(
            row
            for row in replacement_values
            if row[2] == "Digipos B2B Transfer" and row[3] == 1500
        )
        self.assertEqual(detail_transfer[4:], [0, ""])

    def test_migrates_previous_five_column_detail_layout(self):
        previous = [
            PREVIOUS_DETAIL_HEADERS,
            ["01/07/2026", DETAIL_SECTION_LABEL, "", "", ""],
            ["01/07/2026", "Digipos B2B Transfer", 1000, 0, ""],
            ["01/07/2026", MANDIRI_SECTION_LABEL, "", "", ""],
        ]

        values, replaced = _merge_detail_summary_values(previous, [], [])

        self.assertEqual(replaced, 0)
        self.assertEqual(values[0], DETAIL_HEADERS)
        transfer_row = next(
            row for row in values if row[2] == "Digipos B2B Transfer"
        )
        self.assertEqual(
            transfer_row,
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                "Digipos B2B Transfer",
                1000,
                0,
                "",
            ],
        )

    def test_clears_existing_other_for_recognized_detail_movement(self):
        existing = [
            DETAIL_HEADERS,
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                OUT_CLUSTER_SCENARIO,
                1000,
                0,
                1000,
            ],
        ]

        values, replaced = _merge_detail_summary_values(existing, [], [])

        self.assertEqual(replaced, 0)
        transfer_row = next(
            row for row in values if row[2] == OUT_CLUSTER_SCENARIO
        )
        self.assertEqual(transfer_row[3:], [1000, 0, ""])

    def test_migrates_expected_fee_to_other_only(self):
        existing = [
            DETAIL_HEADERS,
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                EXPECTED_FEE_LEDGER_LABEL,
                "",
                200,
                200,
            ],
        ]

        values, replaced = _merge_detail_summary_values(existing, [], [])

        self.assertEqual(replaced, 0)
        expected_row = next(
            row for row in values if row[2] == EXPECTED_FEE_LEDGER_LABEL
        )
        self.assertEqual(expected_row[3:], ["", "", 200])

    def test_migrates_legacy_nine_column_detail_layout(self):
        legacy = [
            LEGACY_DETAIL_HEADERS,
            [
                "01/07/2026",
                "Digipos B2B Transfer",
                1,
                0,
                1000,
                0,
                200,
                0,
                0,
            ],
        ]

        values, replaced = _merge_detail_summary_values(legacy, [], [])

        self.assertEqual(replaced, 0)
        self.assertEqual(values[0], DETAIL_HEADERS)
        transfer_row = next(
            row for row in values if row[2] == "Digipos B2B Transfer"
        )
        self.assertEqual(transfer_row[3:], [1000, 0, ""])
        self.assertTrue(any(
            row[1] == MANDIRI_SECTION_LABEL
            for row in values
        ))

    def test_detail_formatter_only_targets_the_six_used_columns(self):
        worksheet = self.FakeWorksheet()
        spreadsheet = self.FakeSpreadsheet()
        values = [
            DETAIL_HEADERS,
            [
                "01/07/2026",
                DETAIL_SECTION_LABEL,
                "Digipos B2B Transfer",
                1000,
                0,
                "",
            ],
            [
                "01/07/2026",
                MANDIRI_SECTION_LABEL,
                TOTAL_EXPECTED_MANDIRI_LABEL,
                1000,
                "",
                "",
            ],
            [
                "01/07/2026",
                CASH_SECTION_LABEL,
                MANDIRI_SECTION_LABEL,
                1000,
                "",
                "",
            ],
            [
                "01/07/2026",
                CASH_SECTION_LABEL,
                SELISIH_LABEL,
                0,
                "",
                "SESUAI",
            ],
        ]

        _format_linkaja_detail_sheet(
            self.FakeClient(),
            spreadsheet,
            worksheet,
            "421306",
            values,
        )

        requests = spreadsheet.batch_updates[-1]["requests"]
        cash_background_requests = [
            request["repeatCell"]
            for request in requests
            if "repeatCell" in request
            and request["repeatCell"].get("range", {}).get("startRowIndex") == 3
            and request["repeatCell"].get("range", {}).get("endRowIndex") == 5
            and request["repeatCell"].get("range", {}).get("startColumnIndex") == 0
            and request["repeatCell"].get("range", {}).get("endColumnIndex")
            == len(DETAIL_HEADERS)
        ]
        self.assertTrue(any(
            request.get("cell", {})
            .get("userEnteredFormat", {})
            .get("backgroundColor")
            == {"red": 0.965, "green": 0.930, "blue": 0.990}
            for request in cash_background_requests
        ))
        column_widths = {
            request["updateDimensionProperties"]["range"]["startIndex"]:
            request["updateDimensionProperties"]["properties"]["pixelSize"]
            for request in requests
            if "updateDimensionProperties" in request
        }
        self.assertEqual(column_widths[2], DETAIL_COLUMN_WIDTHS[2])
        self.assertEqual(column_widths[5], DETAIL_COLUMN_WIDTHS[5])
        self.assertEqual(DETAIL_COLUMN_WIDTHS[2], 500)
        self.assertEqual(DETAIL_COLUMN_WIDTHS[5], 280)
        for request in requests:
            operation = request.get("repeatCell") or request.get("updateBorders")
            if operation and "range" in operation:
                self.assertLessEqual(
                    operation["range"].get("endColumnIndex", len(DETAIL_HEADERS)),
                    len(DETAIL_HEADERS),
                )
            dimension = request.get("updateDimensionProperties", {}).get("range")
            if dimension and dimension.get("dimension") == "COLUMNS":
                self.assertLessEqual(
                    dimension["endIndex"],
                    len(DETAIL_HEADERS),
                )

    def test_uses_stable_cluster_detail_worksheet_names(self):
        self.assertEqual(detail_worksheet_for_cluster("411311"), "PKY - LinkAja Detail")

    def test_aggregates_expected_and_in_cluster_fees_by_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "laporan-123456-TEST-1.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "Finalized Date",
                        "Transaction Scenario",
                        "Debit",
                        "Credit",
                        "Fee",
                    ],
                )
                writer.writeheader()
                writer.writerows([
                    {
                        "Finalized Date": "01/06/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "1000",
                        "Fee": "",
                    },
                    {
                        "Finalized Date": "01/06/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "0",
                        "Fee": "",
                    },
                    {
                        "Finalized Date": "01/06/2026",
                        "Transaction Scenario": "Digipos B2B Transfer In Cluster",
                        "Debit": "1000",
                        "Credit": "0",
                        "Fee": "20",
                    },
                    {
                        "Finalized Date": "02/06/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "1000",
                        "Fee": "",
                    },
                    {
                        "Finalized Date": "02/06/2026",
                        "Transaction Scenario": "Digipos B2B Transfer In Cluster",
                        "Debit": "1000",
                        "Credit": "0",
                        "Fee": "40",
                    },
                ])

            summary = aggregate_linkaja_fee_file(path)

        self.assertEqual(summary.cluster_id, "123456")
        self.assertEqual(summary.report_dates, ["01/06/2026", "02/06/2026"])
        self.assertEqual(len(summary.rows), 2)
        self.assertEqual(summary.rows[0][EXPECTED_FEE_HEADER], 200)
        self.assertEqual(summary.rows[0][IN_CLUSTER_FEE_HEADER], 20)
        self.assertEqual(summary.rows[0][TOTAL_FEE_HEADER], 220)
        self.assertEqual(summary.rows[1][EXPECTED_FEE_HEADER], 200)
        self.assertEqual(summary.rows[1][IN_CLUSTER_FEE_HEADER], 40)
        self.assertEqual(summary.rows[1][TOTAL_FEE_HEADER], 240)

    def test_initializes_all_static_headers_when_sheet_is_empty(self):
        spreadsheet = self.FakeSpreadsheet()
        worksheet = self.FakeWorksheet()

        initialized = setup_linkaja_fee_headers(
            self.FakeClient(),
            spreadsheet,
            worksheet,
            existing_values=[],
        )

        self.assertTrue(initialized)
        self.assertEqual(worksheet.updates[0][0], "A1:AC2")
        self.assertEqual(worksheet.updates[0][2], "USER_ENTERED")
        values = worksheet.updates[0][1]
        self.assertEqual(values, _build_static_header_values())
        for cluster_id in KNOWN_CLUSTER_ORDER:
            start = CLUSTER_START_COLUMNS[cluster_id]
            self.assertTrue(values[0][start])
            self.assertEqual(
                values[1][start:start + 4],
                [
                    REPORT_DATE_HEADER,
                    EXPECTED_FEE_HEADER,
                    IN_CLUSTER_FEE_HEADER,
                    TOTAL_FEE_HEADER,
                ],
            )

    def test_does_not_reinitialize_when_sheet_has_values(self):
        spreadsheet = self.FakeSpreadsheet()
        worksheet = self.FakeWorksheet(values=[["MRT - 421306"]])

        initialized = setup_linkaja_fee_headers(
            self.FakeClient(),
            spreadsheet,
            worksheet,
            existing_values=worksheet.values,
        )

        self.assertFalse(initialized)
        self.assertEqual(worksheet.updates, [])

    def test_writes_data_only_to_changed_static_cluster_columns(self):
        worksheet = self.FakeWorksheet()
        tables, _replaced_rows = _merge_replacement_rows(
            {},
            [
                {
                    REPORT_DATE_HEADER: "01/06/2026",
                    CLUSTER_ID_HEADER: "421307",
                    EXPECTED_FEE_HEADER: 200,
                    IN_CLUSTER_FEE_HEADER: 20,
                    TOTAL_FEE_HEADER: 220,
                }
            ],
        )

        _write_cluster_data_blocks(
            worksheet,
            tables,
            ["421307"],
            DATA_START_ROW,
        )

        self.assertEqual(len(worksheet.updates), 1)
        self.assertEqual(worksheet.updates[0][0], "F3:I3")
        self.assertEqual(worksheet.updates[0][1], [["01/06/2026", 200, 20, 220]])

    def test_matches_existing_rows_by_report_date_and_cluster_id(self):
        headers = [
            REPORT_DATE_HEADER,
            CLUSTER_ID_HEADER,
            EXPECTED_FEE_HEADER,
            IN_CLUSTER_FEE_HEADER,
            TOTAL_FEE_HEADER,
        ]
        existing_rows = [
            headers,
            ["01/06/2026", "123456", "200", "20", "220"],
            ["01/06/2026", "999999", "200", "20", "220"],
            ["02/06/2026", "123456", "200", "40", "240"],
        ]
        new_rows = [
            {REPORT_DATE_HEADER: "01/06/2026", CLUSTER_ID_HEADER: "123456"},
            {REPORT_DATE_HEADER: "02/06/2026", CLUSTER_ID_HEADER: "123456"},
        ]

        self.assertEqual(
            matching_date_cluster_rows(existing_rows, headers, new_rows),
            [2, 4],
        )

    def test_builds_side_by_side_cluster_tables_without_source_columns(self):
        rows = [
            {
                REPORT_DATE_HEADER: "01/06/2026",
                CLUSTER_ID_HEADER: "421307",
                EXPECTED_FEE_HEADER: 200,
                IN_CLUSTER_FEE_HEADER: 20,
                TOTAL_FEE_HEADER: 220,
                "SOURCE FILE": "ignored.csv",
                "SOURCE ROWS": 10,
            },
            {
                REPORT_DATE_HEADER: "01/06/2026",
                CLUSTER_ID_HEADER: "411311",
                EXPECTED_FEE_HEADER: 400,
                IN_CLUSTER_FEE_HEADER: 40,
                TOTAL_FEE_HEADER: 440,
                "SOURCE FILE": "ignored.csv",
                "SOURCE ROWS": 20,
            },
        ]

        tables, replaced_rows = _merge_replacement_rows({}, rows)
        values, clusters = _build_side_by_side_values(tables)

        self.assertEqual(replaced_rows, 0)
        self.assertEqual(clusters, KNOWN_CLUSTER_ORDER)
        self.assertEqual(values[0][CLUSTER_START_COLUMNS["421306"]], "MRT - 421306")
        self.assertEqual(values[0][CLUSTER_START_COLUMNS["421307"]], "TDR - 421307")
        self.assertEqual(values[0][CLUSTER_START_COLUMNS["411311"]], "PKY - 411311")
        self.assertEqual(
            values[1][CLUSTER_START_COLUMNS["421307"]:CLUSTER_START_COLUMNS["421307"] + 4],
            [
                REPORT_DATE_HEADER,
                EXPECTED_FEE_HEADER,
                IN_CLUSTER_FEE_HEADER,
                TOTAL_FEE_HEADER,
            ],
        )
        self.assertNotIn("SOURCE FILE", values[1])
        self.assertNotIn("SOURCE ROWS", values[1])
        self.assertEqual(
            values[2][CLUSTER_START_COLUMNS["421307"]:CLUSTER_START_COLUMNS["421307"] + 4],
            ["01/06/2026", 200, 20, 220],
        )
        self.assertEqual(
            values[2][CLUSTER_START_COLUMNS["411311"]:CLUSTER_START_COLUMNS["411311"] + 4],
            ["01/06/2026", 400, 40, 440],
        )

    def test_replaces_only_matching_dates_inside_cluster_table(self):
        col_count = len(KNOWN_CLUSTER_ORDER) * 5 - 1
        existing_values = [["" for _ in range(col_count)] for _ in range(4)]
        for cluster_id in KNOWN_CLUSTER_ORDER:
            start = CLUSTER_START_COLUMNS[cluster_id]
            existing_values[0][start] = f"CLUSTER {cluster_id}"
            existing_values[1][start:start + 4] = [
                REPORT_DATE_HEADER,
                EXPECTED_FEE_HEADER,
                IN_CLUSTER_FEE_HEADER,
                TOTAL_FEE_HEADER,
            ]
        tdr_start = CLUSTER_START_COLUMNS["421307"]
        pky_start = CLUSTER_START_COLUMNS["411311"]
        existing_values[2][tdr_start:tdr_start + 4] = ["01/06/2026", "200", "20", "220"]
        existing_values[3][tdr_start:tdr_start + 4] = ["02/06/2026", "300", "30", "330"]
        existing_values[2][pky_start:pky_start + 4] = ["01/06/2026", "400", "40", "440"]

        tables = _extract_cluster_tables(existing_values)
        new_rows = [
            {
                REPORT_DATE_HEADER: "01/06/2026",
                CLUSTER_ID_HEADER: "421307",
                EXPECTED_FEE_HEADER: 999,
                IN_CLUSTER_FEE_HEADER: 88,
                TOTAL_FEE_HEADER: 1087,
            },
        ]

        merged_tables, replaced_rows = _merge_replacement_rows(tables, new_rows)
        values, clusters = _build_side_by_side_values(merged_tables)

        self.assertEqual(replaced_rows, 1)
        self.assertEqual(clusters, KNOWN_CLUSTER_ORDER)
        self.assertEqual(values[2][tdr_start:tdr_start + 4], ["01/06/2026", 999, 88, 1087])
        self.assertEqual(values[3][tdr_start:tdr_start + 4], ["02/06/2026", "300", "30", "330"])
        self.assertEqual(values[2][pky_start:pky_start + 4], ["01/06/2026", "400", "40", "440"])

    def test_rejects_unknown_cluster_for_static_layout(self):
        with self.assertRaisesRegex(ValueError, "Unknown LinkAja cluster ID"):
            _merge_replacement_rows(
                {},
                [
                    {
                        REPORT_DATE_HEADER: "01/06/2026",
                        CLUSTER_ID_HEADER: "999999",
                        EXPECTED_FEE_HEADER: 200,
                        IN_CLUSTER_FEE_HEADER: 20,
                        TOTAL_FEE_HEADER: 220,
                    }
                ],
            )

    def test_parses_new_and_legacy_cluster_titles(self):
        self.assertEqual(_cluster_id_from_title("PKY - 411311"), "411311")
        self.assertEqual(_cluster_id_from_title("CLUSTER 411311"), "411311")
        self.assertEqual(_cluster_id_from_title("411311"), "411311")

    def test_uses_static_widths_for_long_fee_columns(self):
        class Worksheet:
            id = 123

        palette = {
            "title": {"red": 0, "green": 0, "blue": 0},
            "header": {"red": 1, "green": 1, "blue": 1},
            "body": {"red": 1, "green": 1, "blue": 1},
        }
        requests = _cluster_format_requests(
            Worksheet(),
            start_column=0,
            end_column=4,
            row_count=3,
            palette=palette,
        )
        width_requests = [
            request["updateDimensionProperties"]
            for request in requests
            if "updateDimensionProperties" in request
        ]
        widths_by_start = {
            request["range"]["startIndex"]: request["properties"]["pixelSize"]
            for request in width_requests
        }

        self.assertEqual(widths_by_start[1], EXPECTED_FEE_COLUMN_WIDTH)
        self.assertEqual(widths_by_start[2], IN_CLUSTER_FEE_COLUMN_WIDTH)

    def test_centers_body_values(self):
        class Worksheet:
            id = 123

        palette = {
            "title": {"red": 0, "green": 0, "blue": 0},
            "header": {"red": 1, "green": 1, "blue": 1},
            "body": {"red": 1, "green": 1, "blue": 1},
        }
        requests = _cluster_format_requests(
            Worksheet(),
            start_column=0,
            end_column=4,
            row_count=3,
            palette=palette,
        )
        body_request = next(
            request["repeatCell"]
            for request in requests
            if (
                "repeatCell" in request
                and request["repeatCell"]["range"]["startRowIndex"] == 2
                and request["repeatCell"]["range"]["startColumnIndex"] == 0
            )
        )

        self.assertEqual(
            body_request["cell"]["userEnteredFormat"]["horizontalAlignment"],
            "CENTER",
        )

    def test_protection_editors_include_known_owner_and_service_account(self):
        class Auth:
            service_account_email = "service-account@example.com"

        class Client:
            auth = Auth()

        with patch.dict(
            "os.environ",
            {"FINPAY_PROTECTION_EDITOR_EMAILS": "owner@example.com, analyst@example.com"},
            clear=True,
        ):
            self.assertEqual(
                _protection_editor_emails(Client()),
                [
                    "analyst@example.com",
                    "owner@example.com",
                    "service-account@example.com",
                ],
            )

    def test_adds_protected_sheet_request_with_strict_editors(self):
        class Auth:
            signer_email = "service-account@example.com"

        class Client:
            auth = Auth()

        class Worksheet:
            id = 123

        with patch.dict(
            "os.environ",
            {"FINPAY_PROTECTION_EDITOR_EMAILS": "owner@example.com"},
            clear=True,
        ):
            request = _add_protected_sheet_request(
                Client(),
                Worksheet(),
                "LinkAja protected fee summary sheet",
            )

        protected_range = request["addProtectedRange"]["protectedRange"]
        self.assertEqual(protected_range["range"], {"sheetId": 123})
        self.assertFalse(protected_range["warningOnly"])
        self.assertFalse(protected_range["editors"]["domainUsersCanEdit"])
        self.assertEqual(
            protected_range["editors"]["users"],
            ["owner@example.com", "service-account@example.com"],
        )

    def test_deletes_existing_protected_ranges_for_current_sheet(self):
        class Spreadsheet:
            def fetch_sheet_metadata(self, _params):
                return {
                    "sheets": [
                        {
                            "properties": {"sheetId": 123},
                            "protectedRanges": [
                                {"protectedRangeId": 10},
                                {"protectedRangeId": 11},
                            ],
                        },
                        {
                            "properties": {"sheetId": 999},
                            "protectedRanges": [{"protectedRangeId": 99}],
                        },
                    ]
                }

        class Worksheet:
            id = 123

        self.assertEqual(
            _delete_all_protected_range_requests(Spreadsheet(), Worksheet()),
            [
                {"deleteProtectedRange": {"protectedRangeId": 10}},
                {"deleteProtectedRange": {"protectedRangeId": 11}},
            ],
        )


if __name__ == "__main__":
    unittest.main()
