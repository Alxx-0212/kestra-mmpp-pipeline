"""Offline coverage for bounded date-range cell processing."""
import hashlib
import json
import tempfile
import unittest
import warnings
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from finpay_pipeline.cell_contracts import (
    OBSERVED_CELL_ROSTER, configured_cells, fingerprint, roster_fingerprint,
)
from finpay_pipeline.cell_database import CellDatabaseTarget, validate_cluster_batch
from finpay_pipeline.cell_publication import CellWorksheetTarget
from finpay_pipeline.cell_format import bulk_pipeline
from finpay_pipeline.cell_format.bulk_pipeline import (
    persist_cell_range, preprocess_cell_range, publish_cell_range,
)
from finpay_pipeline.cell_format.settings import (
    REPORTING_START_DATE, ROSTER_VERSION, account_settings, report_config, selected_clusters,
)
from finpay_pipeline.cell_publication_v2 import ROLES, cell_worksheet_title
from finpay_pipeline.digipos_scraper import _write_xhr_export

START = "2026-10-01"
END = "2026-10-02"
CLUSTER = "412110"
ACCOUNT = CLUSTER + "_B"
TARGET = CellDatabaseTarget("synthetic-db", 5432, "synthetic", "public", "synthetic-role")


def _write_daily_bundle(root: Path, cluster: str, report_date: str) -> tuple[Path, str]:
    account, codes = account_settings(cluster)
    cells = configured_cells({account: codes})
    export_date = date.fromisoformat(report_date).strftime("%d-%m-%Y")
    entries, artifacts = [], []
    for position, cell in enumerate(cells, 1):
        artifact = f"{cell.account}-{cell.cell_code}.xlsx"
        source = root / artifact
        _write_xhr_export(source, [[
            position, f"{export_date} 12:00:00", f"000123456789012345{position}",
            1000, 100, 0, 1100, "Credit", "RECHARGE", "0812345678",
            "Transaksi Sellthru",
        ]])
        content = source.read_bytes()
        artifacts.append((artifact, content))
        entries.append({
            **cell.to_dict(), "report_date": report_date, "status": "validated",
            "footer_total": 1, "validated_rows": 1, "artifact": artifact,
            "source_sha256": hashlib.sha256(content).hexdigest(),
        })
    manifest = {
        "schema_version": 1, "report_date": report_date,
        "roster_fingerprint": roster_fingerprint(cells), "cells": entries,
    }
    path = root / f"daily-{cluster}-{report_date}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        for name, content in artifacts:
            archive.writestr(name, content)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source_bulk(root: Path, start=START, end=END, cluster_id=CLUSTER, *,
                       entry_mutator=None, member_mutator=None, duplicate_member=False):
    clusters = list(selected_clusters(cluster_id))
    entries, members = [], []
    for cluster in clusters:
        for report_date in bulk_pipeline._dates(start, end):
            daily_path, _ = _write_daily_bundle(root, cluster, report_date)
            member_path = bulk_pipeline._daily_source_path(cluster, report_date)
            content = daily_path.read_bytes()
            if member_mutator:
                content = member_mutator(member_path, content)
            members.append((member_path, content))
            entries.append({
                "cluster_id": cluster, "report_date": report_date,
                "path": member_path, "sha256": hashlib.sha256(content).hexdigest(),
                "cells": len(account_settings(cluster)[1]),
            })
    if entry_mutator:
        entries = entry_mutator(entries)
    index = {
        "schema_version": 1, "start_date": start, "end_date": end,
        "clusters": clusters,
        "roster_version": ROSTER_VERSION, "entries": entries,
    }
    path = root / "cell_bulk_sources_v1.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("index.json", json.dumps(index, sort_keys=True))
        for name, content in members:
            archive.writestr(name, content)
            if duplicate_member:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, content)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class MemoryConnection:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def transaction(self):
        state = (
            deepcopy(self.store.facts), deepcopy(self.store.sources),
            deepcopy(self.store.configurations), deepcopy(self.store.dirty),
            list(self.store.commits), self.store.commit_calls,
        )
        try:
            yield
        except Exception:
            (self.store.facts, self.store.sources, self.store.configurations,
             self.store.dirty, self.store.commits, self.store.commit_calls) = state
            raise


