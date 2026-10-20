"""Bounded, account-parallel acquisition of validated daily cell bundles."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from ..cell_contracts import (
    MAX_CELL_ARTIFACT_BYTES, configured_cells, sha256_file_bounded,
    validate_complete_manifest,
)
from ..cell_workflow import MAX_BUNDLE_BYTES
from .acquisition import acquire_cell_day
from .settings import (
    MAX_CELL_RUNS, ROSTER_VERSION, account_settings, selected_clusters, validate_reporting_date,
)

MAX_BULK_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
_CACHE_SCHEMA_VERSION = 1


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in cell bulk metadata")
        result[key] = value
    return result


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("cell bulk cache directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_json(path: Path, value: dict, limit: int) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > limit:
        raise ValueError("cell bulk metadata exceeds size limit")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path, limit: int) -> dict:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("cell bulk metadata is unsafe or excessive")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("cell bulk metadata grew beyond size limit")
    value = json.loads(data, object_pairs_hook=_unique_json)
    if not isinstance(value, dict):
        raise ValueError("cell bulk metadata must be an object")
    return value


def _validate_daily_bundle(path: Path, cluster_id: str, report_date: str,
                           expected_sha256: str | None = None) -> tuple[str, int]:
    if path.is_symlink():
        raise ValueError("daily cell bundle cannot be a symlink")
    digest = sha256_file_bounded(path, MAX_BUNDLE_BYTES)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("daily cell bundle checksum mismatch")
    account, codes = account_settings(cluster_id)
    cells = configured_cells({account: codes})
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (not 1 <= len(infos) <= 100 or len(names) != len(set(names))
                or "manifest.json" not in names
                or any(name != "manifest.json" and not re.fullmatch(
                    r"\d{6}_B-\d{6}\.xlsx", name) for name in names)):
            raise ValueError("daily cell bundle entries are unsafe or unexpected")
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            total += info.file_size
            if (info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1
                    or mode not in (0, stat.S_IFREG) or total > MAX_BUNDLE_BYTES):
                raise ValueError("daily cell bundle entry is unsafe or excessive")
        if archive.getinfo("manifest.json").file_size > 4 * 1024 * 1024:
            raise ValueError("daily cell manifest exceeds size limit")
        manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=_unique_json)
        validate_complete_manifest(manifest, cells)
        if manifest.get("report_date") != report_date:
            raise ValueError("daily cell bundle date mismatch")
        wanted = {"manifest.json"} | {
            entry["artifact"] for entry in manifest["cells"] if entry["artifact"]
        }
        if set(names) != wanted:
            raise ValueError("daily cell bundle has missing or unexpected artifacts")
        for entry in manifest["cells"]:
            if not entry["artifact"]:
                continue
            info = archive.getinfo(entry["artifact"])
            if info.file_size > MAX_CELL_ARTIFACT_BYTES:
                raise ValueError("cell XLSX exceeds artifact size limit")
            source_hash = hashlib.sha256()
            with archive.open(info) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    source_hash.update(chunk)
            if source_hash.hexdigest() != entry["source_sha256"]:
                raise ValueError("cell XLSX checksum mismatch")
    return digest, len(manifest["cells"])


def _cache_scope(cache_root: Path, output_dir: Path, start: str, end: str,
                 clusters: tuple[str, ...]) -> None:
    scope = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "code_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "execution_dir": str(output_dir.resolve()),
        "start_date": start,
        "end_date": end,
        "clusters": list(clusters),
        "roster_version": ROSTER_VERSION,
    }
    marker = cache_root / "scope.json"
    if marker.exists():
        if _read_json(marker, 16 * 1024) != scope:
            raise ValueError("cell bulk cache belongs to a different execution scope")
    elif any(cache_root.iterdir()):
        raise ValueError("cell bulk cache is missing its execution scope")
    else:
        _write_json(marker, scope, 16 * 1024)


def _cached_cohort(directory: Path, cluster_id: str, day: str) -> dict | None:
    bundle, receipt = directory / "cell_sources_v2.zip", directory / "receipt.json"
    if not receipt.exists() or not bundle.exists():
        return None
    try:
        data = _read_json(receipt, 16 * 1024)
        expected = {
            "schema_version": _CACHE_SCHEMA_VERSION, "cluster_id": cluster_id,
            "report_date": day, "roster_version": ROSTER_VERSION,
        }
        if any(data.get(key) != value for key, value in expected.items()):
            return None
        digest, cells = _validate_daily_bundle(bundle, cluster_id, day, data.get("sha256"))
        if type(data.get("cells")) is not int or data["cells"] != cells:
            return None
        return {"cluster_id": cluster_id, "report_date": day,
                "path": f"sources/{cluster_id}/{day}.zip", "sha256": digest,
                "cells": cells, "_source": bundle}
    except Exception:
        return None


def _acquire_cohort(cluster_id: str, day: str, password: str, directory: Path,
                    max_attempts: int, googleapis_compatibility: bool):
    _private_directory(directory)
    cached = _cached_cohort(directory, cluster_id, day)
    if cached is not None:
        return cached, None
    bundle = directory / "cell_sources_v2.zip"
    error = "AcquisitionError"
    for _ in range(max_attempts):
        try:
            result = acquire_cell_day(
                day, cluster_id, password, str(directory), max_attempts=1,
                googleapis_compatibility=googleapis_compatibility,
            )
            if (result.get("cluster_id") != cluster_id or result.get("report_date") != day
                    or Path(result.get("source_bundle", "")).resolve() != bundle.resolve()):
                raise ValueError("daily cell acquisition returned a different identity")
            digest, cells = _validate_daily_bundle(
                bundle, cluster_id, day, result.get("source_bundle_sha256"),
            )
            receipt = {
                "schema_version": _CACHE_SCHEMA_VERSION, "cluster_id": cluster_id,
                "report_date": day, "roster_version": ROSTER_VERSION,
                "sha256": digest, "cells": cells,
            }
            _write_json(directory / "receipt.json", receipt, 16 * 1024)
            return {"cluster_id": cluster_id, "report_date": day,
                    "path": f"sources/{cluster_id}/{day}.zip", "sha256": digest,
                    "cells": cells, "_source": bundle}, None
        except Exception as exc:
            error = type(exc).__name__
    return None, error


def _acquire_account(cluster_id: str, start: date, end: date, password: str,
                     cache_root: Path, max_attempts: int, googleapis_compatibility: bool):
    entries, failures = [], []
    day = start
    while day <= end:
        text = day.isoformat()
        directory = cache_root / "cohorts" / cluster_id / text
        entry, error = _acquire_cohort(
            cluster_id, text, password, directory, max_attempts, googleapis_compatibility,
        )
        if entry is None:
            failures.append(f"{cluster_id}/{text} ({error})")
        else:
            entries.append(entry)
        day += timedelta(days=1)
    return entries, failures


def _write_member(archive: zipfile.ZipFile, source_path: Path, name: str,
                  expected_sha256: str, current_size: int) -> int:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    overhead = 256 + len(name.encode("utf-8")) * 2
    if current_size + source_path.stat().st_size + overhead > MAX_BULK_BUNDLE_BYTES:
        raise ValueError("cell bulk source bundle exceeds size limit")
    digest, copied = hashlib.sha256(), 0
    with source_path.open("rb") as source, archive.open(info, "w") as destination:
        while chunk := source.read(1024 * 1024):
            copied += len(chunk)
            if current_size + copied + overhead > MAX_BULK_BUNDLE_BYTES:
                raise ValueError("cell bulk source bundle exceeds size limit")
            digest.update(chunk)
            destination.write(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("daily cell bundle changed during bulk assembly")
    return current_size + copied + overhead


def acquire_cell_range(start_date: str, end_date: str, cluster_id: str,
                       password: str, output_dir: str, *, workers: int = 2,
                       max_attempts: int = 3, googleapis_compatibility: bool = False,
                       cache_dir: str | None = None) -> dict:
    """Acquire a complete inclusive range; account dates remain sequential.

    `cache_dir`, when supplied, must be unique to this execution. Cached bundles
    are bound to the output directory, roster, and acquisition source fingerprint.
    """
    start_text, end_text = validate_reporting_date(start_date), validate_reporting_date(end_date)
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    clusters = tuple(sorted(selected_clusters(cluster_id), key=int))
    if ((end - start).days + 1) * len(clusters) > MAX_CELL_RUNS:
        raise ValueError("cell bulk range exceeds the bounded entry limit")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("DIGIPOS_PASSWORD is required")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if type(googleapis_compatibility) is not bool:
        raise ValueError("googleapis_compatibility must be a boolean")

    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cache_root = Path(cache_dir) if cache_dir is not None else output / ".cell_bulk_cache_v1"
    _private_directory(cache_root)
    _cache_scope(cache_root, output, start_text, end_text, clusters)
    active_workers = min(workers, len(clusters))
    results, failures = [], []
    with ThreadPoolExecutor(max_workers=active_workers, thread_name_prefix="cell-account") as pool:
        futures = {
            pool.submit(_acquire_account, cluster, start, end, password, cache_root,
                        max_attempts, googleapis_compatibility): cluster
            for cluster in clusters
        }
        for future in as_completed(futures):
            try:
                entries, failed = future.result()
                results.extend(entries)
                failures.extend(failed)
            except Exception as exc:
                failures.append(f"{futures[future]} (worker {type(exc).__name__})")
    if failures:
        raise RuntimeError("cell bulk acquisition incomplete: " + ", ".join(sorted(failures))) from None

    results.sort(key=lambda item: (int(item["cluster_id"]), item["report_date"]))
    cohorts, cells = len(results), sum(item["cells"] for item in results)
    elapsed = round(time.perf_counter() - started, 3)
    index = {
        "schema_version": 1, "start_date": start_text, "end_date": end_text,
        "clusters": list(clusters), "roster_version": ROSTER_VERSION,
        "cohorts": cohorts, "cells": cells, "elapsed_seconds": elapsed,
        "entries": [{key: item[key] for key in (
            "cluster_id", "report_date", "path", "sha256", "cells",
        )} for item in results],
    }
    index_bytes = json.dumps(index, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(index_bytes) > MAX_INDEX_BYTES:
        raise ValueError("cell bulk index exceeds size limit")
    destination = output / "cell_bulk_sources_v1.zip"
    fd, temporary = tempfile.mkstemp(prefix=".cell_bulk_sources_v1-", suffix=".pending", dir=output)
    try:
        with os.fdopen(fd, "w+b") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as archive:
                info = zipfile.ZipInfo("index.json")
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, index_bytes)
                total = len(index_bytes) + 256
                for item in results:
                    total = _write_member(archive, item["_source"], item["path"], item["sha256"], total)
            raw.flush()
            os.fsync(raw.fileno())
            if raw.tell() > MAX_BULK_BUNDLE_BYTES:
                raise ValueError("cell bulk source bundle exceeds size limit")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    os.chmod(destination, 0o600)
    digest = sha256_file_bounded(destination, MAX_BULK_BUNDLE_BYTES)
    elapsed = round(time.perf_counter() - started, 3)
    return {
        "source_bundle": str(destination), "source_bundle_sha256": digest,
        "start_date": start_text, "end_date": end_text,
        "clusters": list(clusters), "cohorts": cohorts, "cells": cells,
        "workers": active_workers, "elapsed_seconds": elapsed,
    }
