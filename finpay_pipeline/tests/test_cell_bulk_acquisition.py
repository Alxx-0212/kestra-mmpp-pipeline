"""Offline tests for the account-parallel cell source handoff."""
import hashlib
import json
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from finpay_pipeline.cell_contracts import (
    configured_cells, fingerprint, roster_fingerprint,
)
from finpay_pipeline.cell_format.bulk_acquisition import acquire_cell_range
from finpay_pipeline.cell_format.settings import (
    ROSTER_VERSION, account_settings, selected_clusters,
)


def _daily_bundle(cluster_id: str, report_date: str, output_dir: str) -> dict:
    account, codes = account_settings(cluster_id)
    cells = configured_cells({account: codes})
    manifest_cells = []
    for cell in cells:
        manifest_cells.append({
            **cell.to_dict(), "report_date": report_date, "status": "empty",
            "footer_total": 0, "validated_rows": 0, "artifact": None,
            "discovered_parentcodes": sorted(item.parentcode for item in cells),
            "source_sha256": fingerprint({
                **cell.to_dict(), "report_date": report_date, "data": [],
            }),
        })
    manifest = {
        "schema_version": 1, "report_date": report_date,
        "roster_fingerprint": roster_fingerprint(cells), "cells": manifest_cells,
    }
    path = Path(output_dir) / "cell_sources_v2.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    return {
        "cluster_id": cluster_id, "report_date": report_date,
        "source_bundle": str(path),
        "source_bundle_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cells": len(cells),
    }


