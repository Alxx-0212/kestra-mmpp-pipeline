"""Offline safety checks only; no portal, database or Google clients."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finpay_pipeline.tests import run_cell_live_design_e2e as run


class LiveDesignRunScopeTests(unittest.TestCase):
    def test_config_is_exact_real_source_test_scope_with_zero_balances(self):
        config = run.configuration("writer@example.test")
        self.assertEqual(config["data_classification"], "PRODUCTION")
        self.assertEqual(len(config["accounts"]), 8)
        self.assertEqual(sum(len(item["cell_codes"]) for item in config["accounts"]), 18)
        self.assertTrue(all(set(item["opening_balances"].values()) == {0} for item in config["accounts"]))
        ids = {item["worksheet_id"] for group in run.TARGETS.values() for item in group.values()}
        self.assertEqual(ids, set(range(1900174101, 1900174133)))
        self.assertEqual(config["database_target"]["schema"], "cell_design_test")
        self.assertNotEqual(config["spreadsheet_id"], config["legacy_spreadsheet_id"])

    def test_prepare_refuses_replacement_and_config_drift_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            key = base / "fake-key.json"
            key.write_text(json.dumps({"client_email": "writer@example.test"}))
            root = base / "run"
            digest = hashlib.sha256(b"writer@example.test").hexdigest()
            with patch.object(run, "WRITER_SHA256", digest), contextlib.redirect_stdout(io.StringIO()):
                run.prepare(root, key)
            pin = json.loads((root / "result.json").read_text())["configuration_sha256"]
            self.assertEqual(run.check_inputs(root, pin)["spreadsheet_id"], run.BOOK)
            self.assertEqual((root / "db-password").stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "not fresh"):
                run.prepare(root, key)
            with self.assertRaisesRegex(ValueError, "checksum"):
                run.check_inputs(root, "f" * 64)

    def test_destination_sharing_must_match_pinned_private_principals(self):
        data = {"id": run.BOOK, "name": "Salinan dari Monitoring Finpay & LinkAja", "capabilities": {"canEdit": True},
            "permissions": [{"id": "1", "type": "user", "role": "owner"}, {"id": "2", "type": "user", "role": "writer"}]}
        response = Mock()
        response.json.return_value = data
        client = SimpleNamespace(http_client=SimpleNamespace(request=Mock(return_value=response)))
        digest = hashlib.sha256(json.dumps(data["permissions"], sort_keys=True).encode()).hexdigest()
        with patch.object(run, "SHARING_SHA256", digest):
            self.assertEqual(run.private_destination(client), digest)
            data["permissions"].append({"id": "3", "type": "anyone", "role": "reader"})
            with self.assertRaisesRegex(ValueError, "sharing differs"):
                run.private_destination(client)
        client.http_client.request.assert_called_with("get", "https://www.googleapis.com/drive/v3/files/" + run.BOOK,
            params={"fields": "id,name,permissions(id,type,role),capabilities(canEdit)"})

    def test_rejected_attempt_cannot_overwrite_sealed_success_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            result.write_text(json.dumps({"status": "PASS", "checks": {"e2e": "PASS"}}))
            before = result.read_bytes()
            with patch("sys.argv", ["run", "prepare", "--root", str(root), "--credential-file", str(root / "absent-key")]), \
                    patch.object(run.os, "umask"):
                with self.assertRaisesRegex(RuntimeError, "prepare stopped"):
                    run.main()
            self.assertEqual(result.read_bytes(), before)

    def test_private_root_rejects_permissions_and_symlink_in_test_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            run.private_root(root, create=True)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                run.private_root(root)
            root.chmod(0o700)
            link = Path(directory) / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                run.private_root(link)

    def test_source_receipt_requires_independent_pin_and_acquisition_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = {day: {"sha256": "a" * 64, "rows": 10, "cells": 18, "empty_cells": 0}
                       for day in run.DATES}
            run.dump(root / "sources.json", receipt)
            run.dump(root / "result.json", {"status": "ACQUIRED", "source_summary": receipt})
            pin = run.sha(root / "sources.json")
            self.assertEqual(run.source_receipt(root, pin), receipt)
            receipt[run.DATES[0]]["rows"] = 11
            run.dump(root / "sources.json", receipt)
            with self.assertRaisesRegex(ValueError, "receipt pin"):
                run.source_receipt(root, pin)
            with self.assertRaisesRegex(ValueError, "acquisition checkpoint"):
                run.source_receipt(root, run.sha(root / "sources.json"))

    def test_recovery_rejects_unpinned_attempt_before_clients_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            key = base / "fake-key.json"
            key.write_text(json.dumps({"client_email": "writer@example.test"}))
            root = base / "run"
            with patch.object(run, "WRITER_SHA256", hashlib.sha256(b"writer@example.test").hexdigest()), \
                    contextlib.redirect_stdout(io.StringIO()):
                run.prepare(root, key)
            pin = json.loads((root / "result.json").read_text())["configuration_sha256"]
            with patch.object(run, "design_client") as google, patch("psycopg.connect") as database:
                with self.assertRaisesRegex(ValueError, "pinned failed attempt"):
                    run.resume(root, pin, key, "a" * 64, "b" * 64)
                google.assert_not_called()
                database.assert_not_called()

    def test_recovery_evidence_does_not_overwrite_precheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            precheck, outcome = root / "precheck.json", root / "outcome.json"
            run.dump_once(precheck, {"status": "PRECHECKED"})
            before = precheck.read_bytes()
            with self.assertRaises(FileExistsError):
                run.dump_once(precheck, {"status": "COMPLETE"})
            run.dump_once(outcome, {"status": "COMPLETE", "precheck_sha256": run.sha(precheck)})
            self.assertEqual(precheck.read_bytes(), before)

    def test_recovery_records_cas_unconfirmed_separately_without_claiming_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            precheck = root / "precheck.json"
            audit = {"status": "PRECHECKED"}
            run.dump_once(precheck, audit)
            before = precheck.read_bytes()
            run.recovery_outcome(root / "outcome.json", precheck, audit, transitioned=False, error=ValueError("CAS lost"))
            result = json.loads((root / "outcome.json").read_text())
            self.assertEqual(result["status"], "CAS_UNCONFIRMED")
            self.assertFalse(result["transition_confirmed"])
            self.assertEqual(precheck.read_bytes(), before)

    def test_transport_retries_only_read_requests_never_mutations(self):
        from requests.exceptions import ReadTimeout
        client = SimpleNamespace(http_client=SimpleNamespace(request=Mock()))
        response = object()
        client.http_client.request.side_effect = [ReadTimeout("read"), response]
        with patch("finpay_pipeline.cell_workflow.make_cell_sheet_client", return_value=client), \
                patch.object(run.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            original = client.http_client.request
            run.design_client({})
            self.assertIs(client.http_client.request("get", "https://example.test"), response)
            self.assertEqual(original.call_count, 2)
        client = SimpleNamespace(http_client=SimpleNamespace(request=Mock(side_effect=ReadTimeout("write"))))
        with patch("finpay_pipeline.cell_workflow.make_cell_sheet_client", return_value=client), patch.object(run.time, "sleep"):
            original = client.http_client.request
            run.design_client({})
            with self.assertRaises(ReadTimeout):
                client.http_client.request("post", "https://example.test", json={"requests": []})
            self.assertEqual(original.call_count, 1)


if __name__ == "__main__":
    unittest.main()