class MemoryCellStore:
    def __init__(self):
        self.facts, self.sources, self.configurations = {}, {}, {}
        self.dirty, self.commits = set(), []
        self.commit_calls, self.fail_on_commit = 0, None
        self.connection = MemoryConnection(self)

    def earliest_report_date(self):
        return min((day for day, _ in self.facts), default=None)

    def latest_report_date(self):
        return max((day for day, _ in self.facts), default=None)

    def current_results(self, start, end):
        return [{
            "report_date": date.fromisoformat(day), "account": item["identity"]["account"],
            "cluster_id": item["identity"]["cluster_id"], "cell_code": code,
            "generation_id": item["generation_id"], "calculation_id": item["calculation_id"],
            "config_id": item["config_id"], "result": item["calculation"],
        } for (day, code), item in sorted(self.facts.items()) if start <= day <= end]

    def current_source_rows(self, start, end):
        return [{"manifest": item["manifest"], "raw_rows": item["raw_rows"]}
                for (day, _), item in sorted(self.facts.items()) if start <= day <= end]

    def commit_cluster_day(self, batch, expected_cells):
        validate_cluster_batch(batch, expected_cells)
        self.commit_calls += 1
        if self.commit_calls == self.fail_on_commit:
            raise RuntimeError("injected date-cohort commit failure")
        self.commits.append(batch)
        for item in batch:
            key = (item["report_date"], item["identity"]["cell_code"])
            self.facts[key] = item
            self.sources[item["generation_id"]] = (item["manifest"], item["raw_rows"])
            self.configurations[item["config_id"]] = item["configuration"]
            self.dirty.add(item["report_date"])


def _session_for(store, calls=None):
    @contextmanager
    def session(target, approved, cluster, sheet_id, **kwargs):
        if calls is not None:
            calls.append((target, approved, cluster, sheet_id, kwargs))
        yield store
    return session


