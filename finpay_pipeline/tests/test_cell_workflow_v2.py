"""Version-isolated v2 config/bundle and dry-run orchestration tests."""
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finpay_pipeline.cell_contracts import OBSERVED_CELL_ROSTER
from finpay_pipeline.cell_workflow import (
    prepare_cell_bundle_v2, read_cell_configuration, read_cell_configuration_v2,
    run_cell_reporting_v2,
)
from finpay_pipeline.tests.test_cell_workflow import synthetic_bundle


def upgrade_configuration(path, roster, *, legacy="dummy-v1-workbook", spreadsheet="dummy-v2-workbook"):
    config = json.loads(path.read_text())
    config.update({
        "schema_version": 2,
        "data_classification": "SYNTHETIC_CANARY",
        "spreadsheet_id": spreadsheet,
        "legacy_spreadsheet_id": legacy,
        "approved_v2_spreadsheet_ids": [spreadsheet],
        "writer_email": "writer@example.test",
        "finance_emails": ["finance@example.test"],
        "database_target": {"host": "db.test", "port": 5432, "database": "dummy",
                             "schema": "cell_test", "user": "dummy_role"},
        "approved_database_targets": [{"host": "db.test", "port": 5432, "database": "dummy",
                                       "schema": "cell_test", "user": "dummy_role"}],
        "worksheet_targets": {},
    })
    worksheet_id = 1000
    for account in roster:
        cluster = account[:-2]
        config["worksheet_targets"][account] = {}
        for role in ("summary", "invoice", "unusual", "reversal"):
            config["worksheet_targets"][account][role] = {
                "worksheet_id": worksheet_id, "title": f"{cluster} {role.title()}"}
            worksheet_id += 1
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class CellWorkflowV2Tests(unittest.TestCase):
    def test_cluster_alias_naming_is_pinned_and_rejects_cross_cluster_names(self):
        from finpay_pipeline.cell_publication_v2 import cell_worksheet_title
        with tempfile.TemporaryDirectory() as directory:
            bundle, config, _, _, bundle_hash = self.make_inputs(Path(directory))
            runtime = json.loads(config.read_text())
            runtime["worksheet_naming"] = "cluster_alias"
            for account, group in runtime["worksheet_targets"].items():
                for role, item in group.items():
                    item["title"] = cell_worksheet_title(account, role, "cluster_alias")
            config.write_text(json.dumps(runtime))
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
            _, _, targets = read_cell_configuration_v2(config, digest, digest)
            self.assertEqual(targets["412110_B"]["summary"].title, "PKY")
            self.assertEqual(targets["412110_B"]["invoice"].title, "PKY Invoice")
            runtime["worksheet_targets"]["412110_B"]["summary"]["title"] = "MRT"
            config.write_text(json.dumps(runtime))
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "pinned role/cluster"):
                read_cell_configuration_v2(config, digest, digest)

    def make_inputs(self, root, roster=None):
        roster = roster or {"412110_B": ("412111", "412112")}
        bundle, config, _ = synthetic_bundle(root, roster)
        runtime = upgrade_configuration(config, roster)
        bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        return bundle, config, runtime, config_hash, bundle_hash

    def test_v2_config_pins_one_distinct_workbook_and_all_four_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config, runtime, config_hash, _ = self.make_inputs(Path(directory))
            parsed, accounts, targets = read_cell_configuration_v2(config, config_hash, config_hash)
            self.assertEqual(parsed["spreadsheet_id"], "dummy-v2-workbook")
            self.assertEqual(set(accounts), {"412110_B"})
            self.assertEqual(set(targets["412110_B"]), {"summary", "invoice", "unusual", "reversal"})
            with self.assertRaisesRegex(ValueError, "distinct"):
                same = upgrade_configuration(config, {"412110_B": ("412111", "412112")},
                                             legacy="dummy-v2-workbook", spreadsheet="dummy-v2-workbook")
                read_cell_configuration_v2(same, hashlib.sha256(same.read_bytes()).hexdigest(),
                                           hashlib.sha256(same.read_bytes()).hexdigest())
            upgrade_configuration(config, {"412110_B": ("412111", "412112")})
            duplicate = json.loads(config.read_text())
            duplicate["worksheet_targets"]["412110_B"]["invoice"]["worksheet_id"] = 1000
            config.write_text(json.dumps(duplicate))
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_cell_configuration_v2(config, digest, digest)

    def test_v2_bundle_barrier_and_dry_run_return_four_finpay_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, config, runtime, config_hash, bundle_hash = self.make_inputs(Path(directory))
            parsed, configs, manifest, batches = prepare_cell_bundle_v2(
                bundle, config, config_hash, bundle_hash, approved_configuration_sha256=config_hash,
            )
            self.assertEqual(len(batches["412110_B"]), 2)
            session, google = Mock(), Mock()
            result = run_cell_reporting_v2(
                bundle, config, config_hash, bundle_hash, approved_configuration_sha256=config_hash,
                dry_run=True, session_factory=session, sheet_client_factory=google,
                output_dir=Path(directory) / "dry-run",
            )
            session.assert_not_called()
            google.assert_not_called()
            self.assertEqual(result["accounts"][0]["status"], "DRY_RUN")
            matrices = json.loads((Path(directory) / "dry-run/cell_v2_matrices.json").read_text())
            self.assertEqual(set(matrices["412110_B"]), {"summary", "invoice", "unusual", "reversal"})
            invoice = matrices["412110_B"]["invoice"]["values"]
            labels = [row[1] for row in invoice]
            self.assertNotIn("LINKAJA EXPECTED RECHARGE OUT CLUSTER FEE", labels)
            self.assertNotIn("QRISDUWIT worksheet", str(matrices["412110_B"]))
            unusual = matrices["412110_B"]["unusual"]["rows"]
            reversal = matrices["412110_B"]["reversal"]["rows"]
            self.assertEqual(unusual[0][1], "CELL")
            self.assertEqual(reversal[0][1], "CELL")

    def test_all_roster_v2_dry_run_has_four_outputs_for_every_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, config, _, config_hash, bundle_hash = self.make_inputs(Path(directory), OBSERVED_CELL_ROSTER)
            result = run_cell_reporting_v2(bundle, config, config_hash, bundle_hash,
                approved_configuration_sha256=config_hash, dry_run=True,
                output_dir=Path(directory) / "dry")
            matrices = json.loads((Path(directory) / "dry/cell_v2_matrices.json").read_text())
            self.assertEqual(len(result["accounts"]), 8)
            self.assertEqual(sum(item["cells"] for item in result["accounts"]), 18)
            self.assertTrue(all(set(value) == {"summary", "invoice", "unusual", "reversal"}
                                for value in matrices.values()))

    def test_v2_target_or_config_pin_failure_precedes_factories(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, config, runtime, config_hash, bundle_hash = self.make_inputs(Path(directory))
            database, google = Mock(), Mock()
            with self.assertRaisesRegex(ValueError, "deployment-approved"):
                run_cell_reporting_v2(bundle, config, config_hash, bundle_hash,
                    approved_configuration_sha256="0" * 64, dry_run=False,
                    database_password="synthetic", session_factory=database,
                    sheet_client_factory=google)
            database.assert_not_called()
            google.assert_not_called()
            del runtime

    def test_v1_config_reader_remains_schema_one_only(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config, _, config_hash, _ = self.make_inputs(Path(directory))
            with self.assertRaisesRegex(ValueError, "unsupported cell runtime"):
                read_cell_configuration(config, config_hash)

    def test_later_date_dry_run_validates_details_without_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, config, _, _, bundle_hash = self.make_inputs(Path(directory))
            runtime = json.loads(config.read_text())
            runtime["accounts"][0].update(first_report_date="2026-10-12", opening_boundary="2026-10-12T00:00:00+08:00")
            config.write_text(json.dumps(runtime))
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
            database, google = Mock(), Mock()
            run_cell_reporting_v2(bundle, config, digest, bundle_hash,
                approved_configuration_sha256=digest, dry_run=True,
                session_factory=database, sheet_client_factory=google, output_dir=directory)
            matrices = json.loads((Path(directory) / "cell_v2_matrices.json").read_text())["412110_B"]
            self.assertEqual(matrices["summary"]["status"], "NEEDS_PRIOR_HISTORY")
            self.assertEqual(set(matrices), {"summary", "invoice", "unusual", "reversal"})
            database.assert_not_called()
            google.assert_not_called()

    def run_fake_persistence(self, directory, *, write_gsheet, roster=None):
        bundle, config, _, _, bundle_hash = self.make_inputs(Path(directory), roster)
        runtime = json.loads(config.read_text())
        runtime["dummy_data_only"] = False
        config.write_text(json.dumps(runtime))
        digest = hashlib.sha256(config.read_bytes()).hexdigest()

        class Store:
            def __init__(self, cluster):
                self.cluster_id = cluster
                self.target_id = runtime["spreadsheet_id"]
                self.facts = {}
                self.connection = SimpleNamespace(transaction=nullcontext)

            def latest_report_date(self):
                return max((day for day, _ in self.facts), default=None)

            def current_results(self, start, end):
                start = start.isoformat() if hasattr(start, "isoformat") else str(start)
                end = end.isoformat() if hasattr(end, "isoformat") else str(end)
                return [{
                    "report_date": date.fromisoformat(day),
                    "account": item["identity"]["account"],
                    "cell_code": code, "generation_id": item["generation_id"],
                    "calculation_id": item["calculation_id"], "config_id": item["config_id"],
                    "result": item["calculation"],
                } for (day, code), item in sorted(self.facts.items()) if start <= day <= end]

            def commit_cluster_day(self, batch, expected_cells):
                for item in batch:
                    self.facts[(item["report_date"], item["identity"]["cell_code"])] = item

        stores = {}
        for account in runtime["accounts"]:
            cluster = account["account"][:-2]
            stores[cluster] = Store(cluster)
        workbook = Mock()
        workbook.fetch_sheet_metadata.return_value = {
            "spreadsheetId": runtime["spreadsheet_id"],
            "sheets": [{"properties": {"sheetId": item["worksheet_id"], "title": item["title"]}}
                       for roles in runtime["worksheet_targets"].values() for item in roles.values()],
        }
        google = Mock()
        google.return_value.open_by_key.return_value = workbook

        @contextmanager
        def session(target, approved, cluster, spreadsheet, **kwargs):
            yield stores[cluster]

        with patch("finpay_pipeline.cell_workflow.publish_cell_date_blocks",
                   return_value={"status": "PUBLISHED", "report_date": "2026-10-13"}) as publish:
            result = run_cell_reporting_v2(bundle, config, digest, bundle_hash,
                approved_configuration_sha256=digest, dry_run=False, write_gsheet=write_gsheet,
                database_password="synthetic", credentials_info={"client_email": "writer@example.test"},
                session_factory=session, sheet_client_factory=google, output_dir=directory)
        self.assertTrue(all(item["status"] == "PUBLISHED" for item in result["accounts"]) if write_gsheet
                        else all(item["persisted_cells"] > 0 for item in result["accounts"]))
        if not write_gsheet:
            google.assert_not_called()
        else:
            self.assertEqual(publish.call_count, len(runtime["accounts"]))
            self.assertTrue(all(call.args[3] == "2026-10-13" for call in publish.call_args_list))

    def test_db_only_persists_all_four_outputs_without_google_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_fake_persistence(directory, write_gsheet=False)

    def test_full_roster_live_fake_path_publishes_all_32_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_fake_persistence(directory, write_gsheet=True, roster=OBSERVED_CELL_ROSTER)

if __name__ == "__main__":
    unittest.main()
