"""Cell-format stage boundaries and checksummed database-to-Sheets handoff."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

from ..cell_calculation import recalculate_stale_history
from ..cell_contracts import (
    CellIdentity, business_date, fingerprint, read_bounded_bytes, roster_fingerprint,
)
from ..cell_database import cell_publication_session, prepare_cell_day, validate_cluster_batch
from ..cell_publication import ExistingSheetAdapter
from ..cell_publication_v2 import (
    ROLES, build_cell_cluster_matrices_v2, cell_worksheet_title,
    preflight_unpublished_v2_sheet, publish_cell_cluster_v2, validate_output_targets,
)
from ..cell_workflow import MAX_BUNDLE_BYTES, _prepare_cell_bundle_with_configs, make_cell_sheet_client
from .settings import (
    CELL_FORMAT_NAMING_SCHEME, CELL_ROSTER, REPORTING_START_DATE,
    CellSheetConfiguration, database_target,
    expand_cell_range, report_config, selected_clusters, sheet_configuration,
    sheet_targets, validate_reporting_date, validate_sheet_target_group,
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
    """Validate target settings and selected clusters without opening clients."""
    runs = expand_cell_range(start_date, end_date, cluster_id)
    clusters = selected_clusters(cluster_id)
    validate_sheet_target_group(clusters)
    return {"clusters": list(clusters), "runs": len(runs)}


def _spreadsheet_files(client, title):
    """List exact-name spreadsheets through Drive metadata-read-only."""
    escaped = title.replace("\\", "\\\\").replace("'", "\\'")
    params = {
        "q": ("name = '" + escaped + "' and mimeType = "
              "'application/vnd.google-apps.spreadsheet' and trashed = false"),
        "corpora": "allDrives", "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true", "spaces": "drive", "pageSize": 1000,
        "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType)",
    }
    files = []
    while True:
        response = client.http_client.request(
            "get", "https://www.googleapis.com/drive/v3/files", params=params,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, dict) or page.get("incompleteSearch"):
            raise RuntimeError("Google Drive exact-title search was incomplete")
        entries = page.get("files")
        if not isinstance(entries, list):
            raise RuntimeError("Google Drive returned an invalid spreadsheet listing")
        files.extend(
            item for item in entries
            if isinstance(item, dict) and item.get("name") == title
            and item.get("mimeType") == "application/vnd.google-apps.spreadsheet"
        )
        token = page.get("nextPageToken")
        if not token:
            return files
        params["pageToken"] = token


def _verify_workbook(workbook, spreadsheet_id, spreadsheet_name):
    if getattr(workbook, "id", None) != spreadsheet_id:
        raise ValueError("opened cell-format workbook ID differs from the resolved target")
    metadata = workbook.fetch_sheet_metadata()
    if metadata.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("opened cell-format workbook metadata ID differs from the resolved target")
    title = metadata.get("properties", {}).get("title")
    if spreadsheet_name and title != spreadsheet_name:
        raise ValueError("opened cell-format workbook title differs from the configured exact title")
    return metadata


def _verify_workbook_access(workbook, configuration):
    try:
        permissions = workbook.list_permissions()
    except Exception as exc:
        raise RuntimeError(
            "cannot verify existing cell-format workbook editor access; grant writer and Finance editor access manually"
        ) from exc
    if not isinstance(permissions, list) or any(not isinstance(item, dict) for item in permissions):
        raise RuntimeError("Google Drive returned malformed workbook permission metadata")
    editors = {
        item["emailAddress"].casefold()
        for item in permissions
        if item.get("role") in {"owner", "writer", "organizer", "fileOrganizer"}
        and isinstance(item.get("emailAddress"), str)
    }
    required = {configuration.writer_email, *configuration.finance_emails}
    if not {email.casefold() for email in required} <= editors:
        raise PermissionError(
            "existing cell-format workbook must already grant editor access to the configured writer and Finance; "
            "sharing was not changed"
        )


def _name_lock_id(title):
    return "cell-format-name:" + hashlib.sha256(title.encode("utf-8")).hexdigest()


def _spreadsheet_by_name(client, title):
    matches = _spreadsheet_files(client, title)
    if len(matches) > 1:
        raise ValueError("multiple Google spreadsheets have the configured exact title")
    if not matches:
        return None
    spreadsheet_id = matches[0].get("id")
    if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
        raise RuntimeError("Google Drive listing did not return the spreadsheet ID")
    workbook = client.open_by_key(spreadsheet_id)
    _verify_workbook(workbook, spreadsheet_id, title)
    return workbook, spreadsheet_id


def _journaled_create_spreadsheet(store, client, configuration):
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    try:
        store.assert_alive()
        try:
            created = client.create(configuration.spreadsheet_name)
        except Exception as exc:
            # A matching title after a lost create response may belong to another flow.
            raise RuntimeError(
                "spreadsheet create response was ambiguous; no sharing or writes were attempted"
            ) from exc
        created_id = getattr(created, "id", None)
        matches = _spreadsheet_files(client, configuration.spreadsheet_name)
        if (len(matches) != 1 or not isinstance(created_id, str) or not created_id
                or matches[0].get("id") != created_id):
            raise RuntimeError("spreadsheet creation response did not uniquely identify the new file")
        spreadsheet_id = created_id
        workbook = client.open_by_key(spreadsheet_id)
        _verify_workbook(workbook, spreadsheet_id, configuration.spreadsheet_name)
        for email in configuration.finance_emails:
            if email.casefold() != configuration.writer_email.casefold():
                workbook.share(email, perm_type="user", role="writer", notify=False)
        _verify_workbook_access(workbook, configuration)
        store.finish_target_operation(operation_id)
        return workbook, spreadsheet_id
    except Exception as exc:
        try:
            store.finish_target_operation(operation_id, unknown_error=type(exc).__name__)
        except Exception:
            pass
        raise RuntimeError(
            "spreadsheet creation/sharing outcome is unknown; target is blocked for operator reconciliation"
        ) from exc


def resolve_cell_workbook(client, configuration: CellSheetConfiguration, store=None, *, cluster_id=None):
    """Open by explicit ID or exact Drive title; only a missing name can create."""
    if configuration.spreadsheet_id:
        spreadsheet_id = configuration.spreadsheet_id
        if store is not None and cluster_id is not None:
            store.assert_cluster_target(spreadsheet_id)
        workbook = client.open_by_key(spreadsheet_id)
        _verify_workbook(workbook, spreadsheet_id, configuration.spreadsheet_name)
        return workbook, spreadsheet_id, False

    workbook_match = _spreadsheet_by_name(client, configuration.spreadsheet_name)
    if workbook_match:
        if store is not None and cluster_id is not None:
            store.assert_cluster_target(workbook_match[1])
        return workbook_match[0], workbook_match[1], False
    if not configuration.create_spreadsheet_if_missing:
        raise FileNotFoundError(
            "configured cell-format spreadsheet was not found and spreadsheet creation is disabled"
        )
    if store is None:
        raise RuntimeError("workbook creation requires the locked database target-operation journal")
    if cluster_id is not None:
        store.assert_cluster_target(None)
    workbook, spreadsheet_id = _journaled_create_spreadsheet(store, client, configuration)
    return workbook, spreadsheet_id, True


def _journaled_add_worksheet(store, workbook, title, worksheet_id=None):
    operation_id = uuid.uuid4().hex
    store.begin_target_operation(operation_id)
    try:
        store.assert_alive()
        properties = {"title": title, "gridProperties": {"rowCount": 1000, "columnCount": 26}}
        if worksheet_id is not None:
            properties["sheetId"] = worksheet_id
        response_error = None
        response_id = None
        try:
            response = workbook.batch_update({"requests": [{"addSheet": {"properties": properties}}]})
            replies = response.get("replies", []) if isinstance(response, dict) else []
            if replies:
                add_reply = replies[0].get("addSheet") if isinstance(replies[0], dict) else None
                if not isinstance(add_reply, dict):
                    raise RuntimeError("addSheet response did not include its result")
                response_id = add_reply.get("properties", {}).get("sheetId")
        except Exception as exc:
            response_error = exc
        matches = [item for item in workbook.worksheets() if item.title == title]
        if (len(matches) != 1 or type(getattr(matches[0], "id", None)) is not int
                or (worksheet_id is not None and matches[0].id != worksheet_id)
                or (response_id is not None and matches[0].id != response_id)):
            if response_error:
                raise RuntimeError("addSheet outcome could not be reconciled by exact title/ID readback") from response_error
            raise RuntimeError("addSheet response and exact title/ID readback differ")
        store.assert_alive()
        store.finish_target_operation(operation_id)
        return matches[0]
    except Exception as exc:
        try:
            store.finish_target_operation(operation_id, unknown_error=type(exc).__name__)
        except Exception:
            pass
        raise RuntimeError(
            "cell worksheet creation outcome is unknown; target is blocked for operator reconciliation"
        ) from exc


def _resolve_cell_targets(store, workbook, cluster_id, configuration, publication):
    store.assert_cluster_target(workbook.id)
    if publication and publication["status"] != "COMPLETE":
        raise RuntimeError("cell-format target has unresolved publication state")
    spreadsheet_id = workbook.id
    runtime_configuration = replace(configuration, spreadsheet_id=spreadsheet_id)
    account = cluster_id + "_B"
    worksheets = workbook.worksheets()
    by_id = {}
    by_title = {}
    for worksheet in worksheets:
        if type(worksheet.id) is not int or not isinstance(worksheet.title, str) or not worksheet.title:
            raise RuntimeError("Google Sheets returned malformed worksheet identity metadata")
        if worksheet.id in by_id:
            raise RuntimeError("Google Sheets returned duplicate worksheet IDs")
        by_id[worksheet.id] = worksheet
        by_title.setdefault(worksheet.title, []).append(worksheet)
    configured_ids = (configuration.worksheet_ids or {}).get(account)
    if configuration.worksheet_ids is not None and configured_ids is None:
        raise ValueError("CELL_FORMAT_WORKSHEET_IDS is missing the selected account")
    worksheet_ids = {}
    existing_worksheets = {}

    if publication:
        snapshot = publication.get("snapshot") or {}
        prior = snapshot.get("worksheets")
        if ((snapshot.get("schema_version"), snapshot.get("spreadsheet_id")) != (2, spreadsheet_id)
                or not isinstance(prior, dict) or set(prior) != set(ROLES)):
            raise ValueError("prior cell publication target snapshot is incompatible; reconcile before publishing")
        for role in ROLES:
            item = prior[role]
            worksheet_id, title = item.get("worksheet_id"), item.get("title")
            expected_title = cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME)
            worksheet = by_id.get(worksheet_id)
            if (title != expected_title or worksheet is None or worksheet.title != title
                    or (configured_ids and configured_ids.get(role) != worksheet_id)):
                raise ValueError(
                    "committed cell publication worksheet is missing or mismatched; operator reconciliation required"
                )
            worksheet_ids[role] = worksheet_id
    else:
        missing = []
        for role in ROLES:
            title = cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME)
            matches = by_title.get(title, [])
            if len(matches) > 1:
                raise ValueError("multiple worksheets have the same exact cell alias title")
            requested_id = configured_ids.get(role) if configured_ids else None
            if requested_id is not None:
                existing_by_id = by_id.get(requested_id)
                if existing_by_id and existing_by_id.title != title:
                    raise ValueError("configured worksheet ID resolves to a different title")
                if matches and matches[0].id != requested_id:
                    raise ValueError("configured worksheet title resolves to a different ID")
                if existing_by_id:
                    worksheet_ids[role] = requested_id
                    existing_worksheets[role] = existing_by_id
                elif matches:
                    raise ValueError("configured worksheet ID/title identity differs")
                else:
                    missing.append((role, title, requested_id))
            elif matches:
                worksheet_ids[role] = matches[0].id
                existing_worksheets[role] = matches[0]
            else:
                missing.append((role, title, None))
        if len(set(worksheet_ids.values())) != len(worksheet_ids):
            raise ValueError("resolved cell worksheet IDs must be unique")
        if missing and not configuration.create_worksheets_if_missing:
            raise FileNotFoundError("one or more alias worksheets are missing and worksheet creation is disabled")
        for role, worksheet in existing_worksheets.items():
            preflight_unpublished_v2_sheet(
                ExistingSheetAdapter(workbook, worksheet),
                cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME),
            )
        for role, title, requested_id in missing:
            worksheet = _journaled_add_worksheet(store, workbook, title, requested_id)
            worksheet_ids[role] = worksheet.id

    targets = sheet_targets(
        cluster_id, spreadsheet_id=spreadsheet_id,
        worksheet_ids=worksheet_ids, configuration=runtime_configuration,
    )[2]
    report = _cell_config(cluster_id, REPORTING_START_DATE)
    validate_output_targets(
        spreadsheet_id, configuration.legacy_spreadsheet_id,
        {account: report}, {account: targets},
    )
    adapters = {}
    for role, target in targets.items():
        worksheet = workbook.get_worksheet_by_id(target.worksheet_id)
        if worksheet is None or worksheet.id != target.worksheet_id or worksheet.title != target.title:
            raise ValueError("resolved cell worksheet workbook/ID/title identity changed")
        adapters[role] = ExistingSheetAdapter(workbook, worksheet)
    return targets, adapters


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


def _verify_handoff_history(store, handoff, config):
    earliest = store.earliest_report_date()
    if earliest != handoff["first_report_date"]:
        raise ValueError("stale cell Sheet handoff: persisted history start changed")
    committed = _current_history(store, config)
    selected = _selection(committed)
    if (selected != handoff["selected"]
            or fingerprint(selected) != handoff["history_fingerprint"]
            or store.latest_report_date() != handoff["last_report_date"]):
        raise ValueError("stale cell Sheet handoff: persisted source/calculation history changed")
    return committed


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
    configuration = sheet_configuration()
    if not isinstance(credentials_info, dict) or not isinstance(credentials_info.get("client_email"), str):
        raise ValueError("Google service-account credentials are required")
    if credentials_info["client_email"].casefold() != configuration.writer_email.casefold():
        raise ValueError("Google credential identity differs from the code-owned cell writer")
    target = database_target() if target is None else target
    client = None
    workbook_id = configuration.spreadsheet_id
    if configuration.spreadsheet_name:
        include_drive_file = not workbook_id and configuration.create_spreadsheet_if_missing
        client = (sheet_client_factory(credentials_info, include_drive_file=True)
                  if include_drive_file else sheet_client_factory(credentials_info))
        name_lock = _name_lock_id(configuration.spreadsheet_name)
        with session_factory(
            target, {target}, actual_cluster, name_lock, password=database_password,
            approved_sheet_ids={name_lock}, write_gsheet=True,
        ) as name_store:
            _verify_handoff_history(name_store, handoff, config)
            _, workbook_id, _ = resolve_cell_workbook(
                client, configuration, name_store, cluster_id=actual_cluster,
            )
    if not workbook_id:
        raise ValueError("cell-format workbook ID could not be resolved")
    if workbook_id == configuration.legacy_spreadsheet_id:
        raise ValueError("cell-format workbook must be distinct from the legacy workbook")
    approved_db = {target}
    with session_factory(
        target, approved_db, actual_cluster, workbook_id, password=database_password,
        approved_sheet_ids={workbook_id}, write_gsheet=True,
    ) as store:
        committed = _verify_handoff_history(store, handoff, config)
        publication = store.latest_publication(sheet_only=True)
        if publication and publication["status"] != "COMPLETE":
            raise RuntimeError("cell-format target has unresolved publication state")
        build_cell_cluster_matrices_v2(
            config, committed, handoff["last_report_date"], dummy=True,
        )

        if client is None:
            client = sheet_client_factory(credentials_info)
        workbook = client.open_by_key(workbook_id)
        _verify_workbook(workbook, workbook_id, configuration.spreadsheet_name)
        _verify_workbook_access(workbook, configuration)
        targets, adapters = _resolve_cell_targets(
            store, workbook, actual_cluster, configuration, publication,
        )
        result = publish_cell_cluster_v2(
            store, workbook_id, targets, adapters, None, config,
            publication_only=True, preserve_manual_inputs=True,
        )
    return {"status": "COMPLETE", "cluster_id": actual_cluster,
            "history_fingerprint": handoff["history_fingerprint"], **result}
