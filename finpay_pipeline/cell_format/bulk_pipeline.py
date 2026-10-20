"""Bounded date-range cell processing over checksummed ZIP artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path, PurePosixPath

from ..cell_calculation import recalculate_stale_history
from ..cell_contracts import (
    CellIdentity, business_date, fingerprint, roster_fingerprint, sha256_file_bounded,
)
from ..cell_database import cell_publication_session, prepare_cell_day, validate_cluster_batch
from ..cell_publication_v2 import build_cell_cluster_matrices_v2
from ..cell_workflow import MAX_BUNDLE_BYTES, make_cell_sheet_client
from . import pipeline as daily
from .settings import (
    CELL_ROSTER, MAX_CELL_RUNS, REPORTING_START_DATE, ROSTER_VERSION, account_settings,
    database_target, selected_clusters, sheet_targets, validate_reporting_date,
)

MAX_BULK_BYTES = 1024 * 1024 * 1024
MAX_BULK_INDEX_BYTES = 16 * 1024 * 1024
MAX_BULK_HANDOFF_BYTES = len(CELL_ROSTER) * daily.MAX_HANDOFF_BYTES
MAX_BULK_ENTRIES = MAX_CELL_RUNS
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _range(start_date: str, end_date: str, cluster_id: str):
    start = validate_reporting_date(start_date)
    end = validate_reporting_date(end_date)
    first, last = business_date(start), business_date(end)
    if first > last:
        raise ValueError("start_date must be on or before end_date")
    clusters = list(selected_clusters(cluster_id))
    count = (last - first).days + 1
    if count * len(clusters) > MAX_BULK_ENTRIES:
        raise ValueError("cell bulk range exceeds the bounded entry limit")
    return start, end, clusters, count


def _dates(start_date: str, end_date: str):
    day, last = business_date(start_date), business_date(end_date)
    while day <= last:
        yield day.isoformat()
        day += timedelta(days=1)


def _daily_source_path(cluster_id: str, report_date: str) -> str:
    return f"sources/{cluster_id}/{report_date}.zip"


def _daily_prepared_path(cluster_id: str, report_date: str) -> str:
    return f"prepared/{cluster_id}/{report_date}.json"


def _handoff_path(cluster_id: str) -> str:
    return f"handoffs/{cluster_id}.json"


def _require_sha256(value, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError("cell bulk ZIP member exceeds size limit")
    chunks, total = [], 0
    with archive.open(info) as source:
        while chunk := source.read(min(1024 * 1024, limit - total + 1)):
            total += len(chunk)
            if total > limit:
                raise ValueError("cell bulk ZIP member exceeds size limit")
            chunks.append(chunk)
    if total != info.file_size:
        raise ValueError("cell bulk ZIP member size mismatch")
    return b"".join(chunks)


def _copy_member(archive: zipfile.ZipFile, name: str, destination: Path,
                 expected_sha256: str, limit: int) -> tuple[str, int]:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError("cell bulk ZIP member exceeds size limit")
    digest, total = hashlib.sha256(), 0
    with archive.open(info) as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("cell bulk ZIP member exceeds size limit")
            digest.update(chunk)
            target.write(chunk)
    actual = digest.hexdigest()
    if total != info.file_size or actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("cell bulk ZIP member size/checksum mismatch")
    return actual, total


def _parse_index(raw: bytes) -> dict:
    value = json.loads(raw, object_pairs_hook=daily._unique_json)
    if not isinstance(value, dict):
        raise ValueError("cell bulk index must be a JSON object")
    return value


def _validate_index(index: dict, kind: str):
    if type(index.get("schema_version")) is not int or index["schema_version"] != 1:
        raise ValueError("unsupported cell bulk index schema")
    selector = index.get("cluster_id")
    if kind == "source" and selector is None:
        indexed_clusters = index.get("clusters")
        if indexed_clusters == list(selected_clusters("ALL")):
            selector = "ALL"
        elif isinstance(indexed_clusters, list) and len(indexed_clusters) == 1:
            selector = indexed_clusters[0]
        else:
            raise ValueError("source bundle cluster roster is invalid")
    start, end, clusters, days = _range(
        index.get("start_date", ""), index.get("end_date", ""),
        selector or "",
    )
    if index.get("roster_version") != ROSTER_VERSION or index.get("clusters") != clusters:
        raise ValueError("cell bulk index roster identity mismatch")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError("cell bulk index entries must be a list")

    by_key = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("cell bulk index contains a malformed entry")
        cluster = entry.get("cluster_id")
        if cluster not in clusters:
            raise ValueError("cell bulk index contains an unselected cluster")
        if kind == "handoff":
            key = cluster
            expected_path = _handoff_path(cluster)
            if entry.get("path") != expected_path:
                raise ValueError("cell bulk handoff path differs from its cluster")
            if type(entry.get("bytes")) is not int or not 0 < entry["bytes"] <= daily.MAX_HANDOFF_BYTES:
                raise ValueError("cell bulk handoff size is invalid")
        else:
            report_date = entry.get("report_date")
            report_date = validate_reporting_date(report_date)
            if not start <= report_date <= end:
                raise ValueError("cell bulk index date is outside its requested range")
            key = (cluster, report_date)
            expected_path = (_daily_source_path(cluster, report_date) if kind == "source"
                             else _daily_prepared_path(cluster, report_date))
            if entry.get("path") != expected_path:
                raise ValueError("cell bulk member path differs from its cluster/date")
            _, codes = account_settings(cluster)
            if type(entry.get("cells")) is not int or entry["cells"] != len(codes):
                raise ValueError("cell bulk member has incomplete roster coverage")
            if kind == "prepared" and (
                type(entry.get("bytes")) is not int
                or not 0 < entry["bytes"] <= daily.MAX_PREPARED_BYTES
            ):
                raise ValueError("prepared cell member size is invalid")
        _require_sha256(entry.get("sha256"), "cell bulk member checksum")
        if key in by_key:
            raise ValueError("duplicate cell bulk index entry")
        by_key[key] = entry

    if kind == "handoff":
        if set(by_key) != set(clusters):
            raise ValueError("cell bulk handoff roster coverage is incomplete")
    else:
        expected = {(cluster, day) for cluster in clusters for day in _dates(start, end)}
        if set(by_key) != expected:
            raise ValueError("cell bulk range must cover every selected cluster/date")
    return start, end, clusters, by_key


@contextmanager
def _open_bulk_zip(path, expected_sha256: str, kind: str):
    limit = MAX_BULK_HANDOFF_BYTES if kind == "handoff" else MAX_BULK_BYTES
    expected_sha256 = _require_sha256(expected_sha256, "cell bulk artifact checksum")
    if sha256_file_bounded(path, limit) != expected_sha256:
        raise ValueError("cell bulk artifact checksum mismatch")
    archive = zipfile.ZipFile(path, "r")
    try:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (len(names) != len(set(names)) or len(names) > MAX_BULK_ENTRIES + 1
                or any(info.is_dir() for info in infos)):
            raise ValueError("duplicate, excessive, or directory cell bulk ZIP entries")
        if any("\\" in name or name.startswith("/") or ".." in PurePosixPath(name).parts
               for name in names):
            raise ValueError("cell bulk ZIP contains an unsafe path")
        if "index.json" not in names:
            raise ValueError("cell bulk ZIP is missing its root index")
        index_info = archive.getinfo("index.json")
        if index_info.file_size > MAX_BULK_INDEX_BYTES:
            raise ValueError("cell bulk index exceeds size limit")
        index = _parse_index(_read_member(archive, "index.json", MAX_BULK_INDEX_BYTES))
        start, end, clusters, entries = _validate_index(index, kind)
        expected_names = {"index.json"} | {entry["path"] for entry in entries.values()}
        if set(names) != expected_names:
            raise ValueError("cell bulk ZIP has missing or unexpected members")
        total_limit = MAX_BULK_HANDOFF_BYTES if kind == "handoff" else MAX_BULK_BYTES
        if sum(info.file_size for info in infos) > total_limit:
            raise ValueError("cell bulk ZIP uncompressed size exceeds limit")
        member_limit = daily.MAX_HANDOFF_BYTES if kind == "handoff" else (
            MAX_BUNDLE_BYTES if kind == "source" else daily.MAX_PREPARED_BYTES
        )
        if any(info.file_size > member_limit for info in infos if info.filename != "index.json"):
            raise ValueError("cell bulk ZIP member exceeds size limit")
        if kind == "handoff" and any(
            archive.getinfo(entry["path"]).file_size != entry["bytes"]
            for entry in entries.values()
        ):
            raise ValueError("cell bulk handoff size differs from index")
        if kind == "prepared" and any(
            archive.getinfo(entry["path"]).file_size != entry["bytes"]
            for entry in entries.values()
        ):
            raise ValueError("prepared cell member size differs from index")
        yield archive, index, start, end, clusters, entries
    finally:
        archive.close()


def _stage_member(archive, entry, root: Path, limit: int) -> Path:
    destination = root / "member.bin"
    _copy_member(archive, entry["path"], destination, entry["sha256"], limit)
    return destination


def _atomic_zip(path: str | Path, writer, size_limit: int):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name("." + destination.name + ".pending")
    try:
        writer(pending)
        size = pending.stat().st_size
        if size > size_limit:
            raise ValueError("cell bulk artifact exceeds size limit")
        digest = sha256_file_bounded(pending, size_limit)
        os.replace(pending, destination)
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
    return digest, size


def preprocess_cell_range(source_bundle: str, source_bundle_sha256: str, start_date: str,
                          end_date: str, cluster_id: str,
                          output_path: str = "cell_bulk_prepared_v1.zip") -> dict:
    """Validate and calculate every source date/cluster offline before persistence."""
    start, end, requested_clusters, _ = _range(start_date, end_date, cluster_id)
    prepared_entries = []
    with tempfile.TemporaryDirectory(prefix="finpay-cell-bulk-preprocess-") as scratch:
        scratch_dir = Path(scratch)
        with _open_bulk_zip(source_bundle, source_bundle_sha256, "source") as (
            source_zip, _, bundle_start, bundle_end, clusters, source_entries,
        ):
            if (start, end, requested_clusters) != (bundle_start, bundle_end, clusters):
                raise ValueError("source bundle range differs from task inputs")
            prepared_total = 0

            def write_archive(pending):
                nonlocal prepared_total
                with zipfile.ZipFile(
                    pending, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                ) as output:
                    with tempfile.TemporaryDirectory(dir=scratch_dir) as members:
                        member_dir = Path(members)
                        for cluster in clusters:
                            for day in _dates(start, end):
                                source_entry = source_entries[(cluster, day)]
                                source_path = _stage_member(
                                    source_zip, source_entry, member_dir, MAX_BUNDLE_BYTES,
                                )
                                member_path = member_dir / "cell_prepared_v1.json"
                                result = daily.preprocess_cell_day(
                                    str(source_path), source_entry["sha256"], day, cluster,
                                    str(member_path),
                                )
                                digest = _require_sha256(
                                    result["prepared_sha256"], "prepared checksum",
                                )
                                size = member_path.stat().st_size
                                if size > daily.MAX_PREPARED_BYTES:
                                    raise ValueError("prepared cell date exceeds size limit")
                                prepared_total += size
                                if prepared_total > MAX_BULK_BYTES:
                                    raise ValueError("cell bulk prepared data exceeds size limit")
                                item = {
                                    "cluster_id": cluster, "report_date": day,
                                    "path": _daily_prepared_path(cluster, day),
                                    "sha256": digest, "cells": source_entry["cells"],
                                    "bytes": size,
                                }
                                output.write(member_path, arcname=item["path"])
                                prepared_entries.append(item)
                                source_path.unlink(missing_ok=True)
                                member_path.unlink(missing_ok=True)
                    index = {
                        "schema_version": 1, "start_date": start, "end_date": end,
                        "cluster_id": cluster_id, "clusters": requested_clusters,
                        "roster_version": ROSTER_VERSION, "entries": prepared_entries,
                    }
                    index_bytes = json.dumps(
                        index, sort_keys=True, separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")
                    if len(index_bytes) > MAX_BULK_INDEX_BYTES or prepared_total + len(index_bytes) > MAX_BULK_BYTES:
                        raise ValueError("cell bulk prepared artifact exceeds size limit")
                    output.writestr("index.json", index_bytes)

            digest, size = _atomic_zip(output_path, write_archive, MAX_BULK_BYTES)
    return {
        "status": "PREPROCESSED", "start_date": start, "end_date": end,
        "cluster_id": cluster_id, "clusters": requested_clusters,
        "prepared_bundle_sha256": digest, "prepared_bundle_bytes": size,
        "prepared_entries": len(prepared_entries),
    }


def _load_bulk_prepared_member(archive, entry, root: Path):
    path = _stage_member(archive, entry, root, daily.MAX_PREPARED_BYTES)
    if path.stat().st_size != entry["bytes"]:
        raise ValueError("prepared cell member size differs from index")
    prepared, provisional, batch = daily._load_prepared(
        path, entry["sha256"], entry["cluster_id"], entry["report_date"],
    )
    if len(batch) != entry["cells"]:
        raise ValueError("prepared cell member differs from indexed roster coverage")
    return path, prepared, provisional, batch


def _rebind_batch(provisional, batch, config):
    old_config, new_config = provisional.to_dict(), config.to_dict()
    for key in ("first_report_date", "opening_boundary"):
        old_config.pop(key, None)
        new_config.pop(key, None)
    if old_config != new_config:
        raise ValueError("prepared cell calculations cannot be rebound to the active config")
    rebound = [prepare_cell_day(
        CellIdentity(**item["identity"]), item["manifest"], item["raw_rows"],
        item["calculation"], config.to_dict(), config.rules_version,
    ) for item in batch]
    validate_cluster_batch(rebound, config.cells)
    return rebound


def _preflight_prepared(archive, entries, scratch_dir: Path):
    for cluster, day in sorted(entries, key=lambda key: (int(key[0]), key[1])):
        entry = entries[(cluster, day)]
        path, _, _, _ = _load_bulk_prepared_member(archive, entry, scratch_dir)
        path.unlink(missing_ok=True)


def _source_facts_for_cluster(archive, entries, cluster: str, days, config, scratch_dir: Path):
    facts = []
    for day in days:
        entry = entries[(cluster, day)]
        path, _, provisional, batch = _load_bulk_prepared_member(archive, entry, scratch_dir)
        rebound = _rebind_batch(provisional, batch, config)
        facts.extend(_facts_from_batch(config, rebound))
        path.unlink(missing_ok=True)
    return facts


def _facts_from_batch(config, batch):
    return [{
        "identity": item["identity"], "report_date": item["report_date"],
        "generation_id": item["generation_id"],
        "calculation_id": item["calculation_id"], "config_id": item["config_id"],
        "rules_version": config.rules_version, "calculation": item["calculation"],
    } for item in batch]


def _read_handoff_member(archive, entry, scratch_dir: Path):
    path = _stage_member(archive, entry, scratch_dir, daily.MAX_HANDOFF_BYTES)
    handoff = daily._read_json(path, entry["sha256"], daily.MAX_HANDOFF_BYTES)
    return path, handoff


def _validate_complete_handoff(handoff, cluster: str, end_date: str):
    actual_cluster, config = daily._validate_handoff(handoff)
    if actual_cluster != cluster or handoff["submitted_report_date"] != end_date:
        raise ValueError("cell bulk handoff differs from requested cluster/end date")
    first, last = handoff["first_report_date"], handoff["last_report_date"]
    cells = {cell.cell_code for cell in config.cells}
    expected_count = ((business_date(last) - business_date(first)).days + 1) * len(cells)
    if expected_count > 100_000 or len(handoff["selected"]) != expected_count:
        raise ValueError("cell bulk handoff does not cover complete persisted history")
    expected = {
        (day, code) for day in _dates(first, last) for code in cells
    }
    actual = {(item["report_date"], item["cell_code"]) for item in handoff["selected"]}
    if actual != expected:
        raise ValueError("cell bulk handoff has missing or duplicate history coverage")
    return config


def _write_handoff_bundle(output_path, start, end, cluster_id, clusters, handoff_paths):
    entries = []
    for cluster in clusters:
        path = handoff_paths[cluster]
        size = path.stat().st_size
        digest = sha256_file_bounded(path, daily.MAX_HANDOFF_BYTES)
        entries.append({
            "cluster_id": cluster, "path": _handoff_path(cluster),
            "sha256": digest, "bytes": size,
        })
    index = {
        "schema_version": 1, "start_date": start, "end_date": end,
        "cluster_id": cluster_id, "clusters": clusters,
        "roster_version": ROSTER_VERSION, "entries": entries,
    }
    index_bytes = json.dumps(index, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    total = len(index_bytes) + sum(item["bytes"] for item in entries)
    if len(index_bytes) > MAX_BULK_INDEX_BYTES or total > MAX_BULK_HANDOFF_BYTES:
        raise ValueError("cell bulk handoff artifact exceeds size limit")

    def write_archive(pending):
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as output:
            output.writestr("index.json", index_bytes)
            for cluster in clusters:
                output.write(handoff_paths[cluster], arcname=_handoff_path(cluster))

    return _atomic_zip(output_path, write_archive, MAX_BULK_HANDOFF_BYTES)


def validate_cell_bulk_sheet_range(start_date: str, end_date: str, cluster_id: str) -> dict:
    """Validate every selected cluster's distinct code-owned worksheet targets."""
    start, end, clusters, days = _range(start_date, end_date, cluster_id)
    accounts, targets_by_account = {}, {}
    workbook_id = legacy_workbook_id = None
    for cluster in clusters:
        config = daily._cell_config(cluster, start)
        workbook, legacy, targets = sheet_targets(cluster)
        if workbook_id is not None and (workbook_id != workbook or legacy_workbook_id != legacy):
            raise ValueError("cell-format clusters must share the same new and legacy workbook IDs")
        workbook_id, legacy_workbook_id = workbook, legacy
        accounts[config.account] = config
        targets_by_account[config.account] = targets
    daily.validate_output_targets(
        workbook_id, legacy_workbook_id, accounts, targets_by_account,
    )
    return {"clusters": clusters, "dates": days, "runs": days * len(clusters)}


