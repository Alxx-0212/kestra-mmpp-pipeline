"""Cell-format staged persistence and database-backed Sheet publication."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

from ..cell_contracts import (
    CellIdentity, business_date, fingerprint, read_bounded_bytes, roster_fingerprint,
)
from ..cell_database import cell_publication_session, prepare_cell_day, validate_cluster_batch
from ..cell_observability import cohort_counts, observed, report
from ..cell_publication import (
    _read_with_retry, publish_cell_date_blocks,
)
from ..cell_publication_v2 import (
    ROLES, build_cell_cluster_matrices_v2, cell_worksheet_title,
    validate_output_targets,
)
from ..cell_workflow import MAX_BUNDLE_BYTES, _prepare_cell_bundle_with_configs, make_cell_sheet_client
from .settings import (
    CELL_FORMAT_NAMING_SCHEME, REPORTING_START_DATE,
    CellSheetConfiguration, account_settings, database_target,
    expand_cell_range, report_config, selected_clusters, sheet_configuration,
    sheet_targets, validate_reporting_date, validate_sheet_target_group,
)

MAX_PREPARED_BYTES = MAX_BUNDLE_BYTES


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


def validate_sheet_range(start_date: str, end_date: str, cluster_id: str, *,
                         credentials_info=None, sheet_client_factory=None) -> dict:
    """Validate the selected range and, when provided, exact existing Sheet targets."""
    runs = expand_cell_range(start_date, end_date, cluster_id)
    clusters = selected_clusters(cluster_id)
    validate_sheet_target_group(clusters)
    result = {"clusters": list(clusters), "runs": len(runs)}
    if credentials_info is not None:
        result.update(validate_existing_sheet_targets(
            clusters, credentials_info, sheet_client_factory=sheet_client_factory,
        ))
    return result


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
        def read_page():
            response = client.http_client.request(
                "get", "https://www.googleapis.com/drive/v3/files", params=params,
            )
            response.raise_for_status()
            return response.json()

        page = _read_with_retry(read_page)
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
    metadata = _read_with_retry(workbook.fetch_sheet_metadata)
    if metadata.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("opened cell-format workbook metadata ID differs from the resolved target")
    title = metadata.get("properties", {}).get("title")
    if spreadsheet_name and title != spreadsheet_name:
        raise ValueError("opened cell-format workbook title differs from the configured exact title")
    return metadata


def _verify_workbook_access(workbook, configuration):
    try:
        permissions = _read_with_retry(workbook.list_permissions)
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


def _spreadsheet_by_name(client, title):
    matches = _spreadsheet_files(client, title)
    if len(matches) > 1:
        raise ValueError("multiple Google spreadsheets have the configured exact title")
    if not matches:
        return None
    spreadsheet_id = matches[0].get("id")
    if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
        raise RuntimeError("Google Drive listing did not return the spreadsheet ID")
    workbook = _read_with_retry(lambda: client.open_by_key(spreadsheet_id))
    return workbook, spreadsheet_id


def resolve_cell_workbook(client, configuration: CellSheetConfiguration):
    """Open an existing cell workbook; stateless publication never creates one."""
    if configuration.spreadsheet_id:
        spreadsheet_id = configuration.spreadsheet_id
        workbook = _read_with_retry(lambda: client.open_by_key(spreadsheet_id))
        if getattr(workbook, "id", None) != spreadsheet_id:
            raise ValueError("opened cell workbook ID differs from configured target")
        _verify_workbook(workbook, spreadsheet_id, configuration.spreadsheet_name)
        return workbook, spreadsheet_id, False

    workbook_match = _spreadsheet_by_name(client, configuration.spreadsheet_name)
    if workbook_match is None:
        raise FileNotFoundError("configured cell-format spreadsheet must already exist")
    _verify_workbook(workbook_match[0], workbook_match[1], configuration.spreadsheet_name)
    return workbook_match[0], workbook_match[1], False


def _resolve_existing_cell_targets(workbook, cluster_id, configuration):
    """Resolve the configured four tabs without creation, adoption, or DB sheet state."""
    spreadsheet_id = workbook.id
    if (not spreadsheet_id or (configuration.spreadsheet_id
            and configuration.spreadsheet_id != spreadsheet_id)
            or spreadsheet_id == configuration.legacy_spreadsheet_id):
        raise ValueError("opened cell workbook differs from the configured target")
    metadata = _read_with_retry(lambda: workbook.fetch_sheet_metadata(params={
        "fields": ("spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount),tabColorStyle),"
                   "protectedRanges(protectedRangeId,description,range,unprotectedRanges,warningOnly,editors)),"
                   "developerMetadata(metadataId,metadataKey,location(sheetId))"),
    }))
    if metadata.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("cell workbook metadata ID differs from configured target")
    account, _ = account_settings(cluster_id)
    configured = (configuration.worksheet_ids or {}).get(account)
    if configuration.worksheet_ids is not None and configured is None:
        raise ValueError("CELL_FORMAT_WORKSHEET_IDS is missing the selected account")
    by_id = {}
    by_title = {}
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        sheet_id, title = properties.get("sheetId"), properties.get("title")
        if type(sheet_id) is not int or not isinstance(title, str) or not title:
            raise ValueError("Google Sheets returned malformed worksheet identity metadata")
        if sheet_id in by_id:
            raise ValueError("Google Sheets returned duplicate worksheet IDs")
        by_id[sheet_id] = title
        by_title.setdefault(title, []).append(sheet_id)
    worksheet_ids = {}
    for role in ROLES:
        title = cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME)
        matches = by_title.get(title, [])
        if len(matches) != 1:
            raise FileNotFoundError(f"existing {role} worksheet with exact title is required")
        sheet_id = configured.get(role) if configured else matches[0]
        if sheet_id not in by_id or by_id[sheet_id] != title or matches[0] != sheet_id:
            raise ValueError(f"configured {role} worksheet ID/title mismatch")
        worksheet_ids[role] = sheet_id
    targets = sheet_targets(
        cluster_id, spreadsheet_id=spreadsheet_id,
        worksheet_ids=worksheet_ids, configuration=configuration,
    )[2]
    return targets, metadata


def validate_existing_sheet_targets(clusters, credentials_info, *, sheet_client_factory=None):
    """Read-only preflight of the workbook and exact alias worksheet identities."""
    configuration = sheet_configuration()
    if (not isinstance(credentials_info, dict)
            or not isinstance(credentials_info.get("client_email"), str)
            or credentials_info["client_email"].casefold() != configuration.writer_email.casefold()):
        raise ValueError("Google credential identity differs from the configured cell writer")
    if sheet_client_factory is None:
        from ..cell_workflow import make_cell_sheet_client
        sheet_client_factory = make_cell_sheet_client
    client = sheet_client_factory(credentials_info)
    workbook, spreadsheet_id, created = resolve_cell_workbook(client, configuration)
    if created:
        raise RuntimeError("cell reporting requires an existing workbook")
    _verify_workbook_access(workbook, configuration)
    configs, targets_by_account = {}, {}
    for cluster_id in clusters:
        account, _ = account_settings(cluster_id)
        targets, _ = _resolve_existing_cell_targets(workbook, cluster_id, configuration)
        configs[account] = report_config(cluster_id, REPORTING_START_DATE)
        targets_by_account[account] = targets
    validate_output_targets(
        spreadsheet_id, configuration.legacy_spreadsheet_id,
        configs, targets_by_account,
    )
    return {"spreadsheet_id": spreadsheet_id, "validated_clusters": len(clusters),
            "validated_roles": len(clusters) * len(ROLES)}


@observed("preprocess")
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
    report("source_validated", count=len(batch))
    validate_cluster_batch(batch, config.cells)
    build_cell_cluster_matrices_v2(config, batch, day, dummy=True,
        first_report_date=day, calculate_saldo=False)
    roster_digest = roster_fingerprint(config.cells)
    prepared = {
        "schema_version": 1, "report_date": day, "cluster_id": cluster_id,
        "account": config.account, "roster_version": config.roster_version,
        "roster_fingerprint": roster_digest, "batch": batch,
    }
    digest, size = _write_json(output_path, prepared, MAX_PREPARED_BYTES)
    return {"report_date": day, "cluster_id": cluster_id,
            "notification_summary": json.dumps(cohort_counts(batch), sort_keys=True),
            "prepared_sha256": digest, "prepared_bytes": size}


def _read_prepared_artifact(path: str | Path, sha256: str, cluster_id: str | None = None,
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
    if not isinstance(batch, list):
        raise ValueError("prepared artifact batch must be a list")
    return prepared, config, batch


def _load_prepared(path: str | Path, sha256: str, cluster_id: str | None = None,
                   report_date: str | None = None) -> tuple[dict, object, list[dict]]:
    prepared, config, batch = _read_prepared_artifact(path, sha256, cluster_id, report_date)
    validate_cluster_batch(batch, config.cells)
    if any(item.get("report_date") != prepared["report_date"] for item in batch):
        raise ValueError("prepared artifact mixes report dates")
    configuration = config.to_dict()
    config_id = fingerprint(configuration)
    if any(item.get("configuration") != configuration
           or item.get("config_id") != config_id
           or item.get("rules_version") != config.rules_version for item in batch):
        raise ValueError("prepared calculations do not match the code-owned provisional config")
    return prepared, config, batch


def _load_prepared_for_rebind(path: str | Path, sha256: str, cluster_id: str,
                              report_date: str) -> tuple[dict, object, list[dict]]:
    """Read a bulk member's envelope; row validation is performed by the single rebind pass."""
    return _read_prepared_artifact(path, sha256, cluster_id, report_date)


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