class CellBulkAcquisitionTests(unittest.TestCase):
    def test_excessive_range_is_rejected_before_materialization_or_browser_calls(self):
        from finpay_pipeline.cell_format.settings import expand_cell_range
        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day"
        ) as acquire:
            output = Path(temporary) / "must-not-exist"
            with self.assertRaisesRegex(ValueError, "bounded entry limit"):
                acquire_cell_range("2026-10-01", "9999-12-31", "ALL", "synthetic", str(output))
            acquire.assert_not_called()
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, "bounded entry limit"):
            expand_cell_range("2026-10-01", "9999-12-31", "ALL")

    def test_inclusive_range_retries_only_failed_cohort_and_builds_safe_index(self):
        counts = {}

        def acquire(day, cluster, _password, directory, **_kwargs):
            key = cluster, day
            counts[key] = counts.get(key, 0) + 1
            if day == "2026-10-02" and counts[key] == 1:
                raise RuntimeError("synthetic transient failure")
            return _daily_bundle(cluster, day, directory)

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", side_effect=acquire,
        ):
            root = Path(temporary)
            result = acquire_cell_range(
                "2026-10-01", "2026-10-02", "422140", "offline-secret",
                str(root / "out"), max_attempts=2,
            )
            self.assertEqual(counts, {
                ("422140", "2026-10-01"): 1,
                ("422140", "2026-10-02"): 2,
            })
            self.assertEqual((result["cohorts"], result["cells"], result["workers"]), (2, 4, 1))
            self.assertEqual(result["clusters"], ["422140"])
            with zipfile.ZipFile(result["source_bundle"]) as archive:
                self.assertEqual(archive.compression, zipfile.ZIP_STORED)
                index = json.loads(archive.read("index.json"))
                self.assertEqual(index["schema_version"], 1)
                self.assertEqual(index["roster_version"], ROSTER_VERSION)
                self.assertEqual(index["cohorts"], 2)
                self.assertEqual([entry["report_date"] for entry in index["entries"]], [
                    "2026-10-01", "2026-10-02",
                ])
                self.assertEqual(archive.namelist(), [
                    "index.json", "sources/422140/2026-10-01.zip",
                    "sources/422140/2026-10-02.zip",
                ])
                self.assertNotIn("password", archive.read("index.json").decode().lower())
            self.assertEqual(
                result["source_bundle_sha256"],
                hashlib.sha256(Path(result["source_bundle"]).read_bytes()).hexdigest(),
            )

    def test_all_clusters_run_concurrently_but_account_dates_stay_sequential(self):
        clusters = selected_clusters("ALL")
        start_gate = threading.Barrier(4)
        lock = threading.Lock()
        active, maximum = set(), 0
        last_day, calls = {}, []

        def acquire(day, cluster, _password, directory, **_kwargs):
            nonlocal maximum
            with lock:
                self.assertNotIn(cluster, active)
                active.add(cluster)
                maximum = max(maximum, len(active))
                previous = last_day.get(cluster)
                if previous:
                    self.assertLess(previous, day)
                last_day[cluster] = day
                calls.append((cluster, day))
            try:
                if day == "2026-10-01":
                    start_gate.wait(timeout=5)
                return _daily_bundle(cluster, day, directory)
            finally:
                with lock:
                    active.remove(cluster)

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", side_effect=acquire,
        ):
            result = acquire_cell_range(
                "2026-10-01", "2026-10-02", "ALL", "offline-secret",
                temporary, workers=4,
            )
        self.assertEqual(result["clusters"], sorted(clusters, key=int))
        self.assertEqual((result["cohorts"], result["workers"]), (len(clusters) * 2, 4))
        self.assertEqual(maximum, 4)
        self.assertEqual(len(calls), len(clusters) * 2)
        self.assertTrue(all(last_day[cluster] == "2026-10-02" for cluster in clusters))

    def test_validated_cache_reuse_and_corruption_reacquire_only_that_cohort(self):
        calls = 0

        def acquire(day, cluster, _password, directory, **_kwargs):
            nonlocal calls
            calls += 1
            return _daily_bundle(cluster, day, directory)

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", side_effect=acquire,
        ):
            root = Path(temporary)
            args = ("2026-10-01", "2026-10-01", "422140", "offline-secret", str(root / "out"))
            first = acquire_cell_range(*args)
            self.assertEqual(calls, 1)
            acquire_cell_range(*args)
            self.assertEqual(calls, 1)
            cached = root / "out/.cell_bulk_cache_v1/cohorts/422140/2026-10-01/cell_sources_v2.zip"
            cached.write_bytes(b"corrupt")
            second = acquire_cell_range(*args)
            self.assertEqual(calls, 2)
            for result in (first, second):
                with zipfile.ZipFile(result["source_bundle"]) as archive:
                    index = json.loads(archive.read("index.json"))
                self.assertEqual(index["cohorts"], 1)
                self.assertRegex(index["entries"][0]["sha256"], r"^[a-f0-9]{64}$")

    def test_cache_scope_and_reporting_dates_fail_before_acquisition(self):
        def acquire(day, cluster, _password, directory, **_kwargs):
            return _daily_bundle(cluster, day, directory)

        mocked_acquire = Mock(side_effect=acquire)
        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", mocked_acquire,
        ):
            root = Path(temporary)
            output, cache = root / "out", root / "cache"
            acquire_cell_range("2026-10-01", "2026-10-01", "422140", "x", str(output),
                               cache_dir=str(cache))
            for start, end in (
                ("2026-09-30", "2026-10-01"),
                ("2026-10-02", "2026-10-01"),
                ("2026-10-01", "2026-09-30"),
            ):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    acquire_cell_range(start, end, "422140", "x", str(output), cache_dir=str(cache))
            with self.assertRaisesRegex(ValueError, "different execution scope"):
                acquire_cell_range("2026-10-01", "2026-10-02", "422140", "x",
                                   str(output), cache_dir=str(cache))
            with self.assertRaisesRegex(ValueError, "workers"):
                acquire_cell_range("2026-10-01", "2026-10-01", "422140", "x",
                                   str(output), workers=5)
            self.assertEqual(mocked_acquire.call_count, 1)

    def test_failed_required_cohort_never_emits_final_bundle(self):
        calls = {}

        def acquire(day, cluster, _password, directory, **_kwargs):
            calls[day] = calls.get(day, 0) + 1
            if day == "2026-10-01":
                raise RuntimeError("synthetic source failure")
            return _daily_bundle(cluster, day, directory)

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", side_effect=acquire,
        ):
            output = Path(temporary) / "out"
            with self.assertRaisesRegex(RuntimeError, "422140/2026-10-01"):
                acquire_cell_range("2026-10-01", "2026-10-02", "422140", "x",
                                   str(output), max_attempts=2)
            self.assertEqual(calls, {"2026-10-01": 2, "2026-10-02": 1})
            self.assertFalse((output / "cell_bulk_sources_v1.zip").exists())

    def test_unsafe_daily_archive_member_is_rejected(self):
        def acquire(day, cluster, _password, directory, **_kwargs):
            result = _daily_bundle(cluster, day, directory)
            path = Path(result["source_bundle"])
            with zipfile.ZipFile(path) as source:
                manifest = source.read("manifest.json")
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", manifest)
                archive.writestr("../outside.xlsx", b"unsafe")
            result["source_bundle_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            return result

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day", side_effect=acquire,
        ):
            output = Path(temporary) / "out"
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                acquire_cell_range("2026-10-01", "2026-10-01", "422140", "x",
                                   str(output), max_attempts=1)
            self.assertFalse((output / "cell_bulk_sources_v1.zip").exists())

    def test_outer_bundle_limit_is_enforced_before_publication(self):
        def acquire(day, cluster, _password, directory, **_kwargs):
            return _daily_bundle(cluster, day, directory)

        with TemporaryDirectory() as temporary, patch(
            "finpay_pipeline.cell_format.bulk_acquisition.acquire_cell_day",
            side_effect=acquire,
        ), patch("finpay_pipeline.cell_format.bulk_acquisition.MAX_BULK_BUNDLE_BYTES", 512):
            output = Path(temporary) / "out"
            with self.assertRaisesRegex(ValueError, "size limit"):
                acquire_cell_range("2026-10-01", "2026-10-01", "422140", "x", str(output))
            self.assertFalse((output / "cell_bulk_sources_v1.zip").exists())


if __name__ == "__main__":
    unittest.main()