def persist_cell_range(prepared_path: str, prepared_sha256: str, *,
                       database_password: str | None,
                       output_path: str = "cell_bulk_handoffs_v1.zip",
                       dry_run: bool = False, target=None,
                       session_factory=cell_publication_session) -> dict:
    """Commit one cluster at a time; each cluster's date cohorts share one transaction."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be an explicit boolean")
    with tempfile.TemporaryDirectory(prefix="finpay-cell-bulk-persist-") as scratch:
        scratch_dir = Path(scratch)
        with _open_bulk_zip(prepared_path, prepared_sha256, "prepared") as (
            archive, index, start, end, clusters, entries,
        ):
            days = list(_dates(start, end))
            _preflight_prepared(archive, entries, scratch_dir)
            if dry_run:
                return {
                    "status": "DRY_RUN", "start_date": start, "end_date": end,
                    "cluster_id": index["cluster_id"], "clusters": clusters,
                    "prepared_entries": len(entries),
                }
            if not database_password:
                raise ValueError("FINPAY_DB_PASSWORD Kestra secret is required")
            target = database_target() if target is None else target
            handoff_paths = {}
            for cluster in clusters:
                config = daily._cell_config(cluster, REPORTING_START_DATE)
                with session_factory(
                    target, {target}, cluster, "", password=database_password,
                    approved_sheet_ids=set(), write_gsheet=False,
                ) as store:
                    earliest = store.earliest_report_date()
                    if earliest and earliest < REPORTING_START_DATE:
                        raise ValueError(
                            f"persisted cell history predates the code-owned reporting start {REPORTING_START_DATE}"
                        )
                    through = max(end, store.latest_report_date() or end)
                    submitted_dates = set(days)
                    history_batches = recalculate_stale_history(
                        store, config, through, submitted_dates=submitted_dates,
                    )
                    keyed = {
                        (fact["report_date"], fact["identity"]["cell_code"]): fact
                        for fact in daily._facts(
                            config, store.current_results(REPORTING_START_DATE, through),
                        )
                    }
                    for cohort in history_batches:
                        for fact in _facts_from_batch(config, cohort):
                            keyed[(fact["report_date"], fact["identity"]["cell_code"])] = fact
                    for fact in _source_facts_for_cluster(
                        archive, entries, cluster, days, config, scratch_dir,
                    ):
                        keyed[(fact["report_date"], fact["identity"]["cell_code"])] = fact
                    build_cell_cluster_matrices_v2(
                        config, list(keyed.values()), through, dummy=True,
                    )

                    stale_by_day = {batch[0]["report_date"]: batch for batch in history_batches}
                    handoff = None
                    with store.connection.transaction():
                        for day in sorted(set(stale_by_day) | submitted_dates):
                            if day in stale_by_day:
                                batch = stale_by_day[day]
                            else:
                                entry = entries[(cluster, day)]
                                path, _, provisional, provisional_batch = _load_bulk_prepared_member(
                                    archive, entry, scratch_dir,
                                )
                                batch = _rebind_batch(provisional, provisional_batch, config)
                                path.unlink(missing_ok=True)
                            store.commit_cluster_day(batch, config.cells)
                        committed_through = store.latest_report_date()
                        committed = daily._current_history(store, config)
                        build_cell_cluster_matrices_v2(
                            config, committed, committed_through, dummy=True,
                        )
                        selected = daily._selection(committed)
                        handoff = {
                            "schema_version": daily.HANDOFF_SCHEMA_VERSION,
                            "cluster_id": cluster, "account": config.account,
                            "submitted_report_date": end,
                            "first_report_date": REPORTING_START_DATE,
                            "last_report_date": committed_through,
                            "roster_fingerprint": roster_fingerprint(config.cells),
                            "selected": selected,
                            "history_fingerprint": fingerprint(selected),
                        }
                        _validate_complete_handoff(handoff, cluster, end)
                        handoff_path = scratch_dir / f"handoff-{cluster}.json"
                        daily._write_json(handoff_path, handoff, daily.MAX_HANDOFF_BYTES)
                    handoff_paths[cluster] = handoff_path
            digest, size = _write_handoff_bundle(
                output_path, start, end, index["cluster_id"], clusters, handoff_paths,
            )
    return {
        "status": "PERSISTED", "start_date": start, "end_date": end,
        "cluster_id": index["cluster_id"], "clusters": clusters,
        "handoff_bundle_sha256": digest, "handoff_bundle_bytes": size,
        "committed_clusters": len(clusters),
    }


def _validate_publication_targets(clusters, start, end, cluster_id, credentials_info):
    validate_cell_bulk_sheet_range(start, end, cluster_id)
    if not isinstance(credentials_info, dict) or not credentials_info.get("client_email"):
        raise ValueError("Google service-account credentials are required")
    for cluster in clusters:
        _, _, targets = sheet_targets(cluster)
        writer = next(iter(targets.values())).writer_email
        if credentials_info["client_email"] != writer:
            raise ValueError("Google credential identity differs from the code-owned cell writer")


def publish_cell_range(handoff_path: str, handoff_sha256: str, *,
                       database_password: str | None, credentials_info: dict | None,
                       dry_run: bool = False, target=None,
                       session_factory=cell_publication_session,
                       sheet_client_factory=make_cell_sheet_client) -> dict:
    """Publish once per cluster, serially, through the daily guarded publisher."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be an explicit boolean")
    results = []
    with tempfile.TemporaryDirectory(prefix="finpay-cell-bulk-publish-") as scratch:
        scratch_dir = Path(scratch)
        with _open_bulk_zip(handoff_path, handoff_sha256, "handoff") as (
            archive, index, start, end, clusters, entries,
        ):
            for cluster in clusters:
                entry = entries[cluster]
                path, handoff = _read_handoff_member(archive, entry, scratch_dir)
                _validate_complete_handoff(handoff, cluster, end)
                path.unlink(missing_ok=True)
            if dry_run:
                return {
                    "status": "DRY_RUN", "start_date": start, "end_date": end,
                    "cluster_id": index["cluster_id"], "clusters": clusters,
                    "publications": len(clusters),
                }
            if not database_password or not credentials_info:
                raise ValueError("FINPAY_DB_PASSWORD and GCP_SA_KEY Kestra secrets are required")
            _validate_publication_targets(
                clusters, start, end, index["cluster_id"], credentials_info,
            )
            target = database_target() if target is None else target
            for cluster in clusters:
                entry = entries[cluster]
                path, _ = _read_handoff_member(archive, entry, scratch_dir)
                result = daily.publish_cell_sheets(
                    str(path), entry["sha256"], database_password=database_password,
                    credentials_info=credentials_info, cluster_id=cluster, target=target,
                    session_factory=session_factory,
                    sheet_client_factory=sheet_client_factory,
                )
                results.append(result)
                path.unlink(missing_ok=True)
    return {
        "status": "COMPLETE", "start_date": start, "end_date": end,
        "cluster_id": index["cluster_id"], "clusters": clusters,
        "publications": results,
    }
