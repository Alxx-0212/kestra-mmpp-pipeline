import csv
import tempfile
import unittest
from pathlib import Path

from linkaja_fee_pipeline.july_acceptance import analyze_linkaja_july_bundle
from linkaja_fee_pipeline.processing import LINKAJA_SOURCE_COLUMNS


class LinkAjaJulyAcceptanceTest(unittest.TestCase):
    @staticmethod
    def _row(**overrides):
        row = {column: "" for column in LINKAJA_SOURCE_COLUMNS}
        row.update({
            "No": "1",
            "Top Organization": "123456-TEST COMPANY",
            "Parent Organization": "123456-TEST COMPANY",
            "Organization": "123456-TEST COMPANY",
            "Transaction ID": "TX-1",
            "Finalized Date": "01/07/2026",
            "Finalized Time": "12:00:00",
            "Initiate Date": "01/07/2026",
            "Initiate Time": "11:59:00",
            "Transaction Type": "Business to Business Transfer",
            "Transaction Scenario": "Digipos B2B Transfer In Cluster",
            "Transaction Status": "Completed",
            "Account": "6001 - Organization MFS Purchase Account",
            "Debit": "0",
            "Credit": "1000",
            "Balance": "1000",
        })
        row.update(overrides)
        return row

    @staticmethod
    def _write(path, rows):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LINKAJA_SOURCE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def test_current_rules_remove_directly_reversed_original_fees(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self._write(
                directory / "laporan-123456-TEST-100.csv",
                [
                    self._row(**{"Transaction ID": "IN-1"}),
                    self._row(
                        **{
                            "No": "2",
                            "Transaction ID": "OUT-1",
                            "Transaction Scenario": "Digipos B2B Transfer",
                        }
                    ),
                    self._row(
                        **{
                            "No": "3",
                            "Transaction ID": "FEE-1",
                            "Transaction Scenario": "Digipos B2B Transfer Fee",
                            "Credit": "200",
                            "Fee": "200",
                        }
                    ),
                    self._row(
                        **{
                            "No": "4",
                            "Organization": "628111-AGENT",
                            "Transaction ID": "PPOB-ACTIVE",
                            "Transaction Scenario":
                                "General to Purchase B2B Transfer Agent Telco",
                            "Account": "5001 - General Organization MFS Account",
                            "Debit": "100000",
                            "Credit": "0",
                            "Fee": "2500",
                        }
                    ),
                    self._row(
                        **{
                            "No": "5",
                            "Organization": "628222-AGENT",
                            "Transaction ID": "PPOB-MISSING",
                            "Transaction Scenario":
                                "General to Purchase B2B Transfer Agent Telco",
                            "Account": "5002 - General Organization MFS Account",
                            "Debit": "50000",
                            "Credit": "0",
                            "Fee": "",
                        }
                    ),
                    self._row(
                        **{
                            "No": "6",
                            "Transaction ID": "UNRESOLVED",
                            "Original Transaction ID": "MISSING-ORIGINAL",
                            "Finalized Date": "15/07/2026",
                            "Transaction Scenario":
                                "Buy Goods Reversal for General Merchant",
                            "Debit": "1000",
                            "Credit": "0",
                        }
                    ),
                ],
            )
            self._write(
                directory / "laporan-123456-TEST-200.csv",
                [
                    self._row(
                        **{
                            "Transaction ID": "IN-REV",
                            "Original Transaction ID": "IN-1",
                            "Finalized Date": "02/08/2026",
                            "Transaction Scenario":
                                "Buy Goods Reversal for General Merchant",
                            "Debit": "1000",
                            "Credit": "0",
                        }
                    ),
                    self._row(
                        **{
                            "No": "2",
                            "Transaction ID": "OUT-REV",
                            "Original Transaction ID": "OUT-1",
                            "Finalized Date": "02/08/2026",
                            "Transaction Scenario":
                                "Buy Goods Reversal for General Merchant",
                            "Debit": "1000",
                            "Credit": "0",
                        }
                    ),
                    self._row(
                        **{
                            "No": "3",
                            "Transaction ID": "FEE-REV",
                            "Original Transaction ID": "FEE-1",
                            "Finalized Date": "02/08/2026",
                            "Transaction Scenario":
                                "Buy Goods Reversal for General Merchant",
                            "Debit": "200",
                            "Credit": "0",
                        }
                    ),
                    self._row(
                        **{
                            "No": "4",
                            "Transaction ID": "PPOB-REV",
                            "Original Transaction ID": "PPOB-MISSING",
                            "Finalized Date": "02/08/2026",
                            "Transaction Scenario":
                                "Buy Goods Reversal for General Merchant",
                            "Debit": "50000",
                            "Credit": "0",
                        }
                    ),
                ],
            )

            result = analyze_linkaja_july_bundle(directory)

        aggregate = result["aggregate"]
        in_cluster = aggregate["active_daily_fee_rules"][
            "digipos_b2b_transfer_in_cluster_rp20"
        ]
        out_cluster = aggregate["active_daily_fee_rules"][
            "digipos_b2b_transfer_expected_rp200"
        ]
        digipos_fee = aggregate["monthly_fee_summary"][
            "digipos_b2b_transfer_fee"
        ]
        ppob = aggregate["monthly_fee_summary"][
            "general_to_purchase_agent_telco"
        ]

        for fixed_fee in (in_cluster, out_cluster, digipos_fee):
            self.assertEqual(fixed_fee["gross_transaction_count"], 1)
            self.assertEqual(fixed_fee["reversed_transaction_count"], 1)
            self.assertEqual(fixed_fee["active_transaction_count"], 0)
            self.assertEqual(fixed_fee["active_fee"], 0)

        self.assertEqual(ppob["gross_transaction_count"], 2)
        self.assertEqual(ppob["reversed_transaction_count"], 1)
        self.assertEqual(ppob["active_transaction_count"], 1)
        self.assertEqual(ppob["gross_missing_fee_count"], 1)
        self.assertEqual(ppob["active_missing_fee_count"], 0)
        self.assertEqual(ppob["active_fee"], 2500)
        self.assertEqual(ppob["known_active_fee"], 2500)
        self.assertEqual(ppob["active_missing_fee_transactions"], [])
        self.assertEqual(ppob["calculation_status"], "COMPLETE")

        reversals = aggregate["reversals"]
        self.assertEqual(reversals["total"], 5)
        self.assertEqual(reversals["resolved_count"], 4)
        self.assertEqual(reversals["unresolved_count"], 1)
        self.assertEqual(reversals["report_month_unresolved_count"], 1)
        self.assertEqual(reversals["relevant_late_reversal_count"], 4)
        self.assertEqual(reversals["next_month_reversal_count"], 0)


if __name__ == "__main__":
    unittest.main()
