import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkaja_fee_pipeline.processing import (
    CLUSTER_ID_HEADER,
    EXPECTED_FEE_HEADER,
    IN_CLUSTER_FEE_HEADER,
    REPORT_DATE_HEADER,
    TOTAL_FEE_HEADER,
    aggregate_linkaja_fee_file,
)
from linkaja_fee_pipeline.sheets import matching_date_cluster_rows
from linkaja_fee_pipeline.sheets import (
    CLUSTER_START_COLUMNS,
    DATA_START_ROW,
    EXPECTED_FEE_COLUMN_WIDTH,
    IN_CLUSTER_FEE_COLUMN_WIDTH,
    KNOWN_CLUSTER_ORDER,
    STATIC_SHEET_COL_COUNT,
    _add_protected_sheet_request,
    _build_static_header_values,
    _build_side_by_side_values,
    _cluster_format_requests,
    _cluster_id_from_title,
    _delete_all_protected_range_requests,
    _extract_cluster_tables,
    _merge_replacement_rows,
    _protection_editor_emails,
    _write_cluster_data_blocks,
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

        def update(self, range_name, values, value_input_option=None):
            self.updates.append((range_name, values, value_input_option))

    class FakeSpreadsheet:
        def __init__(self):
            self.batch_updates = []

        def batch_update(self, payload):
            self.batch_updates.append(payload)

        def fetch_sheet_metadata(self, _params):
            return {"sheets": [{"properties": {"sheetId": 123}, "protectedRanges": []}]}

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
