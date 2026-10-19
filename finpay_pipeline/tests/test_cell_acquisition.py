"""Synthetic-only tests; no portal, secrets or services are used."""
import hashlib
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finpay_pipeline.cell_contracts import (
    CellIdentity, configured_cells, fingerprint,
    roster_fingerprint, validate_complete_manifest,
)
from finpay_pipeline.digipos_cells import (
    _exchange, _request_fields, _resume_entry, cell_row_to_cells,
    scrape_cell_roster, validate_cell_payload,
)
from finpay_pipeline.digipos_scraper import (
    _datatables_row_to_cells, _read_xlsx_rows, _write_xhr_export,
)


class CellAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.roster = {"412110_B": ("412111", "412112")}
        self.cells = configured_cells(self.roster)
        self.identity = self.cells[0]
        self.day = "2026-10-13"

    def empty_entry(self, identity):
        return {
            **identity.to_dict(), "report_date": self.day,
            "status": "empty", "footer_total": 0, "validated_rows": 0,
            "artifact": None,
            "discovered_parentcodes": [cell.parentcode for cell in self.cells],
            "source_sha256": fingerprint({
                **identity.to_dict(), "report_date": self.day, "data": [],
            }),
        }

    def manifest(self):
        return {
            "schema_version": 1, "report_date": self.day,
            "roster_fingerprint": roster_fingerprint(self.cells),
            "cells": [self.empty_entry(cell) for cell in self.cells],
        }

    def test_contract_distinguishes_identity_and_sorted_slots(self):
        self.assertEqual(self.identity, CellIdentity("412110_B", "412110", "412111", "412110412111"))
        with self.assertRaises(ValueError):
            CellIdentity("412110_B", "412110", "412111", "412111")
        with self.assertRaises(ValueError):
            configured_cells({"412110_B": ("412111", "412111")})

    def test_manifest_requires_all_cells_exactly_once(self):
        valid = self.manifest()
        validate_complete_manifest(valid, self.cells)
        for entries in (valid["cells"][:1], [valid["cells"][0]] * 2):
            with self.subTest(entries=entries):
                invalid = {**valid, "cells": entries}
                with self.assertRaises(ValueError):
                    validate_complete_manifest(invalid, self.cells)

    def test_invalid_or_failed_source_cannot_be_empty(self):
        for payload in ({}, {"data": []}, {"statusCode": "05", "data": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_cell_payload(payload, {"draw": "1"}, 0, full=True)

    def test_successful_empty_requires_explicit_evidence(self):
        self.assertEqual(
            validate_cell_payload({"statusCode": "00", "data": []}, {"draw": "1"}, 0, True),
            [],
        )
        envelope = {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []}
        self.assertEqual(validate_cell_payload(envelope, {"draw": "1"}, 0, True), [])
        with self.assertRaises(ValueError):
            validate_cell_payload(envelope, {"draw": "2"}, 0, True)

    def test_rejects_partial_full_response_and_bool_count(self):
        with self.assertRaises(ValueError):
            validate_cell_payload({"statusCode": "00", "data": [{}]}, {}, 2, True)
        with self.assertRaises(ValueError):
            validate_cell_payload({"draw": True, "data": [], "recordsTotal": 0,
                                   "recordsFiltered": 0}, {"draw": "1"}, 0, True)

    def test_rejects_duplicate_form_parameters(self):
        with self.assertRaises(ValueError):
            _request_fields("draw=1&draw=2")

    def test_exact_ids_duplicates_and_literals_survive_export(self):
        row = {
            "tanggal": "13-10-2026 12:00:00", "wstransferid": "000000001234567890123",
            "transaction_type": "Credit", "wstransfertype": "=1+1",
            "init_amount": "1000", "amount_in": "100", "amount_out": "0",
            "last_amount": "1100", "sumber1": "490812345678", "wstransferdesc": "=2+2",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            rows = [_datatables_row_to_cells(row, index) for index in (1, 2)]
            _write_xhr_export(path, rows)
            loaded = _read_xlsx_rows(path)[2:]
            self.assertEqual(len(loaded), 2)
            self.assertEqual([r[2] for r in loaded], [row["wstransferid"]] * 2)
            self.assertEqual([r[10] for r in loaded], ["=2+2"] * 2)
            self.assertEqual([r[9] for r in loaded], ["0812345678"] * 2)
        for value in (None, "NaN", "inf", "1.25", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _datatables_row_to_cells({**row, "amount_in": value}, 1)

    def test_exchange_ignores_unrelated_pending_response(self):
        fields = (
            "draw=2&start=0&length=-1&adNo=412110_B&parentcode=412110412111"
            "&startDate=2026-10-13+00%3A00%3A00&endDate=2026-10-13+23%3A59%3A59"
        )
        request = SimpleNamespace(post_data=fields, method="POST", headers={})
        response = SimpleNamespace(
            url="https://digipos-cms.finpay.id/deposit/monitoring-riwayat-data",
            request=request, status=200, json=lambda: {"statusCode": "00", "data": []},
        )
        page = Mock()
        route = Mock(request=request)
        stored = {}
        page.route.side_effect = lambda pattern, handler: stored.update(handler=handler)

        def expected(predicate, **kwargs):
            stored["predicate"] = predicate
            return nullcontext(SimpleNamespace(value=response))

        page.expect_response.side_effect = expected

        def trigger(*args):
            stored["handler"](route)
            unrelated = SimpleNamespace(url=response.url, request=object())
            self.assertFalse(stored["predicate"](unrelated))
            self.assertTrue(stored["predicate"](response))

        page.evaluate.side_effect = trigger
        payload, request_fields = _exchange(page, self.identity, self.day, full_length=10)
        self.assertEqual(request_fields["length"], "10")
        self.assertIn("length=10", route.continue_.call_args.kwargs["post_data"])
        self.assertEqual(payload["statusCode"], "00")
        page.unroute.assert_called_once()

    def test_source_strings_round_trip_verbatim_and_money_grouping_is_strict(self):
        raw = {"tanggal": "13-10-2026 12:00:00", "wstransferid": " 000123 ",
               "init_amount": "1,000", "amount_in": "200", "amount_out": "0", "last_amount": "1,200",
               "transaction_type": "Credit", "wstransfertype": " =literal label ",
               "sumber1": "490812345678", "wstransferdesc": " =1+1 "}
        row = cell_row_to_cells(raw, 1)
        self.assertEqual((row[2], row[8], row[10]), (" 000123 ", " =literal label ", " =1+1 "))
        self.assertEqual(row[3:7], [1000, 200, 0, 1200])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            _write_xhr_export(path, [row])
            saved = _read_xlsx_rows(path)[2]
            self.assertEqual((saved[2], saved[8], saved[10]), (row[2], row[8], row[10]))
        for key in ("init_amount", "amount_in", "amount_out", "last_amount"):
            for invalid in ("1,2", "1,,000", "12,34", "1234,000", "1,000,00", "1.00", True):
                with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                    cell_row_to_cells({**raw, key: invalid}, 1)

    def test_resume_checks_checksum_and_artifact_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.empty_entry(self.identity)
            _resume_entry(entry, self.identity, self.day, root)
            with self.assertRaises(ValueError):
                _resume_entry({**entry, "source_sha256": "0" * 64}, self.identity, self.day, root)
            outside = {**entry, "status": "validated", "validated_rows": 1,
                       "footer_total": 1, "artifact": "../outside.xlsx"}
            with self.assertRaises(ValueError):
                _resume_entry(outside, self.identity, self.day, root)

    def test_partial_progress_survives_and_only_missing_cell_retries(self):
        runtime = Mock()
        browser = Mock()
        runtime.chromium.launch.return_value = browser
        fake_module = SimpleNamespace(sync_playwright=lambda: nullcontext(runtime))
        attempts = []

        def download(browser, identity, *args):
            attempts.append(identity.cell_code)
            if identity.cell_code == "412112":
                raise RuntimeError("synthetic error")
            return self.empty_entry(identity)

        with TemporaryDirectory() as directory, patch.dict(
            sys.modules, {"playwright.sync_api": fake_module, "playwright": SimpleNamespace()}
        ):
            with patch("finpay_pipeline.digipos_cells._download_cell", side_effect=download):
                with self.assertRaises(RuntimeError):
                    scrape_cell_roster(self.day, "synthetic-only", self.roster, directory, max_attempts=2)
            state = json.loads((Path(directory) / self.day / "cell_acquisition_state.json").read_text())
            self.assertEqual([row["cell_code"] for row in state["cells"]], ["412111"])
            self.assertEqual(attempts, ["412111", "412112", "412112"])
            with patch("finpay_pipeline.digipos_cells._download_cell",
                       side_effect=lambda browser, identity, *args: self.empty_entry(identity)) as mocked:
                manifest = scrape_cell_roster(self.day, "synthetic-only", self.roster, directory)
            self.assertEqual(mocked.call_count, 1)
            validate_complete_manifest(manifest, self.cells)

    def test_missing_or_corrupt_resumed_cell_is_reacquired_without_valid_peer(self):
        runtime = Mock()
        runtime.chromium.launch.return_value = Mock()
        fake_module = SimpleNamespace(sync_playwright=lambda: nullcontext(runtime))
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), TemporaryDirectory() as directory, patch.dict(
                sys.modules, {"playwright.sync_api": fake_module, "playwright": SimpleNamespace()}
            ):
                root = Path(directory) / self.day
                root.mkdir()
                bad_path = root / "bad.xlsx"
                if corrupt:
                    bad_path.write_bytes(b"not an XLSX")
                old = self.manifest()
                old["cells"][0].update(
                    status="validated", validated_rows=1, footer_total=1, artifact="bad.xlsx",
                    source_sha256=hashlib.sha256(b"not an XLSX").hexdigest(),
                )
                (root / "cell_acquisition_state.json").write_text(json.dumps(old))
                with patch("finpay_pipeline.digipos_cells._download_cell",
                           side_effect=lambda browser, identity, *args: self.empty_entry(identity)) as downloaded:
                    result = scrape_cell_roster(self.day, "synthetic-only", self.roster, directory)
                self.assertEqual(downloaded.call_count, 1)
                self.assertEqual(downloaded.call_args.args[1].cell_code, "412111")
                validate_complete_manifest(result, self.cells)


if __name__ == "__main__":
    unittest.main()
