"""Cell-format stage boundaries and checksummed database-to-Sheets handoff."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..cell_calculation import recalculate_stale_history
from ..cell_contracts import (
    CellIdentity, business_date, fingerprint, read_bounded_bytes, roster_fingerprint,
)
from ..cell_database import cell_publication_session, prepare_cell_day, validate_cluster_batch
from ..cell_publication import ExistingSheetAdapter
from ..cell_publication_v2 import (
    build_cell_cluster_matrices_v2, publish_cell_cluster_v2, validate_output_targets,
)
from ..cell_workflow import MAX_BUNDLE_BYTES, _prepare_cell_bundle_with_configs, make_cell_sheet_client
from .settings import (
    CELL_ROSTER, REPORTING_START_DATE, database_target, expand_cell_range,
    report_config, sheet_targets, validate_reporting_date,
)

MAX_PREPARED_BYTES = MAX_BUNDLE_BYTES
MAX_HANDOFF_BYTES = 16 * 1024 * 1024
HANDOFF_SCHEMA_VERSION = 1


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in internal cell artifact")
        result[key] = value
    return result


def _write_json(path: str | Path, value: dict, limit: int) -> tuple[str, int]:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > limit:
        raise ValueError("cell internal artifact exceeds size limit")
    pending = destination.with_name("." + destination.name + ".pending")
    try:
        pending.write_bytes(data)
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest(), len(data)


def _read_json(path: str | Path, expected_sha256: str, limit: int) -> dict:
    data = read_bounded_bytes(path, limit)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("cell internal artifact checksum mismatch")
    value = json.loads(data, object_pairs_hook=_unique_json)
    if not isinstance(value, dict):
        raise ValueError("cell internal artifact must be a JSON object")
    return value


def _cell_config(cluster_id: str, first_report_date: str):
    return report_config(cluster_id, first_report_date)


def validate_sheet_range(start_date: str, end_date: str, cluster_id: str) -> dict:
    """Check every selected code-owned worksheet target without opening clients."""
    runs = expand_cell_range(start_date, end_date, cluster_id)
    accounts, targets = {}, {}
    workbook_id = legacy_id = None
    for selected in sorted({run["cluster_id"] for run in runs}):
        config = _cell_config(selected, start_date)
        selected_workbook, selected_legacy, selected_targets = sheet_targets(selected)
        if workbook_id is not None and (workbook_id != selected_workbook or legacy_id != selected_legacy):
            raise ValueError("cell-format clusters must share the same new and legacy workbook IDs")
        workbook_id, legacy_id = selected_workbook, selected_legacy
        accounts[config.account] = config
        targets[config.account] = selected_targets
    validate_output_targets(workbook_id, legacy_id, accounts, targets)
    return {"clusters": sorted({run["cluster_id"] for run in runs}), "runs": len(runs)}


def preprocess_cell_day(source_bundle: str, source_bundle_sha256: str, report_date: str,
                        cluster_id: str, output_path: str = "cell_prepared_v1.json") -> dict:
    """Validate a source bundle and calculate one complete cluster/date offline."""
    day = validate_reporting_date(report_date)
    config = _cell_config(cluster_id, day)
    _, _, manifest, batches = _prepare_cell_bundle_with_configs(
        source_bundle, source_bundle_sha256, {"schema_version": 1}, {config.account: config},
    )
    if manifest["report_date"] != day or set(batches) != {config.account}:
        raise ValueError("cell bundle differs from requested date/cluster")
    batch = batches[config.account]
    validate_cluster_batch(batch, config.cells)
    build_cell_cluster_matrices_v2(config, batch, day, dummy=True)
    roster_digest = roster_fingerprint(config.cells)
    prepared = {
        "schema_version": 1, "report_date": day, "cluster_id": cluster_id,
        "account": config.account, "roster_version": config.roster_version,
        "roster_fingerprint": roster_digest, "batch": batch,
    }
    digest, size = _write_json(output_path, prepared, MAX_PREPARED_BYTES)
    return {"report_date": day, "cluster_id": cluster_id,
            "prepared_sha256": digest, "prepared_bytes": size}


def _load_prepared(path: str | Path, sha256: str, cluster_id: str | None = None,
                   report_date: str | None = None) -> tuple[dict, object, list[dict]]:
    prepared = _read_json(path, sha256, MAX_PREPARED_BYTES)
    if prepared.get("schema_version") != 1:
        raise ValueError("unsupported cell prepared-artifact schema")
    day = validate_reporting_date(prepared.get("report_date", ""))
    selected_cluster = prepared.get("cluster_id")
    if cluster_id is not None and selected_cluster != cluster_id:
        raise ValueError("prepared artifact cluster differs from task input")
    if report_date is not None and day != business_date(report_date).isoformat():
        raise ValueError("prepared artifact date differs from task input")
    config = _cell_config(selected_cluster, day)
    if (prepared.get("account") != config.account
            or prepared.get("roster_version") != config.roster_version
            or prepared.get("roster_fingerprint") != roster_fingerprint(config.cells)):
        raise ValueError("prepared artifact roster identity mismatch")
    batch = prepared.get("batch")
    validate_cluster_batch(batch, config.cells)
    if any(item["report_date"] != day for item in batch):
        raise ValueError("prepared artifact mixes report dates")
    configuration = config.to_dict()
    config_id = fingerprint(configuration)
    if any(item.get("configuration") != configuration
           or item.get("config_id") != config_id
           or item.get("rules_version") != config.rules_version for item in batch):
        raise ValueError("prepared calculations do not match the code-owned provisional config")
    return prepared, config, batch


def _facts(config, rows):
    identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
    return [{
        "identity": identities[row["cell_code"]],
        "report_date": row["report_date"].isoformat()
        if hasattr(row["report_date"], "isoformat") else str(row["report_date"]),
        "generation_id": row["generation_id"], "calculation_id": row["calculation_id"],
        "config_id": row["config_id"], "rules_version": config.rules_version,
        "calculation": row["result"],
    } for row in rows]


def _selection(facts):
    return sorted(({
        "account": fact["identity"]["account"],
        "cluster_id": fact["identity"]["cluster_id"],
        "cell_code": fact["identity"]["cell_code"],
        "report_date": fact["report_date"],
        "generation_id": fact["generation_id"],
        "calculation_id": fact["calculation_id"],
        "config_id": fact["config_id"],
    } for fact in facts), key=lambda item: (item["report_date"], item["cell_code"]))


def _validate_handoff(handoff):
    if type(handoff.get("schema_version")) is not int or handoff["schema_version"] != HANDOFF_SCHEMA_VERSION:
        raise ValueError("unsupported cell Sheet handoff schema")
    cluster_id = handoff.get("cluster_id")
    if not isinstance(cluster_id, str):
        raise ValueError("cell Sheet handoff cluster is invalid")
    account = cluster_id + "_B"
    codes = CELL_ROSTER.get(account, ())
    if not codes or handoff.get("account") != account:
        raise ValueError("cell Sheet handoff cluster is not in the code-owned roster")
    first = business_date(handoff.get("first_report_date", "")).isoformat()
    last = business_date(handoff.get("last_report_date", "")).isoformat()
    submitted = business_date(handoff.get("submitted_report_date", "")).isoformat()
    if first != REPORTING_START_DATE or first > submitted or submitted > last:
        raise ValueError("cell Sheet handoff dates are inconsistent")
    config = _cell_config(cluster_id, first)
    if handoff.get("roster_fingerprint") != roster_fingerprint(config.cells):
        raise ValueError("cell Sheet handoff roster fingerprint mismatch")
    selected = handoff.get("selected")
    if not isinstance(selected, list) or not selected or len(selected) > 100_000:
        raise ValueError("cell Sheet handoff selection is empty or excessive")
    if handoff.get("history_fingerprint") != fingerprint(selected):
        raise ValueError("cell Sheet handoff history fingerprint mismatch")
    expected_cells = {cell.cell_code for cell in config.cells}
    by_day = {}
    seen = set()
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("cell Sheet handoff contains a malformed selection")
        if (item.get("account") != account or item.get("cluster_id") != cluster_id
                or item.get("cell_code") not in expected_cells
                or not all(isinstance(item.get(key), str) and item[key]
                           for key in ("generation_id", "calculation_id", "config_id"))):
            raise ValueError("cell Sheet handoff contains an invalid selected source")
        day = business_date(item.get("report_date", "")).isoformat()
        if not first <= day <= last:
            raise ValueError("cell Sheet handoff selection is outside its history range")
        key = (day, item["cell_code"])
        if key in seen:
            raise ValueError("cell Sheet handoff contains duplicate selected sources")
        seen.add(key)
        by_day.setdefault(day, set()).add(item["cell_code"])
    if any(codes != expected_cells for codes in by_day.values()):
        raise ValueError("cell Sheet handoff contains incomplete cluster-day coverage")
    return cluster_id, config


def _current_history(store, config):
    first = config.first_report_date
    latest = store.latest_report_date()
    if not latest:
        raise ValueError("no persisted cell history exists for Sheet publication")
    return _facts(config, store.current_results(first, latest))


def persist_cell_day(prepared_path: str, prepared_sha256: str, *, database_password: str | None,
                     output_path: str = "cell_handoff_v1.json", dry_run: bool = False,
                     target=None, session_factory=cell_publication_session) -> dict:
    """Commit source/calculation rows only, then emit a history-bound handoff."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be an explicit boolean")
    prepared, provisional, provisional_batch = _load_prepared(prepared_path, prepared_sha256)
    if dry_run:
        build_cell_cluster_matrices_v2(provisional, provisional_batch,
                                       prepared["report_date"], dummy=True)
        return {"status": "DRY_RUN", "cluster_id": prepared["cluster_id"],
                "report_date": prepared["report_date"], "selected_cells": len(provisional_batch)}
    if not database_password:
        raise ValueError("FINPAY_DB_PASSWORD Kestra secret is required")
    target = database_target() if target is None else target
    cluster_id = prepared["cluster_id"]
    day = prepared["report_date"]
    with session_factory(
        target, {target}, cluster_id, "", password=database_password,
        approved_sheet_ids=set(), write_gsheet=False,
    ) as store:
        earliest = store.earliest_report_date()
        if earliest and earliest < REPORTING_START_DATE:
            raise ValueError(
                f"persisted cell history predates the code-owned reporting start {REPORTING_START_DATE}"
            )
        first = REPORTING_START_DATE
        config = _cell_config(cluster_id, first)
        provisional_config, final_config = provisional.to_dict(), config.to_dict()
        for key in ("first_report_date", "opening_boundary"):
            provisional_config.pop(key, None)
            final_config.pop(key, None)
        if provisional_config != final_config:
            raise ValueError("prepared cell calculations cannot be rebound to the active config")
        batch = [prepare_cell_day(
            CellIdentity(**item["identity"]), item["manifest"], item["raw_rows"],
            item["calculation"], config.to_dict(), config.rules_version,
        ) for item in provisional_batch]
        validate_cluster_batch(batch, config.cells)
        through = max(day, store.latest_report_date() or day)
        history_batches = recalculate_stale_history(
            store, config, through, submitted_dates={day},
        )
        keyed = {
            (fact["report_date"], fact["identity"]["cell_code"]): fact
            for fact in _facts(config, store.current_results(first, through))
        }
        for cohort in [*history_batches, batch]:
            for fact in cohort:
                keyed[(fact["report_date"], fact["identity"]["cell_code"])] = fact
        candidate = list(keyed.values())
        build_cell_cluster_matrices_v2(config, candidate, through, dummy=True)

        with store.connection.transaction():
            for cohort in sorted([*history_batches, batch], key=lambda rows: rows[0]["report_date"]):
                store.commit_cluster_day(cohort, config.cells)
            through = store.latest_report_date()
            committed = _current_history(store, config)
            build_cell_cluster_matrices_v2(config, committed, through, dummy=True)
            selected = _selection(committed)
            handoff = {
                "schema_version": HANDOFF_SCHEMA_VERSION, "cluster_id": cluster_id,
                "account": config.account, "submitted_report_date": day,
                "first_report_date": first, "last_report_date": through,
                "roster_fingerprint": roster_fingerprint(config.cells),
                "selected": selected, "history_fingerprint": fingerprint(selected),
            }
        digest, size = _write_json(output_path, handoff, MAX_HANDOFF_BYTES)
    return {"status": "PERSISTED", "cluster_id": cluster_id, "report_date": day,
            "history_fingerprint": handoff["history_fingerprint"],
            "handoff_sha256": digest, "handoff_bytes": size}