def _make_handoff_bundle(root: Path, cluster_ids=None) -> tuple[Path, str]:
    clusters = list(selected_clusters("ALL")) if cluster_ids is None else sorted(cluster_ids, key=int)
    entries = []
    with zipfile.ZipFile(root / "cell_bulk_handoffs_v1.zip", "w") as archive:
        for cluster in clusters:
            config = report_config(cluster, REPORTING_START_DATE)
            selected = []
            for report_date in bulk_pipeline._dates(START, END):
                for cell in config.cells:
                    selected.append({
                        "account": config.account, "cluster_id": cluster,
                        "cell_code": cell.cell_code, "report_date": report_date,
                        "generation_id": f"generation-{cluster}-{report_date}-{cell.cell_code}",
                        "calculation_id": f"calculation-{cluster}-{report_date}-{cell.cell_code}",
                        "config_id": f"config-{cluster}",
                    })
            handoff = {
                "schema_version": 1, "cluster_id": cluster, "account": config.account,
                "submitted_report_date": END, "first_report_date": REPORTING_START_DATE,
                "last_report_date": END, "roster_fingerprint": roster_fingerprint(config.cells),
                "selected": selected, "history_fingerprint": fingerprint(selected),
            }
            content = json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode()
            path = bulk_pipeline._handoff_path(cluster)
            archive.writestr(path, content)
            entries.append({
                "cluster_id": cluster, "path": path,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            })
        index = {
            "schema_version": 1, "start_date": START, "end_date": END,
            "cluster_id": "ALL" if len(clusters) > 1 else clusters[0],
            "clusters": clusters, "roster_version": ROSTER_VERSION, "entries": entries,
        }
        archive.writestr("index.json", json.dumps(index, sort_keys=True))
    path = root / "cell_bulk_handoffs_v1.zip"
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class CellBulkPipelineTests(unittest.TestCase):
    def _preprocess(self, directory):
        source, source_sha = _write_source_bulk(directory)
        output = Path(directory) / "cell_bulk_prepared_v1.zip"
        result = preprocess_cell_range(
            str(source), source_sha, START, END, CLUSTER, str(output),
        )
        return source, source_sha, output, result

    def _persist(self, prepared_path, result, store, output_path=None, calls=None):
        session_factory = _session_for(store, calls)
        output_path = output_path or prepared_path.with_name("cell_bulk_handoffs_v1.zip")
        return persist_cell_range(
            str(prepared_path), result["prepared_bundle_sha256"],
            database_password="synthetic-password", output_path=str(output_path),
            target=TARGET, session_factory=session_factory,
        )

    def test_preprocess_emits_exact_two_day_two_cell_prepared_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, prepared_path, result = self._preprocess(Path(temporary))
            self.assertEqual(result["prepared_entries"], 2)
            self.assertEqual(result["cluster_id"], CLUSTER)
            with zipfile.ZipFile(prepared_path) as archive:
                index = json.loads(archive.read("index.json"))
                self.assertEqual(
                    {item["report_date"] for item in index["entries"]}, {START, END},
                )
                self.assertEqual(set(archive.namelist()), {
                    "index.json", bulk_pipeline._daily_prepared_path(CLUSTER, START),
                    bulk_pipeline._daily_prepared_path(CLUSTER, END),
                })
                for entry in index["entries"]:
                    content = archive.read(entry["path"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
                    self.assertEqual(len(json.loads(content)["batch"]), 2)

    def test_source_bundle_requires_exact_coverage_checksum_safe_paths_and_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_sha = _write_source_bulk(
                root, entry_mutator=lambda entries: entries[:-1],
            )
            with self.assertRaisesRegex(ValueError, "every selected cluster/date"):
                preprocess_cell_range(str(source), source_sha, START, END, CLUSTER)

            source, source_sha = _write_source_bulk(root, duplicate_member=True)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                preprocess_cell_range(str(source), source_sha, START, END, CLUSTER)

            source, source_sha = _write_source_bulk(root)
            with zipfile.ZipFile(source) as old:
                index = json.loads(old.read("index.json"))
                source_entry = index["entries"][0]
                old_path = source_entry["path"]
                source_entry["path"] = "../escape.zip"
                traversal = root / "traversal.zip"
                with zipfile.ZipFile(traversal, "w") as new:
                    new.writestr("index.json", json.dumps(index))
                    new.writestr("../escape.zip", old.read(old_path))
                    for name in old.namelist():
                        if name not in ("index.json", old_path):
                            new.writestr(name, old.read(name))
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                preprocess_cell_range(
                    str(traversal), hashlib.sha256(traversal.read_bytes()).hexdigest(),
                    START, END, CLUSTER,
                )

            source, source_sha = _write_source_bulk(root)
            with self.assertRaisesRegex(ValueError, "checksum mismatch|size limit"):
                preprocess_cell_range(str(source), "0" * 64, START, END, CLUSTER)
            with patch.object(bulk_pipeline, "MAX_BULK_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "exceeds configured size limit"):
                    preprocess_cell_range(str(source), source_sha, START, END, CLUSTER)

    def test_corrupt_daily_zip_and_member_checksum_fail_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_sha = _write_source_bulk(
                root, member_mutator=lambda name, content: b"not-a-daily-zip"
                if name.endswith(f"{START}.zip") else content,
            )
            output = root / "failed-prepared.zip"
            with self.assertRaises(zipfile.BadZipFile):
                preprocess_cell_range(str(source), source_sha, START, END, CLUSTER, str(output))
            self.assertFalse(output.exists())

            source, source_sha = _write_source_bulk(root)
            with zipfile.ZipFile(source) as archive:
                index = json.loads(archive.read("index.json"))
            index["entries"][0]["sha256"] = "0" * 64
            rewritten = root / "wrong-member-hash.zip"
            with zipfile.ZipFile(source) as old, zipfile.ZipFile(rewritten, "w") as new:
                new.writestr("index.json", json.dumps(index))
                for name in old.namelist():
                    if name != "index.json":
                        new.writestr(name, old.read(name))
            with self.assertRaisesRegex(ValueError, "member size/checksum mismatch"):
                preprocess_cell_range(
                    str(rewritten), hashlib.sha256(rewritten.read_bytes()).hexdigest(),
                    START, END, CLUSTER, str(root / "wrong-member-prepared.zip"),
                )

    def test_range_commit_rebinds_config_and_emits_full_history_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, prepared_path, prepared = self._preprocess(root)
            store, sessions = MemoryCellStore(), []
            with patch("finpay_pipeline.cell_calculation.calculate_cell_day") as calculate:
                result = self._persist(prepared_path, prepared, store, calls=sessions)
                calculate.assert_not_called()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(store.commits), 2)
            self.assertEqual([len(batch) for batch in store.commits], [2, 2])
            self.assertEqual(len(store.configurations), 1)
            config = next(iter(store.configurations.values()))
            self.assertEqual(config["first_report_date"], REPORTING_START_DATE)
            self.assertEqual(set(store.facts), {
                (START, code) for code in OBSERVED_CELL_ROSTER[ACCOUNT]
            } | {(END, code) for code in OBSERVED_CELL_ROSTER[ACCOUNT]})
            self.assertTrue(all(
                item["raw_rows"][0]["Saldo Awal"] == 1000 for item in store.facts.values()
            ))
            handoff_path = root / "cell_bulk_handoffs_v1.zip"
            with zipfile.ZipFile(handoff_path) as archive:
                index = json.loads(archive.read("index.json"))
                self.assertEqual(index["end_date"], END)
                handoff = json.loads(archive.read(index["entries"][0]["path"]))
            self.assertEqual(handoff["submitted_report_date"], END)
            self.assertEqual(handoff["first_report_date"], REPORTING_START_DATE)
            self.assertEqual(handoff["last_report_date"], END)
            self.assertEqual(len(handoff["selected"]), 4)
            self.assertEqual(result["committed_clusters"], 1)

    def test_all_date_cohorts_rollback_together_when_one_commit_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, prepared_path, prepared = self._preprocess(root)
            store = MemoryCellStore()
            store.fail_on_commit = 2
            output = root / "must-not-exist.zip"
            with self.assertRaisesRegex(RuntimeError, "injected date-cohort"):
                self._persist(prepared_path, prepared, store, output_path=output)
            self.assertEqual(store.facts, {})
            self.assertEqual(store.sources, {})
            self.assertEqual(store.configurations, {})
            self.assertEqual(store.commits, [])
            self.assertFalse(output.exists())

    def test_persist_preflights_every_prepared_member_before_opening_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, prepared_path, prepared = self._preprocess(root)
            broken = root / "missing-prepared-day.zip"
            with zipfile.ZipFile(prepared_path) as old:
                index = json.loads(old.read("index.json"))
                index["entries"].pop()
                with zipfile.ZipFile(broken, "w") as new:
                    new.writestr("index.json", json.dumps(index))
                    for name in old.namelist():
                        if name != "index.json" and name.endswith(f"{START}.json"):
                            new.writestr(name, old.read(name))
            session_factory = Mock()
            with self.assertRaisesRegex(ValueError, "every selected cluster/date"):
                persist_cell_range(
                    str(broken), hashlib.sha256(broken.read_bytes()).hexdigest(),
                    database_password="synthetic", target=TARGET,
                    session_factory=session_factory,
                )
            session_factory.assert_not_called()

    def test_dry_runs_create_no_database_or_sheet_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, prepared_path, prepared = self._preprocess(root)
            session_factory, sheet_factory = Mock(), Mock()
            result = persist_cell_range(
                str(prepared_path), prepared["prepared_bundle_sha256"],
                database_password=None, dry_run=True, session_factory=session_factory,
            )
            self.assertEqual(result["status"], "DRY_RUN")
            session_factory.assert_not_called()

            store = MemoryCellStore()
            persisted = self._persist(prepared_path, prepared, store)
            handoffs = root / "cell_bulk_handoffs_v1.zip"
            database_factory = Mock()
            published = publish_cell_range(
                str(handoffs), persisted["handoff_bundle_sha256"],
                database_password=None, credentials_info=None, dry_run=True,
                session_factory=database_factory, sheet_client_factory=sheet_factory,
            )
            self.assertEqual(published["status"], "DRY_RUN")
            database_factory.assert_not_called()
            sheet_factory.assert_not_called()

    def test_publisher_runs_once_per_cluster_and_forwards_sheet_factory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoffs, digest = _make_handoff_bundle(root)
            sheet_factory = Mock()
            database_factory = Mock()

            def fake_targets(cluster):
                targets = {role: SimpleNamespace(writer_email="writer@example.test") for role in ROLES}
                return "workbook", "legacy", targets

            def fake_publish(path, sha256, **kwargs):
                return {"status": "COMPLETE", "cluster_id": kwargs["cluster_id"]}

            with patch.object(bulk_pipeline, "validate_cell_bulk_sheet_range"), \
                    patch.object(bulk_pipeline, "sheet_targets", side_effect=fake_targets), \
                    patch.object(bulk_pipeline.daily, "publish_cell_sheets", side_effect=fake_publish) as publish:
                result = publish_cell_range(
                    str(handoffs), digest, database_password="synthetic",
                    credentials_info={"client_email": "writer@example.test"}, target=TARGET,
                    session_factory=database_factory, sheet_client_factory=sheet_factory,
                )
            clusters = list(selected_clusters("ALL"))
            self.assertEqual([call.kwargs["cluster_id"] for call in publish.call_args_list], clusters)
            self.assertTrue(all(
                call.kwargs["sheet_client_factory"] is sheet_factory
                for call in publish.call_args_list
            ))
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(len(result["publications"]), len(clusters))

    def test_stale_or_unknown_cluster_publication_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoffs, digest = _make_handoff_bundle(root)
            calls = []

            def fake_targets(cluster):
                return "workbook", "legacy", {
                    role: SimpleNamespace(writer_email="writer@example.test") for role in ROLES
                }

            def fake_publish(path, sha256, **kwargs):
                calls.append(kwargs["cluster_id"])
                if kwargs["cluster_id"] == "422140":
                    raise RuntimeError("stale cell Sheet handoff: target has an ambiguous publication")
                return {"status": "COMPLETE", "cluster_id": kwargs["cluster_id"]}

            with patch.object(bulk_pipeline, "validate_cell_bulk_sheet_range"), \
                    patch.object(bulk_pipeline, "sheet_targets", side_effect=fake_targets), \
                    patch.object(bulk_pipeline.daily, "publish_cell_sheets", side_effect=fake_publish):
                with self.assertRaisesRegex(RuntimeError, "stale cell Sheet handoff"):
                    publish_cell_range(
                        str(handoffs), digest, database_password="synthetic",
                        credentials_info={"client_email": "writer@example.test"}, target=TARGET,
                    )
            self.assertEqual(calls, list(selected_clusters("ALL"))[:6])

    def test_bulk_target_preflight_rejects_cross_cluster_worksheet_id_collisions(self):
        def duplicate_targets(cluster):
            account, _ = account_settings(cluster)
            targets = {
                role: CellWorksheetTarget(
                    role, "cell-workbook", position, cell_worksheet_title(account, role),
                    "writer@example.test", ("finance@example.test",),
                )
                for position, role in enumerate(ROLES, 101)
            }
            return "cell-workbook", "legacy-workbook", targets

        with patch.object(bulk_pipeline, "sheet_targets", side_effect=duplicate_targets):
            with self.assertRaisesRegex(ValueError, "IDs/titles must be unique"):
                bulk_pipeline.validate_cell_bulk_sheet_range(START, END, "ALL")

    def test_five_stage_workflow_scripts_compile_and_keep_writes_serial(self):
        workflow = Path(__file__).parents[1] / "workflows" / "finpay_cell_bulk_v1.yml"
        text = workflow.read_text(encoding="utf-8")
        ids = (
            "preflight_cell_bulk_targets", "acquire_cell_bulk", "preprocess_cell_bulk",
            "persist_cell_bulk", "publish_cell_bulk",
        )
        positions = [text.index(f"- id: {task_id}") for task_id in ids]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("scrape_workers\n    type: INT\n    defaults: 2\n    min: 1\n    max: 4", text)
        self.assertNotIn("ForEach", text)
        self.assertNotIn("Parallel", text)
        scripts, current = [], None
        for line in text.splitlines():
            if line == "    script: |":
                if current is not None:
                    scripts.append("\n".join(current))
                current = []
            elif current is not None and line.startswith("      "):
                current.append(line[6:])
            elif current is not None and line.strip():
                scripts.append("\n".join(current))
                current = None
        if current is not None:
            scripts.append("\n".join(current))
        self.assertEqual(len(scripts), 5)
        for index, script in enumerate(scripts, 1):
            compile(script, f"finpay_cell_bulk_v1.yml:script-{index}", "exec")


if __name__ == "__main__":
    unittest.main()
