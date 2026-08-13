import csv
import os
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from linkaja_fee_pipeline.database import (
    materialize_linkaja_month,
    persist_linkaja_normalized_csv,
)
from linkaja_fee_pipeline.migrations import migrate_linkaja_database
from linkaja_fee_pipeline.processing import (
    LINKAJA_SOURCE_COLUMNS,
    normalize_linkaja_csv,
)


TEST_DSN = os.environ.get("LINKAJA_TEST_DSN", "").strip()
TEST_DATABASE_GUARD = os.environ.get(
    "LINKAJA_TEST_DATABASE_GUARD",
    "",
).strip()

try:
    import psycopg
except ModuleNotFoundError:
    psycopg = None


def _assert_disposable_database_target(dsn, expected_database):
    if not expected_database:
        raise RuntimeError(
            "LINKAJA_TEST_DATABASE_GUARD must name the exact disposable "
            "database before destructive LinkAja integration tests can run"
        )
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            actual_database = str(cursor.fetchone()[0])
    if actual_database != expected_database:
        raise RuntimeError(
            "LinkAja destructive test guard mismatch: connected to "
            f"{actual_database!r}, expected {expected_database!r}"
        )
    return actual_database


@unittest.skipUnless(
    TEST_DSN and psycopg is not None,
    "LINKAJA_TEST_DSN and psycopg are required for database integration tests",
)
class LinkAjaDatabaseIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable_database_target(TEST_DSN, TEST_DATABASE_GUARD)
        migrate_linkaja_database(TEST_DSN)

    def setUp(self):
        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE linkaja_transactions, linkaja_monthly_refreshes, "
                    "linkaja_raw_transactions"
                )

    @staticmethod
    def _row(**overrides):
        row = {column: "" for column in LINKAJA_SOURCE_COLUMNS}
        row.update({
            "No": "1",
            "Top Organization": "123456-TEST COMPANY",
            "Parent Organization": "123456-TEST COMPANY",
            "Organization": "123456-TEST COMPANY",
            "Transaction ID": "TX-1",
            "Finalized Date": "30/06/2026",
            "Finalized Time": "12:00:00",
            "Initiate Date": "30/06/2026",
            "Initiate Time": "11:59:00",
            "Transaction Type": "Business to Business Transfer",
            "Transaction Scenario": "Digipos B2B Transfer Fee",
            "Transaction Status": "Completed",
            "Account": "6001 - Organization MFS Purchase Account",
            "Debit": "0",
            "Credit": "200",
            "Balance": "1000",
        })
        row.update(overrides)
        return row

    def _persist(self, rows, filename, load_id):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / filename
            normalized = Path(temp_dir) / "normalized.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=LINKAJA_SOURCE_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            manifest = normalize_linkaja_csv(
                source,
                normalized,
                filename_hint=filename,
                load_id=load_id,
            )
            return persist_linkaja_normalized_csv(
                normalized,
                manifest.to_json_dict(),
                TEST_DSN,
            )

    def test_non_company_facts_and_late_reversals(self):
        initial = self._persist(
            [
                self._row(
                    **{
                        "No": "1",
                        "Organization": "628111-AGENT",
                        "Transaction ID": "PPOB-1",
                        "Transaction Scenario":
                            "General to Purchase B2B Transfer Agent Telco",
                        "Account": "5001 - General Organization MFS Account",
                        "Debit": "100000",
                        "Credit": "0",
                        "Balance": "500000",
                        "Fee": "2400",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "FEE-1",
                    }
                ),
                self._row(
                    **{
                        "No": "3",
                        "Organization": "628222-AGENT",
                        "Transaction ID": "FEE-1",
                        "Account": "5002 - General Organization MFS Account",
                        "Debit": "200",
                        "Credit": "0",
                        "Balance": "2000",
                        "Fee": "200",
                    }
                ),
            ],
            "laporan-123456-initial.csv",
            "load-initial",
        )

        self.assertEqual(initial["raw_rows"], 3)
        self.assertEqual(initial["transaction_rows"], 2)
        self.assertNotIn("monthly_fee_rows", initial)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM linkaja_transactions")
                self.assertEqual(cursor.fetchone()[0], 0)

        reversal = self._persist(
            [
                self._row(
                    **{
                        "No": "1",
                        "Organization": "628111-AGENT",
                        "Transaction ID": "PPOB-REV-1",
                        "Original Transaction ID": "PPOB-1",
                        "Finalized Date": "02/07/2026",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Account": "5001 - General Organization MFS Account",
                        "Debit": "0",
                        "Credit": "100000",
                        "Balance": "600000",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "FEE-REV-1",
                        "Original Transaction ID": "FEE-1",
                        "Finalized Date": "02/07/2026",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "200",
                        "Credit": "0",
                        "Balance": "800",
                    }
                ),
            ],
            "laporan-123456-reversal.csv",
            "load-reversal",
        )

        self.assertEqual(reversal["report_dates"], ["30/06/2026", "02/07/2026"])
        self.assertNotIn("monthly_fee_rows", reversal)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        transaction_id,
                        ledger_debit_total,
                        ledger_credit_total,
                        signed_amount,
                        has_company_purchase_account,
                        source_fee,
                        is_reversed,
                        reversal_count
                    FROM linkaja_transactions_current_v
                    WHERE transaction_id IN ('PPOB-1', 'FEE-1')
                    ORDER BY transaction_id
                    """
                )
                rows = cursor.fetchall()

        self.assertEqual(
            rows,
            [
                ("FEE-1", 200, 200, 0, True, 200, True, 1),
                ("PPOB-1", 100000, 0, 100000, False, 2400, True, 1),
            ],
        )

        preview = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-06",
            calculation_cutoff="2026-07-01T00:00:00",
            materialization_id="month-preview",
            publish=False,
        )
        self.assertFalse(preview["published"])
        self.assertEqual(preview["transaction_rows"], 2)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM linkaja_transactions),
                        (SELECT COUNT(*) FROM linkaja_monthly_refreshes)
                    """
                )
                self.assertEqual(cursor.fetchone(), (0, 0))

        early = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-06",
            calculation_cutoff="2026-07-01T00:00:00",
            materialization_id="month-early",
            publish=True,
        )
        self.assertTrue(early["published"])
        self.assertEqual(early["transaction_rows"], 2)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT transaction_id, is_reversed, calculation_cutoff
                    FROM linkaja_transactions
                    ORDER BY transaction_id
                    """
                )
                early_rows = cursor.fetchall()

        self.assertEqual(
            [(row[0], row[1]) for row in early_rows],
            [("FEE-1", False), ("PPOB-1", False)],
        )

        late = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-06",
            calculation_cutoff="2026-07-03T00:00:00",
            materialization_id="month-late",
            publish=True,
        )
        self.assertEqual(late["transaction_rows"], 2)
        self.assertEqual(
            {row["ACTIVE TRANSACTION COUNT"] for row in late["fee_rows"]},
            {0},
        )

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        transaction_id,
                        is_reversed,
                        report_month,
                        materialization_id,
                        signed_amount
                    FROM linkaja_transactions
                    ORDER BY transaction_id
                    """
                )
                late_rows = cursor.fetchall()

        self.assertEqual(
            [(row[0], row[1], row[3], row[4]) for row in late_rows],
            [
                ("FEE-1", True, "month-late", 0),
                ("PPOB-1", True, "month-late", 100000),
            ],
        )

    def test_daily_fee_totals_treat_reversed_originals_as_inactive(self):
        initial = self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "DAILY-OUT-1",
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "1000",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "DAILY-IN-1",
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario":
                            "Digipos B2B Transfer In Cluster",
                        "Debit": "0",
                        "Credit": "1000",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-daily-originals.csv",
            "load-daily-originals",
        )
        initial_fee = initial["fee_rows"][0]
        self.assertEqual(initial_fee["EXPECTED RECHARGE OUT CLUSTER FEE"], 200)
        self.assertEqual(
            initial_fee["DIGIPOS B2B TRANSFER IN CLUSTER FEE"],
            20,
        )

        reversed_result = self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "DAILY-OUT-REV-1",
                        "Original Transaction ID": "DAILY-OUT-1",
                        "Finalized Date": "02/07/2026",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "1000",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "DAILY-IN-REV-1",
                        "Original Transaction ID": "DAILY-IN-1",
                        "Finalized Date": "02/07/2026",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "1000",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-daily-reversals.csv",
            "load-daily-reversals",
        )
        fee_by_date = {
            row["REPORT DATE"]: row for row in reversed_result["fee_rows"]
        }
        july_first = fee_by_date["01/07/2026"]
        self.assertEqual(july_first["EXPECTED RECHARGE OUT CLUSTER FEE"], 0)
        self.assertEqual(
            july_first["DIGIPOS B2B TRANSFER IN CLUSTER FEE"],
            0,
        )
        self.assertEqual(july_first["TOTAL FEE"], 0)

    def test_stage_rejects_inconsistent_finalized_times(self):
        with self.assertRaisesRegex(ValueError, r"times=2"):
            self._persist(
                [
                    self._row(**{"Transaction ID": "TIME-CONFLICT"}),
                    self._row(
                        **{
                            "No": "2",
                            "Transaction ID": "TIME-CONFLICT",
                            "Finalized Time": "12:00:01",
                        }
                    ),
                ],
                "laporan-123456-time-conflict.csv",
                "load-time-conflict",
            )

    def test_multiple_reversals_do_not_double_subtract_or_cross_clusters(self):
        shared_id = "SHARED-FEE-1"
        self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": shared_id,
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Fee": "200",
                    }
                )
            ],
            "laporan-123456-shared-original.csv",
            "load-123456-shared-original",
        )
        self._persist(
            [
                self._row(
                    **{
                        "Top Organization": "654321-OTHER COMPANY",
                        "Parent Organization": "654321-OTHER COMPANY",
                        "Organization": "654321-OTHER COMPANY",
                        "Transaction ID": shared_id,
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Fee": "200",
                    }
                )
            ],
            "laporan-654321-shared-original.csv",
            "load-654321-shared-original",
        )
        self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "SHARED-REVERSAL-1",
                        "Original Transaction ID": shared_id,
                        "Finalized Date": "02/08/2026",
                        "Initiate Date": "02/08/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "200",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "SHARED-REVERSAL-2",
                        "Original Transaction ID": shared_id,
                        "Finalized Date": "03/08/2026",
                        "Initiate Date": "03/08/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "200",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-multiple-reversals.csv",
            "load-123456-multiple-reversals",
        )

        reversed_month = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-multiple-reversals",
            publish=False,
        )
        other_cluster_month = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="654321",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-other-cluster",
            publish=False,
        )
        reversed_fee = next(
            row for row in reversed_month["fee_rows"]
            if row["FEE CATEGORY"] == "Digipos B2B Transfer Fee"
        )
        other_fee = next(
            row for row in other_cluster_month["fee_rows"]
            if row["FEE CATEGORY"] == "Digipos B2B Transfer Fee"
        )
        self.assertEqual(reversed_fee["GROSS TRANSACTION COUNT"], 1)
        self.assertEqual(reversed_fee["REVERSED TRANSACTION COUNT"], 1)
        self.assertEqual(reversed_fee["ACTIVE TRANSACTION COUNT"], 0)
        self.assertEqual(reversed_fee["MONTHLY PAYABLE FEE"], 0)
        self.assertEqual(other_fee["GROSS TRANSACTION COUNT"], 1)
        self.assertEqual(other_fee["REVERSED TRANSACTION COUNT"], 0)
        self.assertEqual(other_fee["ACTIVE TRANSACTION COUNT"], 1)
        self.assertEqual(other_fee["MONTHLY PAYABLE FEE"], 200)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cluster_id, reversal_count, is_reversed
                    FROM linkaja_transactions_current_v
                    WHERE transaction_id = 'SHARED-FEE-1'
                    ORDER BY cluster_id
                    """
                )
                states = cursor.fetchall()
        self.assertEqual(
            states,
            [
                ("123456", 2, True),
                ("654321", 0, False),
            ],
        )

    def test_monthly_fee_contract_uses_source_fee_and_surfaces_missing_ppob(self):
        july_date = {
            "Finalized Date": "01/07/2026",
            "Initiate Date": "01/07/2026",
        }
        self._persist(
            [
                self._row(
                    **{
                        **july_date,
                        "No": "1",
                        "Organization": "628111-AGENT",
                        "Transaction ID": "DIGIPOS-ELIGIBLE",
                        "Account": "5001 - General Organization MFS Account",
                        "Debit": "200",
                        "Credit": "0",
                        "Fee": "200",
                    }
                ),
                self._row(
                    **{
                        **july_date,
                        "No": "2",
                        "Transaction ID": "DIGIPOS-NO-FEE",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        **july_date,
                        "No": "3",
                        "Transaction ID": "DIGIPOS-OTHER-FEE",
                        "Fee": "100",
                    }
                ),
                self._row(
                    **{
                        **july_date,
                        "No": "4",
                        "Organization": "628222-AGENT",
                        "Transaction ID": "PPOB-WITH-FEE",
                        "Transaction Scenario":
                            "General to Purchase B2B Transfer Agent Telco",
                        "Account": "5002 - General Organization MFS Account",
                        "Debit": "100000",
                        "Credit": "0",
                        "Fee": "2500",
                    }
                ),
                self._row(
                    **{
                        **july_date,
                        "No": "5",
                        "Organization": "628333-AGENT",
                        "Transaction ID": "PPOB-MISSING-FEE",
                        "Transaction Scenario":
                            "General to Purchase B2B Transfer Agent Telco",
                        "Account": "5003 - General Organization MFS Account",
                        "Debit": "50000",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "6",
                        "Transaction ID": "DIGIPOS-REVERSAL",
                        "Original Transaction ID": "DIGIPOS-ELIGIBLE",
                        "Finalized Date": "02/08/2026",
                        "Initiate Date": "02/08/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "200",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-monthly-contract.csv",
            "load-monthly-contract",
        )

        result = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-contract",
            publish=True,
        )

        self.assertEqual(result["transaction_rows"], 5)
        self.assertEqual(result["unresolved_reversal_count"], 0)
        self.assertEqual(result["incomplete_fee_rows"], 1)
        self.assertFalse(result["monthly_payable_complete"])
        daily_rows = {
            row["TRANSACTION SCENARIO"]: row
            for row in result["daily_rows"]
        }
        self.assertEqual(
            daily_rows["Digipos B2B Transfer Fee"]["TRANSACTION COUNT"],
            3,
        )
        self.assertEqual(
            daily_rows["Digipos B2B Transfer Fee"]
            ["REVERSED ORIGINAL TRANSACTION COUNT"],
            1,
        )
        self.assertEqual(
            daily_rows["General to Purchase B2B Transfer Agent Telco"]
            ["PPOB SOURCE FEE MISSING COUNT"],
            1,
        )
        rows = {row["FEE CATEGORY"]: row for row in result["fee_rows"]}

        digipos = rows["Digipos B2B Transfer Fee"]
        self.assertEqual(digipos["GROSS TRANSACTION COUNT"], 1)
        self.assertEqual(digipos["REVERSED TRANSACTION COUNT"], 1)
        self.assertEqual(digipos["ACTIVE TRANSACTION COUNT"], 0)
        self.assertEqual(digipos["GROSS FEE"], 200)
        self.assertEqual(digipos["REVERSED FEE"], 200)
        self.assertEqual(digipos["NET FEE"], 0)
        self.assertEqual(digipos["MONTHLY PAYABLE FEE"], 0)
        self.assertEqual(digipos["CALCULATION STATUS"], "COMPLETE")

        ppob = rows["General to Purchase B2B Transfer Agent Telco"]
        self.assertEqual(ppob["GROSS TRANSACTION COUNT"], 2)
        self.assertEqual(ppob["ACTIVE TRANSACTION COUNT"], 2)
        self.assertIsNone(ppob["GROSS FEE"])
        self.assertIsNone(ppob["NET FEE"])
        self.assertIsNone(ppob["MONTHLY PAYABLE FEE"])
        self.assertEqual(ppob["GROSS MISSING FEE COUNT"], 1)
        self.assertEqual(ppob["ACTIVE MISSING FEE COUNT"], 1)
        self.assertEqual(
            ppob["CALCULATION STATUS"],
            "INCOMPLETE_MISSING_FEE",
        )

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        fee_category,
                        gross_transaction_count,
                        reversed_transaction_count,
                        active_transaction_count,
                        gross_fee,
                        reversed_fee,
                        net_fee,
                        monthly_payable_fee,
                        active_missing_fee_count,
                        calculation_status
                    FROM linkaja_monthly_fee_summary_v
                    WHERE cluster_id = '123456'
                      AND report_month = DATE '2026-07-01'
                    ORDER BY fee_category
                    """
                )
                published_rows = cursor.fetchall()

        self.assertEqual(
            published_rows,
            [
                (
                    "Digipos B2B Transfer Fee",
                    1,
                    1,
                    0,
                    200,
                    200,
                    0,
                    0,
                    0,
                    "COMPLETE",
                ),
                (
                    "General to Purchase B2B Transfer Agent Telco",
                    2,
                    0,
                    2,
                    None,
                    0,
                    None,
                    None,
                    1,
                    "INCOMPLETE_MISSING_FEE",
                ),
            ],
        )

    def test_reversed_missing_ppob_is_complete_and_categories_are_stable(self):
        self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "PPOB-MISSING-REVERSED",
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario":
                            "General to Purchase B2B Transfer Agent Telco",
                        "Organization": "628111-AGENT",
                        "Account": "5001 - General Organization MFS Account",
                        "Debit": "100000",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "PPOB-MISSING-REVERSAL",
                        "Original Transaction ID": "PPOB-MISSING-REVERSED",
                        "Finalized Date": "02/08/2026",
                        "Initiate Date": "02/08/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "100000",
                        "Credit": "0",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-reversed-missing-ppob.csv",
            "load-reversed-missing-ppob",
        )

        result = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-reversed-missing-ppob",
            publish=True,
        )
        rows = {row["FEE CATEGORY"]: row for row in result["fee_rows"]}
        self.assertEqual(
            set(rows),
            {
                "Digipos B2B Transfer Fee",
                "General to Purchase B2B Transfer Agent Telco",
            },
        )
        digipos = rows["Digipos B2B Transfer Fee"]
        self.assertEqual(digipos["GROSS TRANSACTION COUNT"], 0)
        self.assertEqual(digipos["MONTHLY PAYABLE FEE"], 0)
        self.assertEqual(digipos["CALCULATION STATUS"], "COMPLETE")

        ppob = rows["General to Purchase B2B Transfer Agent Telco"]
        self.assertEqual(ppob["GROSS TRANSACTION COUNT"], 1)
        self.assertEqual(ppob["REVERSED TRANSACTION COUNT"], 1)
        self.assertEqual(ppob["ACTIVE TRANSACTION COUNT"], 0)
        self.assertEqual(ppob["GROSS MISSING FEE COUNT"], 1)
        self.assertEqual(ppob["REVERSED MISSING FEE COUNT"], 1)
        self.assertEqual(ppob["ACTIVE MISSING FEE COUNT"], 0)
        self.assertIsNone(ppob["GROSS FEE"])
        self.assertIsNone(ppob["REVERSED FEE"])
        self.assertEqual(ppob["NET FEE"], 0)
        self.assertEqual(ppob["MONTHLY PAYABLE FEE"], 0)
        self.assertEqual(ppob["CALCULATION STATUS"], "COMPLETE")
        self.assertTrue(result["monthly_payable_complete"])

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fee_category, monthly_payable_fee, calculation_status
                    FROM linkaja_monthly_fee_summary_v
                    WHERE cluster_id = '123456'
                      AND report_month = DATE '2026-07-01'
                    ORDER BY fee_category
                    """
                )
                published_rows = cursor.fetchall()
        self.assertEqual(
            published_rows,
            [
                ("Digipos B2B Transfer Fee", 0, "COMPLETE"),
                (
                    "General to Purchase B2B Transfer Agent Telco",
                    0,
                    "COMPLETE",
                ),
            ],
        )

    def test_empty_month_returns_both_zero_fee_categories(self):
        result = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-01T00:00:00",
            materialization_id="month-empty",
            publish=True,
        )

        self.assertEqual(result["transaction_rows"], 0)
        self.assertEqual(len(result["fee_rows"]), 2)
        for row in result["fee_rows"]:
            self.assertEqual(row["GROSS TRANSACTION COUNT"], 0)
            self.assertEqual(row["REVERSED TRANSACTION COUNT"], 0)
            self.assertEqual(row["ACTIVE TRANSACTION COUNT"], 0)
            self.assertEqual(row["MONTHLY PAYABLE FEE"], 0)
            self.assertEqual(row["CALCULATION STATUS"], "COMPLETE")

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fee_category, monthly_payable_fee, calculation_status
                    FROM linkaja_monthly_fee_summary_v
                    WHERE cluster_id = '123456'
                      AND report_month = DATE '2026-07-01'
                    ORDER BY fee_category
                    """
                )
                published_rows = cursor.fetchall()
        self.assertEqual(len(published_rows), 2)
        self.assertTrue(
            all(row[1:] == (0, "COMPLETE") for row in published_rows)
        )

    def test_unresolved_reversal_becomes_complete_when_original_arrives(self):
        unresolved = self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "LATE-REV-1",
                        "Original Transaction ID": "LATE-ORIGINAL-1",
                        "Finalized Date": "03/07/2026",
                        "Initiate Date": "03/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "1000",
                        "Credit": "0",
                        "Balance": "0",
                    }
                )
            ],
            "laporan-123456-unresolved.csv",
            "load-unresolved",
        )

        unresolved_rows = [
            row for row in unresolved["detail_rows"]
            if row["TRANSACTION SCENARIO"] == "Unusual - Unresolved Reversal"
        ]
        self.assertEqual(len(unresolved_rows), 1)
        self.assertEqual(unresolved_rows[0]["UNRESOLVED REVERSAL COUNT"], 1)
        self.assertEqual(unresolved_rows[0]["REVERSAL IN CLUSTER FEE"], 0)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        reversal_resolution_status,
                        is_unusual,
                        unusual_reason_codes,
                        original_is_resolved
                    FROM linkaja_transactions_current_v
                    WHERE transaction_id = 'LATE-REV-1'
                    """
                )
                unresolved_state = cursor.fetchone()

        self.assertEqual(
            unresolved_state,
            ("UNRESOLVED", True, ["UNRESOLVED_REVERSAL"], False),
        )

        resolved = self._persist(
            [
                self._row(
                    **{
                        "No": "1",
                        "Transaction ID": "LATE-ORIGINAL-1",
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario":
                            "Digipos B2B Transfer In Cluster",
                        "Debit": "0",
                        "Credit": "1000",
                        "Balance": "1000",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Organization": "628333-AGENT",
                        "Transaction ID": "LATE-ORIGINAL-1",
                        "Finalized Date": "01/07/2026",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario":
                            "Digipos B2B Transfer In Cluster",
                        "Account": "5003 - General Organization MFS Account",
                        "Debit": "1000",
                        "Credit": "0",
                        "Balance": "2000",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-late-original.csv",
            "load-late-original",
        )

        self.assertEqual(resolved["report_dates"], ["01/07/2026", "03/07/2026"])
        in_cluster_fee_row = next(
            row for row in resolved["fee_rows"]
            if row["REPORT DATE"] == "01/07/2026"
        )
        self.assertEqual(
            in_cluster_fee_row["DIGIPOS B2B TRANSFER IN CLUSTER FEE"],
            0,
        )
        complete_reversal_row = next(
            row for row in resolved["detail_rows"]
            if row["TRANSACTION SCENARIO"]
            == "Complete Reversal - Digipos B2B Transfer In Cluster"
        )
        self.assertEqual(complete_reversal_row["REVERSAL IN CLUSTER COUNT"], 1)
        self.assertEqual(complete_reversal_row["REVERSAL IN CLUSTER FEE"], 20)
        self.assertEqual(complete_reversal_row["UNRESOLVED REVERSAL COUNT"], 0)

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        transaction_id,
                        reversal_resolution_status,
                        is_unusual,
                        unusual_reason_codes,
                        original_transaction_scenario,
                        transfer_scope
                    FROM linkaja_transactions_current_v
                    WHERE transaction_id IN ('LATE-ORIGINAL-1', 'LATE-REV-1')
                    ORDER BY transaction_id
                    """
                )
                resolved_states = cursor.fetchall()

        self.assertEqual(
            resolved_states,
            [
                (
                    "LATE-ORIGINAL-1",
                    "NOT_REVERSAL",
                    False,
                    [],
                    None,
                    "IN_CLUSTER",
                ),
                (
                    "LATE-REV-1",
                    "COMPLETE",
                    False,
                    [],
                    "Digipos B2B Transfer In Cluster",
                    "IN_CLUSTER",
                ),
            ],
        )

    def test_late_original_flags_frozen_month_for_explicit_refresh(self):
        self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "CLOSE-REVERSAL",
                        "Original Transaction ID": "LATE-JUNE-ORIGINAL",
                        "Finalized Date": "03/07/2026",
                        "Initiate Date": "03/07/2026",
                        "Transaction Scenario":
                            "Buy Goods Reversal for General Merchant",
                        "Debit": "200",
                        "Credit": "0",
                        "Fee": "",
                    }
                )
            ],
            "laporan-123456-closing-reversal.csv",
            "load-closing-reversal",
        )
        first_publication = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-before-late-original",
            publish=True,
        )
        self.assertEqual(first_publication["unresolved_reversal_count"], 1)
        self.assertEqual(
            first_publication["unresolved_reversal_rows"],
            [{
                "REPORT DATE": "03/07/2026",
                "FINALIZED AT LOCAL": "2026-07-03 12:00:00",
                "REVERSAL TRANSACTION ID": "CLOSE-REVERSAL",
                "ORIGINAL TRANSACTION ID": "LATE-JUNE-ORIGINAL",
                "REVERSAL SCENARIO":
                    "Buy Goods Reversal for General Merchant",
                "SOURCE LEDGER ROWS": 1,
                "LEDGER DEBIT": 200,
                "LEDGER CREDIT": 0,
                "SIGNED AMOUNT": 200,
                "COMPANY DEBIT": 200,
                "COMPANY CREDIT": 0,
                "SOURCE FEE": None,
                "COMPANY BALANCE": 1000,
                "BALANCE BEFORE": 1200,
                "SOURCE FILES":
                    "laporan-123456-closing-reversal.csv",
                "UPDATED LOAD ID": "load-closing-reversal",
                "UNUSUAL REASON CODE": "UNRESOLVED_REVERSAL",
            }],
        )

        self._persist(
            [
                self._row(
                    **{
                        "Transaction ID": "LATE-JUNE-ORIGINAL",
                        "Finalized Date": "30/06/2026",
                        "Initiate Date": "30/06/2026",
                        "Fee": "200",
                    }
                )
            ],
            "laporan-123456-late-june-original.csv",
            "load-late-june-original",
        )

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT reversal_resolution_status
                    FROM linkaja_transactions
                    WHERE transaction_id = 'CLOSE-REVERSAL'
                    """
                )
                self.assertEqual(cursor.fetchone()[0], "UNRESOLVED")

                refresh_query = (
                    Path(__file__).parents[1]
                    / "linkaja_fee_pipeline"
                    / "queries"
                    / "linkaja_monthly_refresh_candidates.sql"
                ).read_text(encoding="utf-8")
                cursor.execute(refresh_query)
                candidates = cursor.fetchall()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0:2], ("123456", date(2026, 7, 1)))
        self.assertIn(
            "ORIGINAL_FOR_CLOSING_REVERSAL_CHANGED",
            candidates[0][9],
        )

        refreshed = materialize_linkaja_month(
            TEST_DSN,
            cluster_id="123456",
            report_month="2026-07",
            calculation_cutoff="2026-08-05T00:00:00",
            materialization_id="month-after-late-original",
            publish=True,
        )
        self.assertEqual(refreshed["unresolved_reversal_count"], 0)
        self.assertEqual(refreshed["unresolved_reversal_rows"], [])

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT reversal_resolution_status
                    FROM linkaja_transactions
                    WHERE transaction_id = 'CLOSE-REVERSAL'
                    """
                )
                self.assertEqual(cursor.fetchone()[0], "COMPLETE")

    def test_dashboard_fee_and_actual_withdrawal_cycle_views(self):
        self._persist(
            [
                self._row(
                    **{
                        "No": "1",
                        "Transaction ID": "CYCLE-CREDIT-1",
                        "Finalized Date": "01/07/2026",
                        "Finalized Time": "09:00:00",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "1000",
                        "Balance": "1000",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "2",
                        "Transaction ID": "WITHDRAWAL-1",
                        "Finalized Date": "01/07/2026",
                        "Finalized Time": "12:00:00",
                        "Initiate Date": "01/07/2026",
                        "Transaction Scenario":
                            "Organization Withdraw of Funds with Next Working "
                            "Day Payment",
                        "Debit": "600",
                        "Credit": "0",
                        "Balance": "400",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "3",
                        "Transaction ID": "CYCLE-CREDIT-2",
                        "Finalized Date": "02/07/2026",
                        "Finalized Time": "10:00:00",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario": "Digipos B2B Transfer",
                        "Debit": "0",
                        "Credit": "200",
                        "Balance": "600",
                        "Fee": "",
                    }
                ),
                self._row(
                    **{
                        "No": "4",
                        "Transaction ID": "WITHDRAWAL-2",
                        "Finalized Date": "02/07/2026",
                        "Finalized Time": "16:00:00",
                        "Initiate Date": "02/07/2026",
                        "Transaction Scenario":
                            "Organization Withdraw of Funds with Next Working "
                            "Day Payment",
                        "Debit": "600",
                        "Credit": "0",
                        "Balance": "0",
                        "Fee": "",
                    }
                ),
            ],
            "laporan-123456-dashboard-cycle.csv",
            "load-dashboard-cycle",
        )

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        SUM(gross_transaction_count),
                        SUM(active_transaction_count),
                        SUM(active_fee)
                    FROM linkaja_daily_fee_reconciliation_v
                    WHERE cluster_id = '123456'
                      AND fee_type = 'EXPECTED_OUT_CLUSTER_RP200'
                    """
                )
                self.assertEqual(cursor.fetchone(), (2, 2, 400))

                cursor.execute(
                    """
                    SELECT
                        interval_start_exclusive,
                        interval_end_inclusive,
                        opening_balance,
                        non_withdrawal_company_credit,
                        withdrawal_company_debit,
                        closing_balance,
                        balance_variance,
                        linkaja_reconciliation_status,
                        mandiri_confirmation_status
                    FROM linkaja_mandiri_settlement_cycles_v
                    WHERE withdrawal_transaction_id = 'WITHDRAWAL-2'
                    """
                )
                cycle = cursor.fetchone()

        self.assertEqual(
            cycle,
            (
                datetime(2026, 7, 1, 12, 0),
                datetime(2026, 7, 2, 16, 0),
                Decimal("400"),
                Decimal("200"),
                Decimal("600"),
                Decimal("0"),
                Decimal("0"),
                "BALANCED",
                "PENDING_BANK_EVIDENCE",
            ),
        )


if __name__ == "__main__":
    unittest.main()
