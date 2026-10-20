"""Previous-format DigiPOS range acquisition, separate from cell-format rules."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from ..digipos_scraper import (
    CLUSTERS, _download_cluster, expected_source_filename, parse_report_date,
)


MAX_LEGACY_ARTIFACT_BYTES = 128 * 1024 * 1024


def expand_legacy_range(start_date: str, end_date: str, cluster_id: str) -> list[dict[str, str]]:
    start, end = parse_report_date(start_date), parse_report_date(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if cluster_id == "ALL":
        clusters = CLUSTERS
    elif cluster_id in CLUSTERS:
        clusters = (cluster_id,)
    else:
        raise ValueError("cluster_id must be a configured legacy cluster or ALL")
    runs = []
    day = start
    while day <= end:
        runs.extend({"report_date": day.isoformat(), "cluster_id": cluster} for cluster in clusters)
        day += timedelta(days=1)
    return runs


def acquire_legacy_day(report_date: str, cluster_id: str, password: str, output_dir: str,
                       *, max_attempts: int = 5) -> dict:
    """Acquire and validate exactly one previous-format cluster/day export."""
    day = parse_report_date(report_date)
    if cluster_id not in CLUSTERS:
        raise ValueError("cluster_id is not configured for previous-format acquisition")
    if not password:
        raise ValueError("DIGIPOS_PASSWORD Kestra secret is required")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result = _download_cluster(
        cluster_id, day, password, directory, max_attempts,
        max_file_bytes=MAX_LEGACY_ARTIFACT_BYTES,
    )
    source = directory / result["artifact"]
    filename = expected_source_filename(cluster_id, day.isoformat())
    destination = directory / "source.xlsx"
    if source.stat().st_size > MAX_LEGACY_ARTIFACT_BYTES:
        source.unlink(missing_ok=True)
        raise ValueError("acquired legacy artifact exceeds size limit")
    os.replace(source, destination)
    if destination.stat().st_size > MAX_LEGACY_ARTIFACT_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError("acquired legacy artifact exceeds size limit")
    digest = result["sha256"]
    manifest = {
        "report_date": day.isoformat(), "cluster_id": cluster_id,
        "filename": filename, "row_count": result["row_count"], "sha256": digest,
    }
    (directory / "legacy_acquisition_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8",
    )
    return {**manifest, "artifact_path": str(destination)}


def verify_legacy_artifact(artifact_path: str, manifest_path: str, expected_sha256: str,
                           report_date: str, cluster_id: str) -> dict:
    """Verify the Kestra-stored workbook against its bounded acquisition manifest."""
    day = parse_report_date(report_date)
    if cluster_id not in CLUSTERS:
        raise ValueError("cluster_id is not configured for previous-format acquisition")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256 or ""):
        raise ValueError("legacy artifact checksum must be a SHA-256 digest")
    artifact, manifest_file = Path(artifact_path), Path(manifest_path)
    if artifact.stat().st_size > MAX_LEGACY_ARTIFACT_BYTES:
        raise ValueError("legacy artifact exceeds size limit")
    if manifest_file.stat().st_size > 64 * 1024:
        raise ValueError("legacy acquisition manifest exceeds size limit")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    expected_filename = expected_source_filename(cluster_id, day.isoformat())
    if (manifest.get("report_date") != day.isoformat()
            or manifest.get("cluster_id") != cluster_id
            or manifest.get("filename") != expected_filename
            or manifest.get("sha256") != expected_sha256
            or type(manifest.get("row_count")) is not int or manifest["row_count"] < 0):
        raise ValueError("legacy acquisition manifest identity mismatch")
    digest, total = hashlib.sha256(), 0
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_LEGACY_ARTIFACT_BYTES:
                raise ValueError("legacy artifact grew beyond size limit")
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("legacy artifact checksum mismatch")
    return {"report_date": day.isoformat(), "cluster_id": cluster_id,
            "filename": expected_filename, "sha256": expected_sha256,
            "row_count": manifest["row_count"]}
