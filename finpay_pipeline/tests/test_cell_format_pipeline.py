"""Offline tests for the separated, code-owned cell-format pipeline."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from copy import deepcopy
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from finpay_pipeline.cell_contracts import (
    OBSERVED_CELL_ROSTER, configured_cells, roster_fingerprint,
)
from finpay_pipeline.cell_database import CellDatabaseTarget
from finpay_pipeline.cell_format import (
    acquire_cell_day, expand_cell_range, persist_cell_day, preprocess_cell_day,
    publish_cell_sheets, validate_sheet_range,
)
from finpay_pipeline.cell_format import settings
from finpay_pipeline.cell_format import pipeline as cell_pipeline
from finpay_pipeline.cell_format.pipeline import _load_prepared
from finpay_pipeline.cell_calculation import build_cluster_report
from finpay_pipeline.cell_publication_v2 import ROLES, build_cell_cluster_matrices_v2
from finpay_pipeline.digipos_scraper import _write_xhr_export, expected_source_filename
from finpay_pipeline.legacy import (
    acquire_legacy_day, expand_legacy_range, verify_legacy_artifact,
)

CELL_START = settings.REPORTING_START_DATE
CELL_NEXT = "2026-10-02"


class MemoryConnection:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def transaction(self):
        snapshot = (
            deepcopy(self.store.facts), deepcopy(self.store.sources),
            deepcopy(self.store.configurations), deepcopy(self.store.dirty),
            list(self.store.commits),
        )
        try:
            yield
        except Exception:
            (self.store.facts, self.store.sources, self.store.configurations,
             self.store.dirty, self.store.commits) = snapshot
            raise


class MemoryCellStore:
    def __init__(self, facts=None):
        self.facts = {} if facts is None else dict(facts)
        self.commits = []
        self.sources = {}
        self.configurations = {}
        self.dirty = set()
        self.commit_calls = 0
        self.fail_on_commit = None
        self.connection = MemoryConnection(self)

    def earliest_report_date(self):
        return min((day for day, _ in self.facts), default=None)

    def latest_report_date(self):
        return max((day for day, _ in self.facts), default=None)

    def current_results(self, start, end):
        rows = []
        for (day, code), item in sorted(self.facts.items()):
            if start <= day <= end:
                rows.append({
                    "report_date": date.fromisoformat(day),
                    "account": item["identity"]["account"],
                    "cluster_id": item["identity"]["cluster_id"],
                    "cell_code": code, "generation_id": item["generation_id"],
                    "calculation_id": item["calculation_id"], "config_id": item["config_id"],
                    "result": item["calculation"],
                })
        return rows

    def current_source_rows(self, start, end):
        return [
            {"manifest": item["manifest"], "raw_rows": item["raw_rows"]}
            for (day, _), item in sorted(self.facts.items()) if start <= day <= end
        ]

    def commit_cluster_day(self, batch, expected_cells):
        self.commit_calls += 1
        if self.commit_calls == self.fail_on_commit:
            raise RuntimeError("injected cohort commit failure")
        self.commits.append(batch)
        for item in batch:
            self.facts[(item["report_date"], item["identity"]["cell_code"])] = item
            self.sources[item["generation_id"]] = (item["manifest"], item["raw_rows"])
            self.configurations[item["config_id"]] = item["configuration"]
            self.dirty.add(item["report_date"])


def source_bundle(directory, day, roster=None):
    roster = roster or {"412110_B": OBSERVED_CELL_ROSTER["412110_B"]}
    cells = configured_cells(roster)
    report_day = date.fromisoformat(day)
    export_date = report_day.strftime("%d-%m-%Y")
    entries, contents = [], {}
    for index, cell in enumerate(cells):
        name = f"{cell.account}-{cell.cell_code}.xlsx"
        path = Path(directory) / name
        row = [
            index + 1, f"{export_date} 12:00:00", f"000123456789012345{index}",
            1000, 100, 0, 1100, "Credit", "RECHARGE", "0812345678", "Transaksi Sellthru",
        ]
        _write_xhr_export(path, [row])
        content = path.read_bytes()
        contents[name] = content
        entries.append({
            **cell.to_dict(), "report_date": day, "status": "validated",
            "footer_total": 1, "validated_rows": 1, "artifact": name,
            "source_sha256": hashlib.sha256(content).hexdigest(),
        })
    manifest = {
        "schema_version": 1, "report_date": day,
        "roster_fingerprint": roster_fingerprint(cells), "cells": entries,
    }
    path = Path(directory) / f"sources-{day}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        for name, content in contents.items():
            archive.writestr(name, content)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class CellFormatPipelineTests(unittest.TestCase):
    def setUp(self):
        self.target = CellDatabaseTarget("synthetic-db", 5432, "synthetic", "public", "synthetic-role")
        self.cluster_id = "412110"
        self.account = self.cluster_id + "_B"

    def persist_with_store(self, prepared_path, prepared_sha, store):
        observed = {}

        @contextmanager
        def session(target, approved, cluster_id, sheet_id, **kwargs):
            observed.update(target=target, approved=approved, cluster=cluster_id,
                            sheet_id=sheet_id, kwargs=kwargs)
            yield store

        result = persist_cell_day(
            str(prepared_path), prepared_sha, database_password="synthetic-password",
            output_path=str(Path(prepared_path).with_name("cell_handoff_v1.json")),
            target=self.target, session_factory=session,
        )
        self.assertEqual(observed["cluster"], self.cluster_id)
        self.assertEqual(observed["sheet_id"], "")
        self.assertFalse(observed["kwargs"]["write_gsheet"])
        return result, observed

    def test_inclusive_range_and_all_cluster_expansion(self):
        runs = expand_cell_range(CELL_START, CELL_NEXT, self.cluster_id)
        self.assertEqual(runs, [
            {"report_date": CELL_START, "cluster_id": self.cluster_id},
            {"report_date": CELL_NEXT, "cluster_id": self.cluster_id},
        ])
        all_runs = expand_cell_range(CELL_START, CELL_START, "ALL")
        self.assertEqual(len(all_runs), len(OBSERVED_CELL_ROSTER))
        self.assertEqual({item["cluster_id"] for item in all_runs},
                         {account[:-2] for account in OBSERVED_CELL_ROSTER})
        grouped = expand_cell_range(CELL_START, CELL_START, " 422010, 412110 ")
        self.assertEqual({item["cluster_id"] for item in grouped}, {"412110", "422010"})
        self.assertEqual(len(grouped), 2)
        self.assertEqual(len(expand_legacy_range("2026-10-13", "2026-10-13", "ALL")), 6)
        with self.assertRaisesRegex(ValueError, "on or before"):
            expand_cell_range(CELL_NEXT, CELL_START, self.cluster_id)
        with self.assertRaisesRegex(ValueError, "configured clusters"):
            expand_cell_range(CELL_START, CELL_START, "999999")
        with self.assertRaisesRegex(ValueError, "distinct configured clusters"):
            expand_cell_range(CELL_START, CELL_START, "412110,412110")

    def test_pre_start_range_and_acquisition_fail_before_portal_io(self):
        with self.assertRaisesRegex(ValueError, "starts on 2026-10-01"):
            expand_cell_range("2026-09-30", CELL_START, self.cluster_id)
        with tempfile.TemporaryDirectory() as directory, patch(
            "finpay_pipeline.cell_format.acquisition.scrape_cell_roster",
        ) as scrape:
            with self.assertRaisesRegex(ValueError, "starts on 2026-10-01"):
                acquire_cell_day("2026-09-30", self.cluster_id, "synthetic", directory)
            scrape.assert_not_called()

    def test_acquisition_uses_only_selected_cluster_roster_and_no_uploaded_config(self):
        cells = configured_cells({self.account: OBSERVED_CELL_ROSTER[self.account]})
        manifest = {
            "report_date": CELL_START, "cells": [
                {**cell.to_dict(), "artifact": None} for cell in cells
            ],
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "finpay_pipeline.cell_format.acquisition.scrape_cell_roster", return_value=manifest,
        ) as scrape:
            result = acquire_cell_day(CELL_START, self.cluster_id, "synthetic", directory)
            scrape.assert_called_once()
            self.assertEqual(scrape.call_args.args[2], {
                self.account: OBSERVED_CELL_ROSTER[self.account],
            })
            self.assertTrue(Path(result["source_bundle"]).is_file())
            self.assertEqual(result["cells"], len(cells))
            with zipfile.ZipFile(result["source_bundle"]) as archive:
                self.assertEqual(archive.namelist(), ["manifest.json"])
        flow = Path(__file__).parents[1] / "workflows" / "finpay_cell_range_v1.yml"
        text = flow.read_text(encoding="utf-8")
        self.assertNotIn("configuration_sha256", text)
        self.assertNotIn("configuration:", text)

    def test_previous_format_entrypoint_acquires_one_validated_cluster_day(self):
        content = b"synthetic validated workbook bytes"
        digest = hashlib.sha256(content).hexdigest()

        def download(cluster_id, day, password, directory, max_attempts, *, max_file_bytes=None):
            self.assertEqual(max_file_bytes, 128 * 1024 * 1024)
            (directory / f"{cluster_id}.xlsx").write_bytes(content)
            return {"artifact": f"{cluster_id}.xlsx", "row_count": 3, "sha256": digest}

        with tempfile.TemporaryDirectory() as directory, patch(
            "finpay_pipeline.legacy.acquisition._download_cluster", side_effect=download,
        ) as call:
            result = acquire_legacy_day(
                "2026-10-13", "411311", "synthetic", directory,
            )
            call.assert_called_once()
            self.assertEqual(result["filename"], "finpay-411311(13-10-2026to13-10-2026).xlsx")
            self.assertEqual(Path(result["artifact_path"]).name, "source.xlsx")
            self.assertEqual(Path(result["artifact_path"]).read_bytes(), content)
            self.assertEqual(result["sha256"], digest)

    def test_previous_format_acquisition_rejects_oversized_artifact(self):
        def download(cluster_id, day, password, directory, max_attempts, *, max_file_bytes=None):
            (directory / f"{cluster_id}.xlsx").write_bytes(b"12345")
            return {"artifact": f"{cluster_id}.xlsx", "row_count": 3, "sha256": "ignored"}

        with tempfile.TemporaryDirectory() as directory, \
                patch("finpay_pipeline.legacy.acquisition.MAX_LEGACY_ARTIFACT_BYTES", 4), \
                patch("finpay_pipeline.legacy.acquisition._download_cluster", side_effect=download) as call:
            with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                acquire_legacy_day("2026-10-13", "411311", "synthetic", directory)
            self.assertEqual(call.call_args.kwargs["max_file_bytes"], 4)
            self.assertFalse((Path(directory) / "411311.xlsx").exists())
            self.assertFalse((Path(directory) / "source.xlsx").exists())

    def test_previous_format_internal_handoff_detects_tampering_and_size_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "source.xlsx"
            manifest_path = Path(directory) / "legacy_acquisition_manifest.json"
            content = b"synthetic workbook"
            artifact.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            manifest_path.write_text(json.dumps({
                "report_date": "2026-10-13", "cluster_id": "411311",
                "filename": expected_source_filename("411311", "2026-10-13"),
                "row_count": 1, "sha256": digest,
            }), encoding="utf-8")
            self.assertEqual(verify_legacy_artifact(
                str(artifact), str(manifest_path), digest, "2026-10-13", "411311",
            )["sha256"], digest)
            artifact.write_bytes(b"tampered workbook")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_legacy_artifact(
                    str(artifact), str(manifest_path), digest, "2026-10-13", "411311",
                )

            artifact.write_bytes(b"12345")
            with patch("finpay_pipeline.legacy.acquisition.MAX_LEGACY_ARTIFACT_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                    verify_legacy_artifact(
                        str(artifact), str(manifest_path), digest, "2026-10-13", "411311",
                    )

    def test_preprocess_zero_seed_and_database_only_persist_retain_history(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = MemoryCellStore()
            outputs = []
            for day in (CELL_START, CELL_NEXT):
                bundle, bundle_sha = source_bundle(directory, day)
                prepared_path = directory / f"prepared-{day}.json"
                result = preprocess_cell_day(
                    str(bundle), bundle_sha, day, self.cluster_id, str(prepared_path),
                )
                prepared, config, batch = _load_prepared(
                    prepared_path, result["prepared_sha256"], self.cluster_id, day,
                )
                self.assertEqual(config.opening_balances,
                                 {code: 0 for code in OBSERVED_CELL_ROSTER[self.account]})
                self.assertTrue(all(item["raw_rows"][0]["Saldo Awal"] == 1000 for item in batch))
                saved = json.loads(prepared_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["batch"][0]["configuration"]["opening_balances"],
                                 {code: 0 for code in OBSERVED_CELL_ROSTER[self.account]})
                persisted, session = self.persist_with_store(
                    prepared_path, result["prepared_sha256"], store,
                )
                outputs.append(persisted)
                self.assertEqual(session["target"], self.target)
            self.assertEqual(len(store.commits), 2)
            self.assertTrue(all(
                item["config_id"] == store.facts[(CELL_START, item["identity"]["cell_code"])]["config_id"]
                for item in store.facts.values()
            ))
            handoff = json.loads((directory / "cell_handoff_v1.json").read_text(encoding="utf-8"))
            self.assertEqual(handoff["first_report_date"], CELL_START)
            self.assertEqual(handoff["last_report_date"], CELL_NEXT)
            self.assertEqual(len(handoff["selected"]), 2 * len(OBSERVED_CELL_ROSTER[self.account]))
            config = settings.report_config(self.cluster_id, CELL_START)
            facts = list(store.facts.values())
            report_days = build_cluster_report(config, facts, CELL_NEXT)["days"]
            self.assertEqual(report_days[0]["combined"]["opening_saldo"], 0)
            self.assertEqual(report_days[1]["combined"]["opening_saldo"],
                             report_days[0]["combined"]["closing_saldo"])
            summary = build_cell_cluster_matrices_v2(
                config, facts, CELL_NEXT, dummy=True,
            )["summary"]
            for block in summary["blocks"]:
                saldo_column = block["start_column"] - 1 + 2
                self.assertEqual(summary["numeric_preview"][2][saldo_column], 0)
            second_range = next(
                item for item in summary["date_ranges"]
                if item["report_date"] == CELL_NEXT
            )
            carry_row = next(index for index in range(
                second_range["start_row"] - 1, second_range["end_row"],
            ) if summary["row_keys"][index].startswith("DETAIL:") and all(
                summary["numeric_preview"][index][block["start_column"] - 1 + column] == 0
                for block in summary["blocks"] for column in (0, 1)
            ))
            prior_close = {
                cell["cell_code"]: cell["closing_saldo"] for cell in report_days[0]["cells"]
            }
            for block in summary["blocks"]:
                saldo_column = block["start_column"] - 1 + 2
                self.assertEqual(summary["numeric_preview"][carry_row][saldo_column],
                                 prior_close[block["cell_code"]])
            rerun, _ = self.persist_with_store(
                prepared_path, result["prepared_sha256"], store,
            )
            self.assertEqual(rerun["history_fingerprint"], outputs[-1]["history_fingerprint"])
            self.assertEqual(len(store.commits), 3)

    def test_later_day_preprocesses_offline_but_cannot_initialize_history(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            bundle, bundle_sha = source_bundle(directory, CELL_NEXT)
            prepared_path = directory / "prepared-later.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_NEXT, self.cluster_id, str(prepared_path),
            )
            _, provisional, batch = _load_prepared(
                prepared_path, prepared["prepared_sha256"], self.cluster_id, CELL_NEXT,
            )
            self.assertEqual(provisional.first_report_date, CELL_NEXT)
            self.assertEqual(len(batch), len(OBSERVED_CELL_ROSTER[self.account]))

            store = MemoryCellStore()
            with self.assertRaisesRegex(ValueError, "missing cell coverage for report date 2026-10-01"):
                self.persist_with_store(prepared_path, prepared["prepared_sha256"], store)
            self.assertEqual(store.commits, [])
            self.assertEqual(store.sources, {})
            self.assertEqual(store.configurations, {})
            self.assertEqual(store.dirty, set())
            self.assertFalse((directory / "cell_handoff_v1.json").exists())

    def test_history_before_reporting_start_is_rejected_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            bundle, bundle_sha = source_bundle(directory, CELL_START)
            prepared_path = directory / "prepared-start.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_START, self.cluster_id, str(prepared_path),
            )
            store = MemoryCellStore({
                ("2026-09-30", code): {"existing": True}
                for code in OBSERVED_CELL_ROSTER[self.account]
            })
            before = deepcopy(store.facts)
            with self.assertRaisesRegex(ValueError, "predates the code-owned reporting start"):
                self.persist_with_store(prepared_path, prepared["prepared_sha256"], store)
            self.assertEqual(store.facts, before)
            self.assertEqual(store.commits, [])
            self.assertEqual(store.sources, {})
            self.assertEqual(store.configurations, {})
            self.assertEqual(store.dirty, set())
            self.assertFalse((directory / "cell_handoff_v1.json").exists())

    def test_dry_run_and_missing_sheet_targets_instantiate_no_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, bundle_sha = source_bundle(directory, CELL_START)
            prepared_path = Path(directory) / "prepared.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_START, self.cluster_id, str(prepared_path),
            )
            database = Mock()
            result = persist_cell_day(
                str(prepared_path), prepared["prepared_sha256"], database_password=None,
                dry_run=True, session_factory=database,
            )
            self.assertEqual(result["status"], "DRY_RUN")
            database.assert_not_called()

            store = MemoryCellStore()
            persisted, _ = self.persist_with_store(
                prepared_path, prepared["prepared_sha256"], store,
            )
            sheet_client = Mock()
            database = Mock()
            sheet_result = publish_cell_sheets(
                str(Path(directory) / "cell_handoff_v1.json"), persisted["handoff_sha256"],
                database_password=None, credentials_info=None, dry_run=True,
                session_factory=database, sheet_client_factory=sheet_client,
            )
            self.assertEqual(sheet_result["status"], "DRY_RUN")
            database.assert_not_called()
            sheet_client.assert_not_called()
            with patch.object(settings, "CELL_FORMAT_SPREADSHEET_ID", ""), \
                    patch.object(settings, "LEGACY_SPREADSHEET_ID", ""):
                with self.assertRaisesRegex(ValueError, "CELL_FORMAT_SPREADSHEET_ID or CELL_FORMAT_SPREADSHEET_NAME"):
                    validate_sheet_range(CELL_START, CELL_START, self.cluster_id)
            sheet_client.assert_not_called()

    def test_named_workbook_history_guard_precedes_workbook_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, bundle_sha = source_bundle(directory, CELL_START)
            prepared_path = Path(directory) / "prepared.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_START, self.cluster_id, str(prepared_path),
            )
            store = MemoryCellStore()
            persisted, _ = self.persist_with_store(prepared_path, prepared["prepared_sha256"], store)
            configuration = settings.CellSheetConfiguration(
                None, "Cell Reports", "legacy-workbook", "writer@example.test",
                ("finance@example.test",), True, True, None,
            )
            client = Mock()
            client.http_client.request.return_value.json.return_value = {
                "files": [], "incompleteSearch": False,
            }

            class HistoryStore:
                def assert_cluster_target(self, target_id):
                    if target_id is None:
                        raise ValueError("cluster already has publication history in another workbook")

            @contextmanager
            def session(*args, **kwargs):
                yield HistoryStore()

            with patch.object(cell_pipeline, "sheet_configuration", return_value=configuration), \
                    patch.object(cell_pipeline, "_verify_handoff_history", return_value=None), \
                    patch.object(cell_pipeline, "resolve_cell_workbook", wraps=cell_pipeline.resolve_cell_workbook) as resolve:
                with self.assertRaisesRegex(ValueError, "another workbook"):
                    publish_cell_sheets(
                        str(Path(directory) / "cell_handoff_v1.json"), persisted["handoff_sha256"],
                        database_password="synthetic-password",
                        credentials_info={"client_email": "writer@example.test"},
                        cluster_id=self.cluster_id, target=self.target,
                        session_factory=session,
                        sheet_client_factory=lambda *args, **kwargs: client,
                    )
            self.assertEqual(resolve.call_args.kwargs["cluster_id"], self.cluster_id)
            client.create.assert_not_called()

    def test_all_range_preflight_rejects_cross_cluster_worksheet_id_aliases(self):
        accounts = sorted(OBSERVED_CELL_ROSTER)
        configured = {}
        for cluster_index, account in enumerate(accounts):
            configured[account] = {
                role: 1000 + cluster_index * 10 + role_index
                for role_index, role in enumerate(ROLES)
            }
        configured[accounts[1]][ROLES[0]] = configured[accounts[0]][ROLES[0]]
        with patch.object(settings, "CELL_FORMAT_SPREADSHEET_ID", "synthetic-cell-workbook"), \
                patch.object(settings, "LEGACY_SPREADSHEET_ID", "synthetic-legacy-workbook"), \
                patch.object(settings, "CELL_FORMAT_WRITER_EMAIL", "writer@example.test"), \
                patch.object(settings, "CELL_FORMAT_FINANCE_EMAILS", ("finance@example.test",)), \
                patch.object(settings, "CELL_FORMAT_WORKSHEET_IDS", configured):
            with self.assertRaisesRegex(ValueError, "worksheet IDs/titles must be unique"):
                validate_sheet_range(CELL_START, CELL_START, "ALL")

    def test_new_workflow_inline_python_compiles(self):
        workflow_dir = Path(__file__).parents[1] / "workflows"
        names = (
            "finpay_legacy_day_v1.yml", "finpay_legacy_range_v1.yml",
            "finpay_cell_day_v1.yml", "finpay_cell_range_v1.yml",
        )
        for name in names:
            path = workflow_dir / name
            lines = path.read_text(encoding="utf-8").splitlines()
            blocks = 0
            for index, line in enumerate(lines):
                if line != "    script: |":
                    continue
                body = []
                for script_line in lines[index + 1:]:
                    if script_line.startswith("      "):
                        body.append(script_line[6:])
                    elif not script_line.strip():
                        body.append("")
                    else:
                        break
                self.assertTrue(body, f"empty inline script in {name}")
                compile("\n".join(body), f"{name}:script-{blocks + 1}", "exec")
                blocks += 1
            self.assertGreater(blocks, 0, f"no inline script found in {name}")

    def test_stale_handoff_fails_before_google_client_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = MemoryCellStore()
            for day in (CELL_START, CELL_NEXT):
                bundle, bundle_sha = source_bundle(directory, day)
                prepared_path = directory / f"prepared-{day}.json"
                prepared = preprocess_cell_day(
                    str(bundle), bundle_sha, day, self.cluster_id, str(prepared_path),
                )
                persisted, _ = self.persist_with_store(
                    prepared_path, prepared["prepared_sha256"], store,
                )
            handoff_path = directory / "cell_handoff_v1.json"
            key = next(key for key in store.facts if key[0] == CELL_START)
            store.facts[key] = {**store.facts[key], "calculation_id": "stale-calculation"}

            worksheet_ids = {self.account: {role: 400 + index for index, role in enumerate(ROLES)}}
            with patch.object(settings, "CELL_FORMAT_SPREADSHEET_ID", "synthetic-cell-workbook"), \
                    patch.object(settings, "LEGACY_SPREADSHEET_ID", "synthetic-legacy-workbook"), \
                    patch.object(settings, "CELL_FORMAT_WRITER_EMAIL", "writer@example.test"), \
                    patch.object(settings, "CELL_FORMAT_FINANCE_EMAILS", ("finance@example.test",)), \
                    patch.object(settings, "CELL_FORMAT_WORKSHEET_IDS", worksheet_ids):
                @contextmanager
                def session(*args, **kwargs):
                    self.assertEqual(args[3], "synthetic-cell-workbook")
                    self.assertTrue(kwargs["write_gsheet"])
                    yield store

                sheet_client = Mock()
                with self.assertRaisesRegex(ValueError, "stale cell Sheet handoff"):
                    publish_cell_sheets(
                        str(handoff_path), persisted["handoff_sha256"],
                        database_password="synthetic-password",
                        credentials_info={"client_email": "writer@example.test"},
                        cluster_id=self.cluster_id, target=self.target,
                        session_factory=session, sheet_client_factory=sheet_client,
                    )
                sheet_client.assert_not_called()

    def test_stale_history_and_submitted_cohorts_rollback_together(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = MemoryCellStore()
            bundle, bundle_sha = source_bundle(directory, CELL_NEXT)
            prepared_path = directory / f"prepared-{CELL_NEXT}.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_NEXT, self.cluster_id, str(prepared_path),
            )
            _, config, batch = _load_prepared(prepared_path, prepared["prepared_sha256"])
            store.commit_cluster_day(batch, config.cells)
            before = (
                deepcopy(store.facts), deepcopy(store.sources),
                deepcopy(store.configurations), deepcopy(store.dirty), list(store.commits),
            )

            bundle, bundle_sha = source_bundle(directory, CELL_START)
            prepared_path = directory / f"prepared-{CELL_START}.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_START, self.cluster_id, str(prepared_path),
            )
            store.fail_on_commit = store.commit_calls + 2

            @contextmanager
            def session(target, approved, cluster_id, sheet_id, **kwargs):
                yield store

            handoff_path = directory / "failed-handoff.json"
            with self.assertRaisesRegex(RuntimeError, "injected cohort commit failure"):
                persist_cell_day(
                    str(prepared_path), prepared["prepared_sha256"],
                    database_password="synthetic-password", output_path=str(handoff_path),
                    target=self.target, session_factory=session,
                )
            self.assertEqual(
                (store.facts, store.sources, store.configurations, store.dirty, store.commits),
                before,
            )
            self.assertFalse(handoff_path.exists())

    def test_ambiguous_database_publication_gate_precedes_google(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            bundle, bundle_sha = source_bundle(directory, CELL_START)
            prepared_path = directory / "prepared.json"
            prepared = preprocess_cell_day(
                str(bundle), bundle_sha, CELL_START, self.cluster_id, str(prepared_path),
            )
            store = MemoryCellStore()
            persisted, _ = self.persist_with_store(prepared_path, prepared["prepared_sha256"], store)
            worksheet_ids = {self.account: {role: 500 + index for index, role in enumerate(ROLES)}}
            with patch.object(settings, "CELL_FORMAT_SPREADSHEET_ID", "synthetic-cell-workbook"), \
                    patch.object(settings, "LEGACY_SPREADSHEET_ID", "synthetic-legacy-workbook"), \
                    patch.object(settings, "CELL_FORMAT_WRITER_EMAIL", "writer@example.test"), \
                    patch.object(settings, "CELL_FORMAT_FINANCE_EMAILS", ("finance@example.test",)), \
                    patch.object(settings, "CELL_FORMAT_WORKSHEET_IDS", worksheet_ids):
                @contextmanager
                def refused(*args, **kwargs):
                    raise RuntimeError("target has an ambiguous publication; operator reconciliation required")
                    yield  # pragma: no cover

                sheet_client = Mock()
                with self.assertRaisesRegex(RuntimeError, "ambiguous publication"):
                    publish_cell_sheets(
                        str(directory / "cell_handoff_v1.json"), persisted["handoff_sha256"],
                        database_password="synthetic-password",
                        credentials_info={"client_email": "writer@example.test"},
                        cluster_id=self.cluster_id, target=self.target,
                        session_factory=refused, sheet_client_factory=sheet_client,
                    )
                sheet_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