def _prepared_facts(config, batch):
    return [{"identity": item["identity"], "report_date": item["report_date"],
        "generation_id": item["generation_id"], "calculation_id": item["calculation_id"],
        "config_id": item["config_id"], "rules_version": config.rules_version,
        "calculation": item["calculation"]} for item in batch]


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


def _current_history(store, config):
    first = config.first_report_date
    latest = store.latest_report_date()
    if not latest:
        raise ValueError("no persisted cell history exists for Sheet publication")
    return _facts(config, store.current_results(first, latest))


@observed("persist")
def persist_cell_day(prepared_path: str, prepared_sha256: str, *, database_password: str | None,
                     dry_run: bool = False, target=None,
                     session_factory=cell_publication_session) -> dict:
    """Replace the current source generation for one date; Sheet output is not persisted."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be an explicit boolean")
    prepared, provisional, provisional_batch = _load_prepared(prepared_path, prepared_sha256)
    if dry_run:
        build_cell_cluster_matrices_v2(provisional, provisional_batch,
            prepared["report_date"], dummy=True, first_report_date=prepared["report_date"],
            calculate_saldo=False)
        return {"status": "DRY_RUN", "cluster_id": prepared["cluster_id"],
                "report_date": prepared["report_date"], "selected_cells": len(provisional_batch)}
    if not database_password:
        raise ValueError("FINPAY_DB_PASSWORD Kestra secret is required")
    target = database_target() if target is None else target
    cluster_id = prepared["cluster_id"]
    day = prepared["report_date"]
    report("waiting_for_database_lock", cluster_id=cluster_id, report_date=day)
    with session_factory(
        target, {target}, cluster_id, "database-only:" + cluster_id,
        password=database_password, approved_sheet_ids=set(), write_gsheet=False,
    ) as store:
        report("database_lock_acquired")
        config = _cell_config(cluster_id, REPORTING_START_DATE)
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
        build_cell_cluster_matrices_v2(config, _prepared_facts(config, batch), day,
                                       dummy=True, first_report_date=day, calculate_saldo=False)
        with store.connection.transaction():
            store.commit_cluster_day(batch, config.cells)
        committed = _facts(config, store.current_results(day, day))
        if _selection(committed) != _selection(_prepared_facts(config, batch)):
            raise RuntimeError("persisted date cohort differs from its prepared DB data")
        report("database_committed", cluster_id=cluster_id, report_date=day, count=len(batch))
    return {"status": "PERSISTED", "cluster_id": cluster_id, "report_date": day,
            "selected_cells": len(batch)}


def _persisted_cell_matrices(store, cluster_id, report_date):
    config = _cell_config(cluster_id, REPORTING_START_DATE)
    latest = store.latest_report_date()
    if latest is None:
        raise ValueError("no persisted cell data is available for Sheet publication")
    latest = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
    if latest < report_date:
        raise ValueError("the requested report date has not been persisted")
    next_day = (business_date(report_date) + timedelta(days=1)).isoformat()
    through = next_day if latest >= next_day else report_date
    facts = _facts(config, store.current_results(config.first_report_date, through))
    selected = {item["identity"]["cell_code"] for item in facts
                if item["report_date"] == report_date}
    if selected != {cell.cell_code for cell in config.cells}:
        raise ValueError("persisted report date does not contain the complete cluster roster")
    matrices = build_cell_cluster_matrices_v2(
        config, facts, through, first_report_date=config.first_report_date,
        calculate_saldo=True,
    )
    return config, matrices


@observed("publish")
def publish_cell_sheets(report_date: str, cluster_id: str, *, database_password: str | None,
                        credentials_info: dict | None, target=None,
                        session_factory=cell_publication_session,
                        sheet_client_factory=make_cell_sheet_client) -> dict:
    """Render one DB-derived date block and idempotently replace its existing Sheet rows."""
    day = validate_reporting_date(report_date)
    if not database_password or not credentials_info:
        raise ValueError("FINPAY_DB_PASSWORD and GCP_SA_KEY Kestra secrets are required")
    configuration = sheet_configuration()
    if (not isinstance(credentials_info, dict)
            or credentials_info.get("client_email", "").casefold() != configuration.writer_email.casefold()):
        raise ValueError("Google credential identity differs from the configured cell writer")
    target = database_target() if target is None else target
    client = sheet_client_factory(credentials_info)
    workbook, spreadsheet_id, _ = resolve_cell_workbook(client, configuration)
    targets, metadata = _resolve_existing_cell_targets(workbook, cluster_id, configuration)
    if spreadsheet_id != workbook.id:
        raise ValueError("resolved cell workbook identity changed")

    report("waiting_for_publication_lock", cluster_id=cluster_id, report_date=day)
    with session_factory(
        target, {target}, cluster_id, spreadsheet_id, password=database_password,
        approved_sheet_ids={spreadsheet_id}, write_gsheet=True,
    ) as store:
        report("publication_lock_acquired")
        config, matrices = _persisted_cell_matrices(store, cluster_id, day)
        result = publish_cell_date_blocks(workbook, targets, matrices, day, metadata=metadata)
        report("date_block_published", roles=len(targets), requests=result["http_write_batches"])
    return {"cluster_id": cluster_id, **result}
