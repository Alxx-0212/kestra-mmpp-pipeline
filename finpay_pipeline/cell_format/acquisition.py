"""Single-cluster cell-format DigiPOS acquisition with an internal ZIP handoff."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from ..cell_contracts import (
    MAX_CELL_ARTIFACT_BYTES, configured_cells, sha256_file_bounded,
    validate_complete_manifest,
)
from ..cell_progress import Progress
from ..digipos_cells import scrape_cell_roster
from ..digipos_scraper import expected_source_filename
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
    with Progress(
        "cell_day_acquisition", total=2, cluster_id=cluster_id,
        report_date=day, account=account, role="acquisition",
    ) as progress:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        progress.update("cell_roster_acquisition_start", count=len(codes))
        manifest = scrape_cell_roster(
            day, password, {account: codes}, str(directory), max_attempts=max_attempts,
            googleapis_compatibility=googleapis_compatibility,
        )
        if manifest["report_date"] != day or {item["account"] for item in manifest["cells"]} != {account}:
            raise ValueError("cell acquisition identity differs from the requested date/cluster")
        progress.update("cell_roster_acquisition_complete", completed=1, count=len(manifest["cells"]))
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
            progress.update("source_bundle_write_start", count=len(files))
            with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                for source, name in files:
                    archive.write(source, arcname=name)
            digest = sha256_file_bounded(pending, MAX_BUNDLE_BYTES)
            os.replace(pending, destination)
        finally:
            pending.unlink(missing_ok=True)
        progress.update("source_bundle_ready", completed=2, bytes=destination.stat().st_size)
        return {
            "report_date": day, "cluster_id": cluster_id, "account": account,
            "source_bundle": str(destination), "source_bundle_sha256": digest,
            "cells": len(manifest["cells"]),
        }


def copy_cell_sources_to_inbox(source_bundle: str | Path, source_bundle_sha256: str,
                               report_date: str, cluster_id: str,
                               inbox_dir: str | Path) -> dict:
    """Atomically publish a verified cell cohort to its content-addressed inbox."""
    day = validate_reporting_date(report_date)
    account, codes = account_settings(cluster_id)
    cluster_id = account[:-2]
    cells = configured_cells({account: codes})
    bundle_path = Path(source_bundle)
    if sha256_file_bounded(bundle_path, MAX_BUNDLE_BYTES) != source_bundle_sha256:
        raise ValueError("cell source bundle checksum mismatch")

    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("cell source bundle has invalid entries")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("report_date") != day:
            raise ValueError("cell source bundle date mismatch")
        validate_complete_manifest(manifest, cells)
        entries = {
            (entry["account"], entry["cell_code"]): entry
            for entry in manifest["cells"]
        }

        artifacts = []
        expected_names = {"manifest.json"}
        for cell in cells:
            entry = entries[(cell.account, cell.cell_code)]
            name = entry["artifact"]
            if not name:
                continue
            if name != f"{cell.account}-{cell.cell_code}.xlsx":
                raise ValueError("cell source artifact name mismatch")
            info = archive.getinfo(name)
            if (info.file_size <= 0 or info.file_size > MAX_CELL_ARTIFACT_BYTES
                    or info.compress_type != zipfile.ZIP_STORED):
                raise ValueError("cell source artifact is invalid or exceeds size limit")
            artifacts.append((cell, entry, info))
            expected_names.add(name)
        if set(names) != expected_names:
            raise ValueError("cell source bundle contains unexpected entries")
        generation_identity = {
            "report_date": day,
            "cluster_id": cluster_id,
            "cells": sorted((
                {
                    "account": cell.account,
                    "cell_code": cell.cell_code,
                    "status": entries[(cell.account, cell.cell_code)]["status"],
                    "source_sha256": entries[(cell.account, cell.cell_code)]["source_sha256"],
                }
                for cell in cells
            ), key=lambda item: item["cell_code"]),
        }
        generation_sha256 = hashlib.sha256(json.dumps(
            generation_identity, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()

        inbox_path = Path(inbox_dir)
        if not inbox_path.is_absolute():
            raise ValueError("cell inbox directory must be absolute")
        if inbox_path.is_symlink():
            raise ValueError("cell inbox root cannot be a symlink")
        inbox_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        inbox_root = inbox_path.resolve()
        filename = expected_source_filename(cluster_id, day)

        def ensure_directory(relative_path: Path) -> Path:
            current = inbox_root
            for part in relative_path.parts:
                if part in ("", ".", ".."):
                    raise ValueError("invalid cell inbox path component")
                current = current / part
                if current.is_symlink():
                    raise ValueError("cell inbox path cannot contain symlinks")
                current.mkdir(mode=0o700, exist_ok=True)
                if not current.resolve().is_relative_to(inbox_root):
                    raise ValueError("cell inbox path escaped its root")
            return current

        cohort_dir = ensure_directory(Path(cluster_id, day))
        generation_dir = cohort_dir / generation_sha256
        if generation_dir.is_symlink():
            raise ValueError("cell inbox generation cannot be a symlink")

        expected_files = {
            f"{cell.cell_code}/{filename}": (entry, info)
            for cell, entry, info in artifacts
        }

        def verify_generation(directory: Path) -> None:
            if not directory.is_dir():
                raise ValueError("existing cell inbox generation is not a directory")
            members = list(directory.rglob("*"))
            if any(member.is_symlink() for member in members):
                raise ValueError("existing cell inbox generation contains a symlink")
            if any(not member.is_file() and not member.is_dir() for member in members):
                raise ValueError("existing cell inbox generation contains a special file")
            actual_files = {
                member.relative_to(directory).as_posix()
                for member in members if member.is_file()
            }
            if actual_files != set(expected_files):
                raise ValueError("existing cell inbox generation is incomplete")
            for relative_name, (entry, info) in expected_files.items():
                existing = directory / relative_name
                if (existing.stat().st_size != info.file_size
                        or sha256_file_bounded(existing, MAX_CELL_ARTIFACT_BYTES)
                        != entry["source_sha256"]):
                    raise ValueError("existing cell inbox generation checksum mismatch")

        if generation_dir.exists():
            verify_generation(generation_dir)
            return {"files_copied": len(artifacts), "generation_sha256": generation_sha256}

        staging_dir = Path(tempfile.mkdtemp(
            prefix=f".{generation_sha256}.pending-", dir=cohort_dir,
        ))
        try:
            for cell, entry, info in artifacts:
                staged_path = staging_dir / cell.cell_code / filename
                staged_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, staged_path.open("xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > MAX_CELL_ARTIFACT_BYTES:
                            raise ValueError("cell source artifact exceeds size limit")
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != info.file_size or digest.hexdigest() != entry["source_sha256"]:
                    raise ValueError("cell source artifact checksum mismatch")
                os.chmod(staged_path, 0o600)

            try:
                os.replace(staging_dir, generation_dir)
                staging_dir = None
            except OSError:
                if not generation_dir.exists():
                    raise
                verify_generation(generation_dir)
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)

    return {"files_copied": len(artifacts), "generation_sha256": generation_sha256}
