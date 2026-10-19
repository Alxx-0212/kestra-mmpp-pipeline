from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import digipos_v3 as dg  # noqa: E402


class CellDiscoveryTests(unittest.TestCase):
    def test_separates_cluster_account_aggregate_and_district_cells(self):
        cells, aggregates = dg.discover_cells(
            "412110_B",
            [
                {"value": "412110", "label": "Semua Cell (Dealer 412110)"},
                {"value": "412110412111", "label": "412111 - District North"},
                {"value": "412110412112", "label": "412112 - District South"},
            ],
        )

        self.assertEqual(dg.cluster_id_for("412110_B"), "412110")
        self.assertEqual([cell.cell_code for cell in cells], ["412111", "412112"])
        self.assertEqual([cell.parentcode for cell in cells], ["412110412111", "412110412112"])
        self.assertEqual([cell.account for cell in cells], ["412110_B", "412110_B"])
        self.assertEqual(
            aggregates,
            [{"parentcode": "412110", "label": "Semua Cell (Dealer 412110)"}],
        )

    def test_rejects_cell_value_outside_cluster_prefix(self):
        with self.assertRaisesRegex(ValueError, "unexpected cell value"):
            dg.discover_cells(
                "412110_B",
                [{"value": "421111", "label": "421111 - Other cluster"}],
            )

    def test_requires_one_to_three_cells(self):
        with self.assertRaisesRegex(ValueError, "found 0"):
            dg.discover_cells("412110_B", [])
        options = [
            {"value": f"41211041211{i}", "label": f"41211{i} - District {i}"}
            for i in range(1, 5)
        ]
        with self.assertRaisesRegex(ValueError, "found 4"):
            dg.discover_cells("412110_B", options)

    def test_rejects_unknown_login_account(self):
        with self.assertRaisesRegex(ValueError, "not in the v3 _B roster"):
            dg.cluster_id_for("123456_B")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        cells, _ = dg.discover_cells(
            "412110_B",
            [{"value": "412110412111", "label": "412111 - District North"}],
        )
        self.cell = cells[0]

    def test_artifact_name_keeps_account_and_cell_grain(self):
        self.assertEqual(
            dg.artifact_filename(self.cell, "2026-10-13"),
            "finpay-412110_B-412111(13-10-2026to13-10-2026).xlsx",
        )

    def test_row_mapping_preserves_credit_phone_and_hides_it_on_debit(self):
        credit = {
            "tanggal": "13-10-2026 06:52:25",
            "wstransferid": "F_04303700001150213437",
            "init_amount": "86134048",
            "amount_in": "111100",
            "amount_out": "0",
            "last_amount": "86245148",
            "transaction_type": "Credit",
            "wstransfertype": "RECHARGE",
            "sumber1": "49082155835188",
            "wstransferdesc": "validated fixture",
        }
        debit = {**credit, "transaction_type": "Debit"}

        self.assertEqual(dg.row_to_cells(credit, 1)[9], "082155835188")
        self.assertEqual(dg.row_to_cells(debit, 2)[9], "-")

    def test_xlsx_round_trip_has_eleven_column_header_and_one_row(self):
        cells = dg.row_to_cells(
            {
                "tanggal": "13-10-2026 06:52:25",
                "wstransferid": "F_04303700001150213437",
                "init_amount": "1000",
                "amount_in": "10",
                "amount_out": "0",
                "last_amount": "1010",
                "transaction_type": "Credit",
                "wstransfertype": "RECHARGE",
                "sumber1": "49082155835188",
                "wstransferdesc": "fixture only",
            },
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.xlsx"
            dg.write_xlsx(path, [cells])
            self.assertEqual(dg.row_count_from_xlsx(path, "2026-10-13"), 1)


if __name__ == "__main__":
    unittest.main()
