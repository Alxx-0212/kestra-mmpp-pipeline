"""No-network checks for the bulk runner's remote-write boundary and rendering."""
import ast
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from finpay_pipeline.tests import run_cell_bulk_live as live


class Response:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self):
        self.writes = []

    def request(self, method, endpoint, *args, **kwargs):
        if method.lower() == "get":
            return Response({"spreadsheetId": live.WORKBOOK, "sheets": [{"properties": {"sheetId": 9999},
                "protectedRanges": [{"protectedRangeId": 9000}]}]})
        self.writes.append((method, endpoint, kwargs))
        replies = []
        for item in kwargs.get("json", {}).get("requests", []):
            replies.append({"addProtectedRange": {"protectedRange": {"protectedRangeId": 7001}}}
                           if "addProtectedRange" in item else {})
        return Response({"replies": replies})


class BulkLiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "audit").mkdir()
        self.profile = {"targets": {}, "titles": {}, "writer_email": "writer@example.invalid", "finance_emails": ["finance@example.invalid"]}
        number = 1
        for account in live.ACCOUNTS:
            self.profile["targets"][account], self.profile["titles"][account] = {}, {}
            for role in ("summary", "invoice", "unusual", "reversal"):
                self.profile["targets"][account][role] = number
                self.profile["titles"][account][role] = account[:-2] + " " + role.title()
                number += 1
        live.ops.write_json(self.root / "profile.json", self.profile)
        self.environment = patch.dict(os.environ, {"E2E_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.http = FakeHTTP()
        self.client = type("Client", (), {"http_client": self.http})()
        self.factory = patch("finpay_pipeline.cell_workflow.make_cell_sheet_client", return_value=self.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.audit = patch.object(live, "_AUDIT", {"last_request": 0.0, "requests": 0, "mutations": 0, "sheet_ids": set(), "protected": {}})
        self.audit.start()
        self.addCleanup(self.audit.stop)
        self.pacing = patch.object(live.time, "sleep")
        self.pacing.start()
        self.addCleanup(self.pacing.stop)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + live.WORKBOOK

    def test_scoped_write_is_audited_without_private_payload(self):
        live.scoped_sheet_client({}).http_client.request("post", self.base + ":batchUpdate", json={
            "requests": [{"repeatCell": {"range": {"sheetId": 1}, "cell": {"userEnteredValue": {"numberValue": 123}}, "fields": "userEnteredValue"}}]})
        audit = json.loads((self.root / "audit/publication-mutation-audit.json").read_text())
        self.assertEqual(audit["sheet_ids"], [1])
        self.assertTrue(audit["scope_enforced"])
        self.assertNotIn("123", json.dumps(audit))

    def test_global_foreign_and_unrelated_mutations_fail_before_http(self):
        client = live.scoped_sheet_client({})
        attempts = [
            (self.base + ":batchUpdate", {"requests": [{"updateSpreadsheetProperties": {"properties": {"title": "changed"}}}]}),
            (self.base + ":batchUpdate", {"requests": [{"repeatCell": {"range": {"sheetId": 9999}}}]}),
            (self.base + ":batchUpdate", {"requests": [{"deleteProtectedRange": {"protectedRangeId": 9000}}]}),
            (self.base + "/values/Sheet1!A1", {"values": [[1]]}),
            (self.base.replace(live.WORKBOOK, "foreign") + ":batchUpdate", {"requests": []}),
        ]
        for endpoint, payload in attempts:
            with self.subTest(endpoint=endpoint), self.assertRaises(live.ops.Blocked):
                client.http_client.request("post", endpoint, json=payload)
        self.assertEqual(self.http.writes, [])

    def test_created_protection_ids_are_owned_but_unknown_ids_are_blocked(self):
        client = live.scoped_sheet_client({})
        client.http_client.request("post", self.base + ":batchUpdate", json={"requests": [
            {"addProtectedRange": {"protectedRange": {"range": {"sheetId": 1}}}}]})
        client.http_client.request("post", self.base + ":batchUpdate", json={"requests": [
            {"deleteProtectedRange": {"protectedRangeId": 7001}}]})
        with self.assertRaises(live.ops.Blocked):
            client.http_client.request("post", self.base + ":batchUpdate", json={"requests": [
                {"deleteProtectedRange": {"protectedRangeId": 7002}}]})
        self.assertEqual(len(self.http.writes), 2)

    def test_isolated_render_keeps_five_stages_and_uses_only_fixed_settings_targets(self):
        source_root = Path(__file__).resolve().parents[2]
        state = {"state": "DATABASE_READY", "resources": {"taskworkdir": str(self.root / "taskworkdir")}}
        live.ops.write_json(self.root / "state.json", state)
        (self.root / "ops").mkdir()
        with patch.dict(os.environ, {"E2E_WORKFLOW_DIR": str(source_root / "finpay_pipeline/workflows")}):
            live.render_flow()
        text = (self.root / "isolated-bulk-flow.yml").read_text()
        self.assertEqual(text.count("    taskRunner:"), 5)
        self.assertEqual(text.count("networkMode: host"), 1)
        self.assertNotIn("controller.env", text)
        self.assertNotIn(f'{self.root}:/evidence"', text)
        self.assertNotIn("/opt/finpay-ops", text)
        self.assertIn("_settings.CELL_FORMAT_WORKSHEET_IDS =", text)
        self.assertIn("defaults: false", text)
        scripts = re.findall(r"(?m)^    script: \|\n((?:^      .*\n|^\n)*)", text)
        self.assertEqual(len(scripts), 5)
        for script in scripts:
            ast.parse(textwrap.dedent(script))


if __name__ == "__main__":
    unittest.main()
