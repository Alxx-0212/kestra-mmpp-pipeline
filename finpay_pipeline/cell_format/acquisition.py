"""Single-cluster cell-format DigiPOS acquisition with an internal ZIP handoff."""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from ..cell_contracts import MAX_CELL_ARTIFACT_BYTES, sha256_file_bounded
from ..digipos_cells import scrape_cell_roster
from ..cell_workflow import MAX_BUNDLE_BYTES
from .settings import account_settings, validate_reporting_date


def acquire_cell_day(report_date: str, cluster_id: str, password: str, output_dir: str,
                     *, max_attempts: int = 3, googleapis_compatibility: bool = False) -> dict:
    """Acquire all configured cells for exactly one cluster and date."""
    day = validate_reporting_date(report_date)
    if not password:
        raise ValueError("DIGIPOS_PASSWORD Kestra secret is required")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    account, codes = account_settings(cluster_id)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = scrape_cell_roster(
        day, password, {account: codes}, str(directory), max_attempts=max_attempts,
        googleapis_compatibility=googleapis_compatibility,
    )
    if manifest["report_date"] != day or {item["account"] for item in manifest["cells"]} != {account}:
        raise ValueError("cell acquisition identity differs from the requested date/cluster")
    manifest_bytes = json.dumps(manifest, sort_keys=True, allow_nan=False).encode("utf-8")
    files, estimated_size = [], len(manifest_bytes) + 4096
    cell_dir = directory / day
    for entry in manifest["cells"]:
        name = entry["artifact"]
        if not name:
            continue
        source = cell_dir / name
        if source.parent.resolve() != cell_dir.resolve():
            raise ValueError("cell source artifact escaped its date directory")
        size = source.stat().st_size
        if size > MAX_CELL_ARTIFACT_BYTES:
            raise ValueError("cell source artifact exceeds size limit")
        estimated_size += size + 4096
        files.append((source, name))
    if estimated_size > MAX_BUNDLE_BYTES:
        raise ValueError("cell source bundle exceeds size limit")
    pending = directory / "cell_sources_v2.pending.zip"
    destination = directory / "cell_sources_v2.zip"
    try:
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for source, name in files:
                archive.write(source, arcname=name)
        digest = sha256_file_bounded(pending, MAX_BUNDLE_BYTES)
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return {
        "report_date": day, "cluster_id": cluster_id, "account": account,
        "source_bundle": str(destination), "source_bundle_sha256": digest,
        "cells": len(manifest["cells"]),
    }