def publish_cell_sheets(handoff_path: str, handoff_sha256: str, *, database_password: str | None,
                        credentials_info: dict | None, cluster_id: str | None = None,
                        dry_run: bool = False, target=None, session_factory=cell_publication_session,
                        sheet_client_factory=make_cell_sheet_client) -> dict:
    """Recheck persisted history under locks, then use the guarded four-tab writer."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be an explicit boolean")
    handoff = _read_json(handoff_path, handoff_sha256, MAX_HANDOFF_BYTES)
    actual_cluster, config = _validate_handoff(handoff)
    if cluster_id is not None and cluster_id != actual_cluster:
        raise ValueError("cell Sheet handoff cluster differs from task input")
    if dry_run:
        return {"status": "DRY_RUN", "cluster_id": actual_cluster,
                "history_fingerprint": handoff["history_fingerprint"]}
    if not database_password or not credentials_info:
        raise ValueError("FINPAY_DB_PASSWORD and GCP_SA_KEY Kestra secrets are required")
    workbook_id, legacy_workbook_id, targets = sheet_targets(actual_cluster)
    validate_output_targets(
        workbook_id, legacy_workbook_id, {config.account: config}, {config.account: targets},
    )
    if credentials_info.get("client_email") != next(iter(targets.values())).writer_email:
        raise ValueError("Google credential identity differs from the code-owned cell writer")
    target = database_target() if target is None else target
    approved_db = {target}
    with session_factory(
        target, approved_db, actual_cluster, workbook_id, password=database_password,
        approved_sheet_ids={workbook_id}, write_gsheet=True,
    ) as store:
        earliest = store.earliest_report_date()
        if earliest != handoff["first_report_date"]:
            raise ValueError("stale cell Sheet handoff: persisted history start changed")
        committed = _current_history(store, config)
        selected = _selection(committed)
        if (selected != handoff["selected"]
                or fingerprint(selected) != handoff["history_fingerprint"]
                or store.latest_report_date() != handoff["last_report_date"]):
            raise ValueError("stale cell Sheet handoff: persisted source/calculation history changed")
        build_cell_cluster_matrices_v2(
            config, committed, handoff["last_report_date"], dummy=True,
        )

        client = sheet_client_factory(credentials_info)
        workbook = client.open_by_key(workbook_id)
        adapters = {
            role: ExistingSheetAdapter(
                workbook, workbook.get_worksheet_by_id(item.worksheet_id),
            ) for role, item in targets.items()
        }
        result = publish_cell_cluster_v2(
            store, workbook_id, targets, adapters, None, config,
            publication_only=True, preserve_manual_inputs=True,
        )
    return {"status": "COMPLETE", "cluster_id": actual_cluster,
            "history_fingerprint": handoff["history_fingerprint"], **result}
