"""Versioned cell workflow boundary: verified bundles, pure barrier, serial writes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import zipfile
from pathlib import Path

from .cell_calculation import CellReportConfig, build_cluster_report, calculate_cell_day, recalculate_stale_history
from .cell_contracts import (
    MAX_CELL_ARTIFACT_BYTES, OBSERVED_CELL_ROSTER, configured_cells,
    fingerprint, read_bounded_bytes as _bounded_bytes, sha256_file_bounded, validate_complete_manifest,
)
from .cell_database import CellDatabaseTarget, cell_publication_session
from .cell_detail_sheets import build_cell_detail_history
from .cell_invoice_sheets import build_cell_invoice_matrix
from .cell_publication import (
    CellSheetTarget, CellWorksheetTarget, ExistingSheetAdapter, publish_cell_cluster,
)
from .cell_publication_v2 import (
    bind_cell_style_contract, build_cell_cluster_matrices_v2, preflight_cell_cluster_v2, cell_worksheet_title,
    publish_cell_cluster_v2, validate_output_targets,
)
from .cell_sheets import render_cell_matrix
from .digipos_cells import scrape_cell_roster
from .digipos_scraper import HEADERS, _read_xlsx_rows, validate_export_rows

MAX_BUNDLE_BYTES = 256 * 1024 * 1024


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration/manifest JSON key")
        result[key] = value
    return result


def _require_trusted_configuration(expected_sha256, approved_sha256):
    """The approved pin must come from deployment state, never a flow input."""
    if (not isinstance(approved_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", approved_sha256)
            or not isinstance(expected_sha256, str)
            or not hmac.compare_digest(expected_sha256, approved_sha256)):
        raise ValueError("live configuration does not match the deployment-approved checksum")


def read_cell_configuration(path, expected_sha256):
    data = _bounded_bytes(path, 2 * 1024 * 1024)
    if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("configuration size/checksum mismatch")
    configuration = json.loads(data, object_pairs_hook=_unique_json)
    if configuration.get("schema_version") != 1:
        raise ValueError("unsupported cell runtime configuration")
    configs = {}
    for item in configuration.get("accounts", []):
        item = {**item, "cell_codes": tuple(item["cell_codes"])}
        config = CellReportConfig(**item)
        config.to_dict()
        if config.account in configs:
            raise ValueError("duplicate configured account")
        configs[config.account] = config
    if not configs:
        raise ValueError("explicit cell account configurations required")
    return configuration, configs


V2_ROLES = ("summary", "invoice", "unusual", "reversal")


def read_cell_configuration_v2(path, expected_sha256, approved_sha256=None, *, allow_unpinned=False):
    """Parse a separately pinned, workbook-scoped v2 config without v1 fallback."""
    if allow_unpinned:
        if approved_sha256:
            _require_trusted_configuration(expected_sha256, approved_sha256)
    else:
        _require_trusted_configuration(expected_sha256, approved_sha256)
    data = _bounded_bytes(path, 2 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("v2 configuration checksum mismatch")
    runtime = json.loads(data, object_pairs_hook=_unique_json)
    if runtime.get("schema_version") != 2:
        raise ValueError("unsupported cell reporting v2 config")
    workbook = runtime.get("spreadsheet_id")
    legacy_workbook = runtime.get("legacy_spreadsheet_id")
    if not isinstance(workbook, str) or not workbook or not isinstance(legacy_workbook, str) or workbook == legacy_workbook:
        raise ValueError("v2 workbook must be explicit and distinct from v1")
    writer_email, finance_emails = runtime.get("writer_email"), runtime.get("finance_emails")
    if not isinstance(writer_email, str) or not writer_email or not isinstance(finance_emails, list) or not finance_emails:
        raise ValueError("v2 writer/Finance editor identities are required")
    accounts, targets = {}, {}
    all_sheet_ids, all_titles = set(), set()
    for source in runtime.get("accounts", []):
        account = source.get("account")
        if account in accounts:
            raise ValueError("duplicate v2 account config")
        config_fields = dict(source)
        config_fields["cell_codes"] = tuple(config_fields["cell_codes"])
        config = CellReportConfig(**config_fields)
        config.to_dict()
        cluster = account[:-2]
        role_targets = runtime.get("worksheet_targets", {}).get(account)
        if not isinstance(role_targets, dict) or set(role_targets) != set(V2_ROLES):
            raise ValueError("each v2 cluster requires exactly four output target roles")
        targets[account] = {}
        for role in V2_ROLES:
            item = role_targets[role]
            target = CellWorksheetTarget(
                role=role, spreadsheet_id=workbook, worksheet_id=item["worksheet_id"],
                title=item["title"], writer_email=runtime["writer_email"],
                finance_emails=tuple(runtime["finance_emails"]),
                naming_scheme=runtime.get("worksheet_naming", "code"),
            )
            target.sheet_target()
            expected_title = cell_worksheet_title(account, role, target.naming_scheme)
            if target.title != expected_title:
                raise ValueError("v2 worksheet title does not match the pinned role/cluster")
            if target.worksheet_id in all_sheet_ids or target.title in all_titles:
                raise ValueError("duplicate v2 worksheet ID/title")
            all_sheet_ids.add(target.worksheet_id)
            all_titles.add(target.title)
            targets[account][role] = target
        accounts[account] = config
    if not accounts or set(runtime.get("worksheet_targets", {})) != set(accounts):
        raise ValueError("v2 account and worksheet target rosters differ")
    if runtime.get("approved_v2_spreadsheet_ids") != [workbook]:
        raise ValueError("v2 workbook must match the independently pinned target list")
    if runtime.get("data_classification") not in ("PRODUCTION", "SYNTHETIC_CANARY"):
        raise ValueError("explicit v2 data classification is required")
    if runtime["data_classification"] == "PRODUCTION" and set(accounts) != set(OBSERVED_CELL_ROSTER):
        raise ValueError("production v2 config must include the complete _B roster")
    return runtime, accounts, targets


def acquire_cell_bundle(report_date, configuration_path, expected_configuration_sha256,
                        output_dir, *, password, googleapis_compatibility=False,
                        approved_configuration_sha256=None):
    _require_trusted_configuration(expected_configuration_sha256, approved_configuration_sha256)
    runtime, configs = read_cell_configuration(configuration_path, expected_configuration_sha256)
    if runtime.get("dummy_data_only"):
        raise ValueError("dummy configurations cannot select live acquisition")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    roster = {account: config.cell_codes for account, config in configs.items()}
    manifest = scrape_cell_roster(report_date, password, roster, str(directory),
                                  googleapis_compatibility=googleapis_compatibility)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    artifacts = []
    estimated_size = len(manifest_bytes) + 4096
    for entry in manifest["cells"]:
        if entry["artifact"]:
            source = directory / report_date / entry["artifact"]
            if source.parent.resolve() != (directory / report_date).resolve():
                raise ValueError("source artifact is outside acquisition scope")
            size = source.stat().st_size
            if size > MAX_CELL_ARTIFACT_BYTES:
                raise ValueError("source artifact exceeds acquisition size limit")
            estimated_size += size + 4096
            artifacts.append((source, entry["artifact"]))
    if estimated_size > MAX_BUNDLE_BYTES:
        raise ValueError("source bundle exceeds acquisition size limit")
    pending = directory / "cell_sources.pending.zip"
    destination = directory / "cell_sources.zip"
    with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for source, name in artifacts:
            archive.write(source, arcname=name)
    digest = sha256_file_bounded(pending, MAX_BUNDLE_BYTES)
    os.replace(pending, destination)
    return {"report_date": report_date, "source_bundle": str(destination),
            "source_bundle_sha256": digest,
            "cells": len(manifest["cells"])}


def acquire_cell_bundle_v3(report_date, configuration_path, expected_configuration_sha256,
                           output_dir, *, password, approved_configuration_sha256,
                           googleapis_compatibility=False):
    runtime, configs, _ = read_cell_configuration_v2(
        configuration_path, expected_configuration_sha256, approved_configuration_sha256,
    )
    if runtime.get("data_classification") != "PRODUCTION":
        raise ValueError("live acquisition requires an explicitly production-pinned config")
    from .cell_contracts import business_date
    business_date(report_date)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    roster = {account: config.cell_codes for account, config in configs.items()}
    manifest = scrape_cell_roster(report_date, password, roster, str(directory),
                                  googleapis_compatibility=googleapis_compatibility)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    artifacts, estimated_size = [], len(manifest_bytes) + 4096
    for entry in manifest["cells"]:
        if not entry["artifact"]:
            continue
        source = directory / report_date / entry["artifact"]
        if source.parent.resolve() != (directory / report_date).resolve():
            raise ValueError("source artifact is outside acquisition scope")
        size = source.stat().st_size
        if size > MAX_CELL_ARTIFACT_BYTES:
            raise ValueError("source artifact exceeds size limit")
        estimated_size += size + 4096
        artifacts.append((source, entry["artifact"]))
    if estimated_size > MAX_BUNDLE_BYTES:
        raise ValueError("cell source bundle exceeds size limit")
    pending = directory / "cell_sources_v2.pending.zip"
    destination = directory / "cell_sources_v2.zip"
    with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for source, name in artifacts:
            archive.write(source, arcname=name)
    digest = sha256_file_bounded(pending, MAX_BUNDLE_BYTES)
    os.replace(pending, destination)
    return {"report_date": report_date, "source_bundle": str(destination),
            "source_bundle_sha256": digest, "cells": len(manifest["cells"])}


def prepare_cell_bundle(bundle_path, configuration_path, expected_configuration_sha256,
                        expected_bundle_sha256):
    """Read without extracting archive paths and validate every cell before writes."""
    runtime, configs = read_cell_configuration(configuration_path, expected_configuration_sha256)
    return _prepare_cell_bundle_with_configs(bundle_path, expected_bundle_sha256, runtime, configs)


def _prepare_cell_bundle_with_configs(bundle_path, expected_bundle_sha256, runtime, configs):
    data = _bounded_bytes(bundle_path, MAX_BUNDLE_BYTES)
    if len(data) > MAX_BUNDLE_BYTES or hashlib.sha256(data).hexdigest() != expected_bundle_sha256:
        raise ValueError("source bundle size/checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or len(names) > 100:
            raise ValueError("duplicate/excess source bundle entries")
        if any(Path(name).name != name or "\\" in name for name in names):
            raise ValueError("source bundle contains paths instead of flat artifact names")
        if sum(item.file_size for item in infos) > MAX_BUNDLE_BYTES:
            raise ValueError("uncompressed source bundle exceeds limit")
        manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=_unique_json)
        cells = configured_cells({account: config.cell_codes for account, config in configs.items()})
        validate_complete_manifest(manifest, cells)
        wanted = {"manifest.json"} | {entry["artifact"] for entry in manifest["cells"] if entry["artifact"]}
        if set(names) != wanted:
            raise ValueError("source bundle has missing/unexpected artifacts")
        entries = {(entry["account"], entry["cell_code"]): entry for entry in manifest["cells"]}
        batches = {account: [] for account in configs}
        for cell in cells:
            entry = entries[(cell.account, cell.cell_code)]
            rows = []
            if entry["artifact"]:
                if archive.getinfo(entry["artifact"]).file_size > MAX_CELL_ARTIFACT_BYTES:
                    raise ValueError("cell XLSX exceeds artifact size limit")
                content = archive.read(entry["artifact"])
                if hashlib.sha256(content).hexdigest() != entry["source_sha256"]:
                    raise ValueError("cell XLSX checksum mismatch")
                with zipfile.ZipFile(io.BytesIO(content)) as inner:
                    if len(inner.infolist()) > 1000 or sum(item.file_size for item in inner.infolist()) > 64 * 1024 * 1024:
                        raise ValueError("expanded cell XLSX exceeds parsing size limit")
                exported = _read_xlsx_rows(io.BytesIO(content))
                validate_export_rows(exported, manifest["report_date"], entry["validated_rows"])
                rows = [dict(zip(HEADERS, row)) for row in exported[2:] if any(value is not None for value in row)]
            batches[cell.account].append(calculate_cell_day(cell, entry, rows, configs[cell.account]))
    return runtime, configs, manifest, batches


def prepare_cell_bundle_v2(bundle_path, configuration_path, expected_configuration_sha256,
                           expected_bundle_sha256, *, approved_configuration_sha256=None,
                           dry_run=False):
    runtime, configs, targets = read_cell_configuration_v2(
        configuration_path, expected_configuration_sha256, approved_configuration_sha256,
        allow_unpinned=dry_run,
    )
    if runtime.get("data_classification") not in ("PRODUCTION", "SYNTHETIC_CANARY"):
        raise ValueError("unsupported v2 data classification")
    runtime["_worksheet_targets"] = targets
    return _prepare_cell_bundle_with_configs(bundle_path, expected_bundle_sha256, runtime, configs)


def make_cell_sheet_client(credentials_info):
    """A dedicated no-write-retry client; legacy retry policy is not reused."""
    import gspread
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    from requests.adapters import HTTPAdapter

    credentials = Credentials.from_service_account_info(
        credentials_info, scopes=["https://www.googleapis.com/auth/spreadsheets",
                                  "https://www.googleapis.com/auth/drive.metadata.readonly"],
    )
    session = AuthorizedSession(credentials, max_refresh_attempts=0)
    session.mount("https://", HTTPAdapter(max_retries=0))
    client = gspread.Client(auth=credentials, session=session)
    client.set_timeout((10, 45))
    return client


def _account_preflight(store, config, batch, report_date, configuration_only):
    through = max(report_date, store.latest_report_date() or report_date)
    current = store.current_results(config.first_report_date, through)
    baseline = fingerprint(sorted([
        {key: str(row[key]) for key in ("account", "cell_code", "report_date", "generation_id", "calculation_id", "config_id")}
        for row in current
    ], key=lambda row: (row["report_date"], row["cell_code"])))
    if configuration_only:
        actual = {(row["cell_code"], row["generation_id"]) for row in current if str(row["report_date"]) == report_date}
        expected = {(item["identity"]["cell_code"], item["generation_id"]) for item in batch}
        if actual != expected:
            raise ValueError("configuration-only operation requires existing identical source generations")
    history = recalculate_stale_history(store, config, through, submitted_dates={report_date})
    identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
    facts = {(str(row["report_date"]), row["cell_code"]): {
        "identity": identities[row["cell_code"]], "report_date": str(row["report_date"]),
        "generation_id": row["generation_id"], "calculation_id": row["calculation_id"],
        "config_id": row["config_id"], "calculation": row["result"],
    } for row in current}
    for cohort in [*history, batch]:
        for item in cohort:
            facts[(item["report_date"], item["identity"]["cell_code"])] = item
    # Full coverage and numeric/formula inputs are pure-validated for every
    # account before the request's first mutation; manual inputs are read later.
    render_cell_matrix(build_cluster_report(config, list(facts.values()), through), dummy=True)
    return baseline, history


def run_cell_reporting(bundle_path, configuration_path, expected_configuration_sha256,
                       expected_bundle_sha256, *, dry_run=True, write_gsheet=True,
                       database_password=None, credentials_info=None, output_dir=".",
                       session_factory=cell_publication_session, sheet_client_factory=make_cell_sheet_client,
                       approved_configuration_sha256=None, configuration_only=False):
    if any(type(value) is not bool for value in (dry_run, write_gsheet, configuration_only)):
        raise ValueError("dry_run and write_gsheet must be explicit booleans")
    if not dry_run:
        _require_trusted_configuration(expected_configuration_sha256, approved_configuration_sha256)
    runtime, configs, manifest, batches = prepare_cell_bundle(
        bundle_path, configuration_path, expected_configuration_sha256, expected_bundle_sha256,
    )
    # All requested accounts are calculated before any DB/Google factory call.
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    prepared = directory / "cell_prepared.json"
    prepared.write_text(json.dumps({"report_date": manifest["report_date"], "batches": batches},
                                   sort_keys=True, allow_nan=False), encoding="utf-8")
    summary = {"report_date": manifest["report_date"], "accounts": [], "dry_run": dry_run}
    if dry_run:
        matrices = []
        for account, config in sorted(configs.items()):
            batch = batches[account]
            available = config.first_report_date == manifest["report_date"]
            if available:
                report = build_cluster_report(config, batch, manifest["report_date"])
                matrices.append(render_cell_matrix(report, dummy=True))
            summary["accounts"].append({"account": account, "status": "DRY_RUN",
                                        "cells": len(batch), "needs_prior_coverage": not available})
        (directory / "cell_design_matrices.json").write_text(json.dumps(matrices), encoding="utf-8")
        return summary
    if runtime.get("dummy_data_only"):
        raise ValueError("dummy configurations cannot select live persistence/publication")
    if database_password is None:
        raise ValueError("explicit cell database authentication required")
    target = CellDatabaseTarget(**runtime["database_target"])
    approved_db = {CellDatabaseTarget(**item) for item in runtime["approved_database_targets"]}
    target.validate(approved_db)
    targets = {}
    if write_gsheet:
        if credentials_info is None:
            raise ValueError("explicit Google credentials required")
        for account in configs:
            values = runtime["sheet_targets"][account]
            targets[account] = CellSheetTarget(**{**values, "finance_emails": tuple(values["finance_emails"])})
        allowed = {CellSheetTarget(**{**item, "finance_emails": tuple(item["finance_emails"])})
                   for item in runtime["approved_sheet_targets"]}
        for item in targets.values():
            item.validate(allowed)
        if credentials_info.get("client_email") not in {item.writer_email for item in targets.values()}:
            raise ValueError("Google credential identity differs from worksheet writer")
        if any(item.writer_email != credentials_info.get("client_email") for item in targets.values()):
            raise ValueError("every worksheet writer must match the supplied Google identity")
    client = None
    preflight = {}
    for account, config in sorted(configs.items()):
        sheet = targets.get(account)
        with session_factory(
            target, approved_db, account[:-2], sheet.spreadsheet_id if sheet else "",
            password=database_password, approved_sheet_ids={sheet.spreadsheet_id} if sheet else set(),
            write_gsheet=write_gsheet,
        ) as store:
            preflight[account], _ = _account_preflight(
                store, config, batches[account], manifest["report_date"], configuration_only,
            )
    for account, config in sorted(configs.items()):
        sheet = targets.get(account)
        with session_factory(
            target, approved_db, account[:-2], sheet.spreadsheet_id if sheet else "",
            password=database_password, approved_sheet_ids={sheet.spreadsheet_id} if sheet else set(),
            write_gsheet=write_gsheet,
        ) as store:
            current_token, history = _account_preflight(
                store, config, batches[account], manifest["report_date"], configuration_only,
            )
            if current_token != preflight[account]:
                raise ValueError("account history changed after global preflight; retry with current facts")
            if write_gsheet:
                if client is None:
                    client = sheet_client_factory(credentials_info)
                spreadsheet = client.open_by_key(sheet.spreadsheet_id)
                worksheet = spreadsheet.get_worksheet_by_id(sheet.worksheet_id)
                result = publish_cell_cluster(
                    store, sheet, allowed, ExistingSheetAdapter(spreadsheet, worksheet),
                    batches[account], config, changed_dates=[manifest["report_date"]],
                    additional_batches=history,
                    configuration_only=configuration_only,
                )
            else:
                def render_database(session):
                    through = max(manifest["report_date"], session.latest_report_date() or manifest["report_date"])
                    current = session.current_results(config.first_report_date, through)
                    identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
                    facts = [{"identity": identities[row["cell_code"]],
                              "report_date": str(row["report_date"]), "generation_id": row["generation_id"],
                              "calculation_id": row["calculation_id"], "config_id": row["config_id"],
                              "calculation": row["result"]} for row in current]
                    report = build_cluster_report(config, facts, through)
                    return {"start": config.first_report_date, "end": through, "snapshot": {"report": report}}
                result = store.commit_and_stage(batches[account], config.cells, render_database,
                                                write_gsheet=False, additional_batches=history)
            summary["accounts"].append({"account": account, "cells": len(batches[account]), **result})
    return summary


def _v2_fact_rows(config, rows):
    identities = {cell.cell_code: cell.to_dict() for cell in config.cells}
    return [{"identity": identities[row["cell_code"]], "report_date": str(row["report_date"]),
             "generation_id": row["generation_id"], "calculation_id": row["calculation_id"],
             "config_id": row["config_id"], "rules_version": config.rules_version,
             "calculation": row["result"]} for row in rows]


def run_cell_reporting_v2(bundle_path, configuration_path, expected_configuration_sha256,
                          expected_bundle_sha256, *, dry_run=True, write_gsheet=True,
                          database_password=None, credentials_info=None, output_dir=".",
                          approved_configuration_sha256, configuration_only=False,
                          session_factory=cell_publication_session,
                          sheet_client_factory=make_cell_sheet_client):
    """Distinct config-v2 reporting entrypoint; old cell-reporting v1 is unchanged."""
    if any(type(value) is not bool for value in (dry_run, write_gsheet, configuration_only)):
        raise ValueError("dry_run, write_gsheet and configuration_only must be explicit booleans")
    runtime, configs, manifest, batches = prepare_cell_bundle_v2(
        bundle_path, configuration_path, expected_configuration_sha256,
        expected_bundle_sha256, approved_configuration_sha256=approved_configuration_sha256,
        dry_run=dry_run,
    )
    targets = runtime["_worksheet_targets"]
    spreadsheet_id = runtime["spreadsheet_id"]
    legacy_id = runtime["legacy_spreadsheet_id"]
    validate_output_targets(spreadsheet_id, legacy_id, configs, targets)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 2, "report_date": manifest["report_date"],
              "data_classification": runtime["data_classification"],
              "spreadsheet_id": spreadsheet_id, "dry_run": dry_run, "accounts": []}

    if dry_run:
        all_matrices = {}
        for account, config in sorted(configs.items()):
            batch = batches[account]
            if manifest["report_date"] == config.first_report_date:
                matrices = build_cell_cluster_matrices_v2(
                    config, batch, manifest["report_date"], dummy=True,
                )
            else:
                invoice = build_cell_invoice_matrix(config, batch, dummy=True)
                details = {kind: build_cell_detail_history(config, batch, kind)
                           for kind in ("unusual", "reversal")}
                matrices = {"invoice": invoice, **details,
                            "summary": {"status": "NEEDS_PRIOR_HISTORY",
                                        "first_report_date": config.first_report_date}}
                for role, matrix in matrices.items():
                    if role != "summary":
                        bind_cell_style_contract(role, matrix)
            all_matrices[account] = matrices
            result["accounts"].append({"account": account, "status": "DRY_RUN",
                                       "cells": len(batch)})
        (output / "cell_v2_matrices.json").write_text(
            json.dumps(all_matrices, sort_keys=True, allow_nan=False), encoding="utf-8",
        )
        return result

    if runtime.get("dummy_data_only"):
        raise ValueError("dummy-only configuration cannot select live DB/Sheets")
    if not database_password:
        raise ValueError("explicit v2 database credential is required")
    target = CellDatabaseTarget(**runtime["database_target"])
    approved_db = {CellDatabaseTarget(**item) for item in runtime["approved_database_targets"]}
    target.validate(approved_db)
    if write_gsheet:
        if not credentials_info or credentials_info.get("client_email") != runtime["writer_email"]:
            raise ValueError("the explicit v2 spreadsheet writer credential is required")
        if any(item.writer_email != runtime["writer_email"]
               for account in targets.values() for item in account.values()):
            raise ValueError("v2 worksheet writer differs from pinned config")

    # Load and validate every cluster's complete history before opening a write
    # session or issuing any Google mutation for the run.
    preflight = {}
    for account, config in sorted(configs.items()):
        with session_factory(target, {target}, config.account[:-2], spreadsheet_id,
                             password=database_password,
                             approved_sheet_ids={spreadsheet_id} if write_gsheet else set(),
                             write_gsheet=write_gsheet) as store:
            baseline, history = _account_preflight(
                store, config, batches[account], manifest["report_date"], configuration_only,
            )
            through = max(manifest["report_date"], store.latest_report_date() or manifest["report_date"])
            facts = _v2_fact_rows(config, store.current_results(config.first_report_date, through))
            keyed = {(item["report_date"], item["identity"]["cell_code"]): item for item in facts}
            for cohort in [*history, batches[account]]:
                for item in cohort:
                    keyed[(item["report_date"], item["identity"]["cell_code"])] = item
            complete_facts = list(keyed.values())
            # Validate every output projection at the global barrier. The summary
            # uses current manual inputs later under the publication guard.
            build_cell_cluster_matrices_v2(
                config, complete_facts, max(item["report_date"] for item in complete_facts),
                dummy=True,
            )
            preflight[account] = baseline

    workbook = None
    if write_gsheet:
        workbook = sheet_client_factory(credentials_info).open_by_key(spreadsheet_id)
        for account, config in sorted(configs.items()):
            with session_factory(target, approved_db, account[:-2], spreadsheet_id,
                                 password=database_password, approved_sheet_ids={spreadsheet_id},
                                 write_gsheet=True) as store:
                adapters = {role: ExistingSheetAdapter(workbook,
                    workbook.get_worksheet_by_id(item.worksheet_id)) for role, item in targets[account].items()}
                preflight_cell_cluster_v2(store, spreadsheet_id, targets[account], adapters, account[:-2],
                                         allow_legacy_migration=configuration_only)

    for account, config in sorted(configs.items()):
        with session_factory(target, {target}, config.account[:-2], spreadsheet_id,
                             password=database_password,
                             approved_sheet_ids={spreadsheet_id} if write_gsheet else set(),
                             write_gsheet=write_gsheet) as store:
            baseline, history = _account_preflight(
                store, config, batches[account], manifest["report_date"], configuration_only,
            )
            if baseline != preflight[account]:
                raise ValueError("v2 cluster state changed after global no-write preflight")
            if write_gsheet:
                adapters = {role: ExistingSheetAdapter(
                    workbook, workbook.get_worksheet_by_id(item.worksheet_id),
                ) for role, item in targets[account].items()}
                result_for_cluster = publish_cell_cluster_v2(
                    store, spreadsheet_id, targets[account], adapters, batches[account], config,
                    changed_dates=[manifest["report_date"]], additional_batches=history,
                    configuration_only=configuration_only,
                )
            else:
                def render_db(session):
                    through = max(manifest["report_date"], session.latest_report_date() or manifest["report_date"])
                    current = _v2_fact_rows(config, session.current_results(config.first_report_date, through))
                    keyed = {(item["report_date"], item["identity"]["cell_code"]): item for item in current}
                    for cohort in [*history, batches[account]]:
                        for item in cohort:
                            keyed[(item["report_date"], item["identity"]["cell_code"])] = item
                    facts = list(keyed.values())
                    matrices = build_cell_cluster_matrices_v2(config, facts,
                                                              max(item["report_date"] for item in facts))
                    worksheets = {role: {"worksheet_id": targets[account][role].worksheet_id,
                        "title": targets[account][role].title,
                        "values": _v2_publication_values(role, matrix), "matrix": matrix}
                        for role, matrix in matrices.items()}
                    source = {}
                    for item in facts:
                        source.setdefault(item["report_date"], []).append(item["calculation_id"])
                    return {"start": config.first_report_date, "end": max(source),
                        "snapshot": {"schema_version": 2, "spreadsheet_id": spreadsheet_id,
                                     "worksheets": worksheets,
                                     "source_calculations": {key: fingerprint(sorted(value))
                                         for key, value in source.items()}}}
                result_for_cluster = store.commit_and_stage(
                    batches[account], config.cells, render_db, write_gsheet=False,
                    additional_batches=history,
                )
            result["accounts"].append({"account": account, "cells": len(batches[account]),
                                       **result_for_cluster})
    return result


def _v2_publication_values(role, matrix):
    values = matrix.get("values", matrix.get("rows"))
    if not isinstance(values, list) or not values or len({len(row) for row in values}) != 1:
        raise ValueError(f"invalid v2 {role} output matrix")
    return values


def decode_cell_google_credentials(value):
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = json.loads(base64.b64decode(value, validate=True))
    if not isinstance(parsed, dict):
        raise ValueError("Google credential payload must be an object")
    return parsed
