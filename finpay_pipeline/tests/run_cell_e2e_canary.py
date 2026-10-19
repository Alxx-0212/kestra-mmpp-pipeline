"""Manually authorized synthetic canary. Fixed isolated targets; no production mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import zipfile
from pathlib import Path

import psycopg
from psycopg import sql

from finpay_pipeline.cell_contracts import configured_cells, fingerprint, roster_fingerprint
from finpay_pipeline.cell_database import CELL_MIGRATION
from finpay_pipeline.cell_publication import ExistingSheetAdapter, GUARD_PREFIX, MANAGED_PREFIX, _editors_match
from finpay_pipeline.cell_workflow import make_cell_sheet_client, run_cell_reporting
from finpay_pipeline.digipos_scraper import _write_xhr_export

COPY_ID = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
TAB_ID = 1900150013
TAB_NAME = "E2E 412110_B - SYNTHETIC"
DATABASE = "finpay_cell_e2e"
ROLE = "finpay_cell_test"
SCHEMA = "cell_test"
PORT = 55433
DATES = ("2026-10-13", "2026-10-14", "2026-10-15")


def _dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def prepare_password(root):
    root.mkdir(parents=True, exist_ok=True)
    target = root / "db-password"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(secrets.token_urlsafe(32))
    print("Private disposable database password prepared; value not printed.")


def _counts(connection):
    names = ("sources", "ledger", "current", "calculations")
    return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
        sql.Identifier(SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"),
    )).fetchone()[0] for name in names}


def _configuration(writer):
    target = {"host": "127.0.0.1", "port": PORT, "database": DATABASE, "schema": SCHEMA, "user": ROLE}
    sheet = {"spreadsheet_id": COPY_ID, "worksheet_id": TAB_ID,
             "writer_email": writer, "finance_emails": [writer]}
    config = {
        "schema_version": 1, "dummy_data_only": False,
        "data_classification": "SYNTHETIC_CANARY_NOT_PRODUCTION",
        "accounts": [{"account": "412110_B", "cell_codes": ["412111", "412112"],
            "first_report_date": DATES[0], "opening_boundary": DATES[0] + "T00:00:00+08:00",
            "opening_balances": {"412111": 1000000, "412112": 2000000},
            "roster_version": "SYNTHETIC-CANARY-20261015", "template_version": "finpay-cell-cashflow-v2"}],
        "database_target": target, "approved_database_targets": [target],
        "sheet_targets": {"412110_B": sheet}, "approved_sheet_targets": [sheet],
    }
    return config


def _inputs(root, writer):
    config = _configuration(writer)
    config_path = root / "approved-canary-configuration.json"
    _dump(config_path, config)
    cells = configured_cells({"412110_B": ("412111", "412112")})
    bundles = []
    for index, day in enumerate(DATES):
        folder = root / day
        folder.mkdir(exist_ok=True)
        entries = []
        for slot, cell in enumerate(cells):
            amount = (slot + 1) * 100000 + index * 10000
            rows = [[1, day + " 12:00:00", f"DUMMY_{cell.cell_code}_{index}_SALE",
                     1000000, amount, 0, 1000000 + amount, "Credit", "RECHARGE", "-", "Transaksi Sellthru"]]
            if index:
                bank = 100000 + (index - 1) * 10000 if slot == 0 else 200000
                rows.append([2, day + " 09:00:00", f"DUMMY_{cell.cell_code}_{index}_BANK",
                             1000000, bank, 0, 1000000 + bank, "Credit", "CASHOUT APOLLO", "-",
                             "APOLLO MRC100 " + day[8:10] + "-10-2026"])
            if index == 1 and slot == 1:
                rows = []
            entry = {**cell.to_dict(), "report_date": day, "status": "validated" if rows else "empty",
                     "validated_rows": len(rows), "footer_total": len(rows), "artifact": None}
            if rows:
                entry["artifact"] = cell.account + "-" + cell.cell_code + ".xlsx"
                path = folder / entry["artifact"]
                _write_xhr_export(path, rows)
                entry["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                entry["source_sha256"] = fingerprint({**cell.to_dict(), "report_date": day, "data": []})
            entries.append(entry)
        manifest = {"schema_version": 1, "report_date": day,
                    "roster_fingerprint": roster_fingerprint(cells), "cells": entries}
        bundle = folder / "cell_sources.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for entry in entries:
                if entry["artifact"]:
                    archive.write(folder / entry["artifact"], arcname=entry["artifact"])
        bundles.append(bundle)
    return config_path, bundles


def reconcile_confirmed_guard(root, credential_file, password_file, report_sha, writer_sha):
    """Only the observed completed guard/default-field mismatch, never a timeout."""
    previous_bytes = (root / "canary-result.json").read_bytes()
    if hashlib.sha256(previous_bytes).hexdigest() != report_sha:
        raise RuntimeError("reconciliation report identity mismatch")
    previous = json.loads(previous_bytes)
    if not (previous.get("diagnostic") == "temporary input protection was not confirmed"
            and previous.get("error_class") == "ValueError" and previous.get("test_tab_created")
            and previous.get("copy_id") == COPY_ID and previous.get("test_tab_id") == TAB_ID
            and previous.get("checks", {}).get("db_rerun") == "PASS"):
        raise RuntimeError("this is not the confirmed no-data guard mismatch")
    info = json.loads(Path(credential_file).read_text())
    if hashlib.sha256(info["client_email"].encode()).hexdigest() != writer_sha:
        raise RuntimeError("reconciliation writer identity mismatch")
    connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname=DATABASE, user=ROLE,
                                 password=Path(password_file).read_text().strip(), autocommit=True)
    try:
        if connection.execute("SELECT current_database(),current_user").fetchone() != (DATABASE, ROLE):
            raise RuntimeError("reconciliation database identity mismatch")
        for namespace, value in (("finpay-cell-publisher-v1", COPY_ID), ("finpay-cell-cluster-v1", "412110")):
            if not connection.execute("SELECT pg_try_advisory_lock(hashtext(%s),hashtext(%s))", (namespace, value)).fetchone()[0]:
                raise RuntimeError("reconciliation resource is locked")
        if _counts(connection) != previous["final_db_counts"]:
            raise RuntimeError("canary source state changed after failed guard verification")
        if connection.execute(sql.SQL("SELECT count(*) FROM {}.finpay_cell_ledger_v1 WHERE transaction_id NOT LIKE 'DUMMY_%'").format(sql.Identifier(SCHEMA))).fetchone()[0]:
            raise RuntimeError("non-synthetic evidence found")
        if connection.execute(sql.SQL("SELECT count(*) FROM {}.finpay_cell_publications_v1 WHERE target_id=%s").format(sql.Identifier(SCHEMA)), (COPY_ID,)).fetchone()[0]:
            raise RuntimeError("Sheet publication already staged; refusing this recovery path")
        operation = connection.execute(sql.SQL(
            "SELECT operation_id,status,error_class FROM {}.finpay_cell_target_operations_v1 WHERE target_id=%s"
        ).format(sql.Identifier(SCHEMA)), (COPY_ID,)).fetchone()
        if not operation or operation[1:] != ("UNKNOWN", "ValueError"):
            raise RuntimeError("unexpected operation state")
        spreadsheet = make_cell_sheet_client(info).open_by_key(COPY_ID)
        worksheet = spreadsheet.get_worksheet_by_id(TAB_ID)
        adapter = ExistingSheetAdapter(spreadsheet, worksheet)
        metadata = adapter.metadata()
        if worksheet.title != TAB_NAME or metadata.get("developerMetadata") or any(any(row) for row in adapter.read(200, 20)):
            raise RuntimeError("test worksheet is not the empty pre-publication resource")
        guards = metadata.get("protectedRanges", [])
        if len(guards) != 1:
            raise RuntimeError("unexpected protection scope")
        guard = guards[0]
        if not (guard.get("description") == GUARD_PREFIX + operation[0]
                and guard.get("range") == {"sheetId": TAB_ID} and not guard.get("unprotectedRanges")
                and guard.get("warningOnly", False) is False
                and _editors_match(guard.get("editors", {}), [info["client_email"]], metadata.get("implicitOwnerEmails", []))):
            raise RuntimeError("known completed guard does not match approved scope")
        guard_id = guard["protectedRangeId"]
        spreadsheet.batch_update({"requests": [{"deleteProtectedRange": {"protectedRangeId": guard_id}}]})
        if any(item["protectedRangeId"] == guard_id for item in adapter.metadata().get("protectedRanges", [])):
            raise RuntimeError("known guard deletion was not confirmed")
        changed = connection.execute(sql.SQL(
            "UPDATE {}.finpay_cell_target_operations_v1 SET status='IDLE',error_class='RECONCILED_CONFIRMED_METADATA_DEFAULT' "
            "WHERE target_id=%s AND operation_id=%s AND status='UNKNOWN' AND error_class='ValueError' RETURNING operation_id"
        ).format(sql.Identifier(SCHEMA)), (COPY_ID, operation[0])).fetchone()
        if not changed:
            raise RuntimeError("operation changed during known-case reconciliation")
        _dump(root / "canary-guard-reconciliation.json", {
            "case": "completed guard response/local metadata canonicalization mismatch",
            "prior_report_sha256": report_sha, "operation_id": operation[0], "guard_id": guard_id,
            "proof": "runner exited after successful guard and metadata calls; no retries, no data or publication marker",
            "blank_test_tab_confirmed": True, "sheet_publication_count": 0, "status": "RECONCILED",
        })
        print("Only the confirmed completed guard mismatch was reconciled; no report data changed.")
    finally:
        connection.close()


def reconcile_rejected_restoration(root, credential_file, password_file, report_sha, writer_sha):
    """Only the observed HTTP400 atomic rejection; never a timeout/unknown apply."""
    data = (root / "canary-result.json").read_bytes()
    previous = json.loads(data)
    if not (hashlib.sha256(data).hexdigest() == report_sha and previous.get("http_status") == 400
            and "already has sheet protection" in previous.get("api_message", "")
            and previous.get("error_class") == "APIError" and previous.get("test_tab_created")
            and previous.get("copy_id") == COPY_ID and previous.get("test_tab_id") == TAB_ID):
        raise RuntimeError("not the known rejected protection-restoration batch")
    info = json.loads(Path(credential_file).read_text())
    if hashlib.sha256(info["client_email"].encode()).hexdigest() != writer_sha:
        raise RuntimeError("restoration reconciliation writer mismatch")
    connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname=DATABASE, user=ROLE,
                                 password=Path(password_file).read_text().strip(), autocommit=True)
    try:
        if connection.execute("SELECT current_database(),current_user").fetchone() != (DATABASE, ROLE):
            raise RuntimeError("reconciliation database mismatch")
        for namespace, value in (("finpay-cell-publisher-v1", COPY_ID), ("finpay-cell-cluster-v1", "412110")):
            if not connection.execute("SELECT pg_try_advisory_lock(hashtext(%s),hashtext(%s))", (namespace, value)).fetchone()[0]:
                raise RuntimeError("reconciliation resource is locked")
        if _counts(connection) != previous["final_db_counts"]:
            raise RuntimeError("canary evidence changed")
        operation = connection.execute(sql.SQL(
            "SELECT operation_id,status,error_class FROM {}.finpay_cell_target_operations_v1 WHERE target_id=%s"
        ).format(sql.Identifier(SCHEMA)), (COPY_ID,)).fetchone()
        publication = connection.execute(sql.SQL(
            "SELECT epoch,status,snapshot_fingerprint,snapshot FROM {}.finpay_cell_publications_v1 "
            "WHERE target_id=%s ORDER BY epoch DESC LIMIT 1"
        ).format(sql.Identifier(SCHEMA)), (COPY_ID,)).fetchone()
        if not operation or operation[1:] != ("UNKNOWN", "APIError") or not publication or publication[1] != "UNKNOWN":
            raise RuntimeError("unexpected reconciliation operation/publication state")
        spreadsheet = make_cell_sheet_client(info).open_by_key(COPY_ID)
        adapter = ExistingSheetAdapter(spreadsheet, spreadsheet.get_worksheet_by_id(TAB_ID))
        metadata = adapter.metadata()
        guards = metadata.get("protectedRanges", [])
        if len(guards) != 1 or guards[0].get("description") != GUARD_PREFIX + operation[0]:
            raise RuntimeError("unexpected guard before rejected-batch reconciliation")
        guard = guards[0]
        if (guard.get("range") != {"sheetId": TAB_ID} or guard.get("warningOnly", False) is not False
                or guard.get("unprotectedRanges") or not _editors_match(guard.get("editors", {}),
                    [info["client_email"]], metadata.get("implicitOwnerEmails", []))):
            raise RuntimeError("guard policy mismatch")
        marker = metadata.get("developerMetadata", [])
        expected_marker = {"status": "PENDING", "epoch": publication[0], "fingerprint": publication[2]}
        if len(marker) != 1 or json.loads(marker[0]["metadataValue"]) != expected_marker:
            raise RuntimeError("pending generation does not match native DB snapshot")
        expected = publication[3]["matrix"]["values"]
        actual = adapter.read(len(expected), len(expected[0]))
        for row_index, row in enumerate(expected):
            for column, value in enumerate(row):
                found = actual[row_index][column] if row_index < len(actual) and column < len(actual[row_index]) else ""
                if found != value:
                    raise RuntimeError("pending data differs from immutable staged snapshot")
        # Keep the test tab read-only. Convert the confirmed guard to managed
        # protection instead of leaving an unprotected interval before retry.
        spreadsheet.batch_update({"requests": [{"updateProtectedRange": {
            "protectedRange": {"protectedRangeId": guard["protectedRangeId"],
                               "description": MANAGED_PREFIX + "COMPUTED"}, "fields": "description",
        }}]})
        if not any(item.get("protectedRangeId") == guard["protectedRangeId"]
                   and item.get("description") == MANAGED_PREFIX + "COMPUTED"
                   for item in adapter.metadata().get("protectedRanges", [])):
            raise RuntimeError("read-only protection conversion not confirmed")
        with connection.transaction():
            connection.execute(sql.SQL(
                "UPDATE {}.finpay_cell_publications_v1 SET status='SUPERSEDED',error_class='RECONCILED_ATOMIC_HTTP400' "
                "WHERE cluster_id='412110' AND target_id=%s AND epoch=%s AND status='UNKNOWN'"
            ).format(sql.Identifier(SCHEMA)), (COPY_ID, publication[0]))
            connection.execute(sql.SQL(
                "UPDATE {}.finpay_cell_publication_dates_v1 SET status='SUPERSEDED' "
                "WHERE cluster_id='412110' AND target_id=%s AND epoch=%s"
            ).format(sql.Identifier(SCHEMA)), (COPY_ID, publication[0]))
            connection.execute(sql.SQL(
                "UPDATE {}.finpay_cell_target_operations_v1 SET status='IDLE',error_class='RECONCILED_ATOMIC_HTTP400' "
                "WHERE target_id=%s AND operation_id=%s AND status='UNKNOWN'"
            ).format(sql.Identifier(SCHEMA)), (COPY_ID, operation[0]))
        _dump(root / "canary-restoration-reconciliation.json", {
            "case": "explicit HTTP400 rejected the complete restoration batch; no in-flight mutation",
            "prior_report_sha256": report_sha, "superseded_epoch": publication[0],
            "operation_id": operation[0], "guard_id": guard["protectedRangeId"],
            "immutable_data_readback": "MATCH", "read_only_protection_preserved": True,
            "status": "RECONCILED",
        })
        print("Only the confirmed HTTP400 test restoration was reconciled; data and protection retained.")
    finally:
        connection.close()


def execute(root, credential_file, password_file, approved_config_sha256, approved_writer_sha256,
            approved_bundle_sha256, approved_existing_report_sha256=None):
    root.mkdir(parents=True, exist_ok=True)
    report = {"scope": "USER_APPROVED_SYNTHETIC_CANARY", "production_touched": False,
              "cluster": "412110_B", "dates": DATES, "copy_id": COPY_ID,
              "test_tab_id": TAB_ID, "test_tab_title": TAB_NAME, "checks": {}}
    result_path = root / "canary-result.json"
    previous = None
    if approved_existing_report_sha256:
        if hashlib.sha256(result_path.read_bytes()).hexdigest() != approved_existing_report_sha256:
            raise RuntimeError("prior owned canary report differs from approved recovery snapshot")
        previous = json.loads(result_path.read_text())
        if not (previous.get("scope") == report["scope"] and previous.get("copy_id") == COPY_ID
                and previous.get("test_tab_id") == TAB_ID and previous.get("test_tab_created")
                and previous.get("configuration_sha256") == approved_config_sha256
                and previous.get("checks", {}).get("native_migration") == "PASS"):
            raise RuntimeError("existing test resource ownership is not established")
    connection = None
    try:
        password = Path(password_file).read_text().strip()
        credential_info = json.loads(Path(credential_file).read_text())
        writer = credential_info["client_email"]
        if hashlib.sha256(writer.encode()).hexdigest() != approved_writer_sha256:
            raise RuntimeError("service-account identity differs from independently pinned principal")
        config = root / "approved-canary-configuration.json"
        config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
        if config_sha != approved_config_sha256 or json.loads(config.read_text()) != _configuration(writer):
            raise RuntimeError("canary configuration differs from approved snapshot/scope")
        bundles = [root / day / "cell_sources.zip" for day in DATES]
        if set(approved_bundle_sha256) != set(DATES):
            raise RuntimeError("all three independent bundle pins are required")
        for bundle in bundles:
            if hashlib.sha256(bundle.read_bytes()).hexdigest() != approved_bundle_sha256[bundle.parent.name]:
                raise RuntimeError("synthetic bundle differs from approved snapshot")
        report["configuration_sha256"] = config_sha
        connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname=DATABASE,
                                     user=ROLE, password=password, autocommit=True)
        identity = connection.execute("SELECT current_database(),current_user").fetchone()
        if identity != (DATABASE, ROLE):
            raise RuntimeError("isolated database identity mismatch")
        exists = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMA,)).fetchone()
        if exists and previous is None:
            raise RuntimeError("test schema already exists; refusing implicit reset")
        if previous is not None and not exists:
            raise RuntimeError("previous owned test database disappeared")
        if not exists:
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
                connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
                connection.execute(CELL_MIGRATION.read_text(), prepare=False)
        report["checks"]["native_migration"] = "PASS"

        def run(bundle, sheets):
            return run_cell_reporting(
                bundle, config, config_sha, approved_bundle_sha256[bundle.parent.name],
                approved_configuration_sha256=config_sha, dry_run=False, write_gsheet=sheets,
                database_password=password, credentials_info=credential_info if sheets else None,
                output_dir=root / ("sheet" if sheets else "db") / bundle.parent.name,
            )

        for bundle in bundles:
            run(bundle, False)
        before = _counts(connection)
        if before != {"sources": 6, "ledger": 8, "current": 6, "calculations": 6}:
            raise AssertionError("unexpected isolated ingestion counts")
        run(bundles[-1], False)
        if _counts(connection) != before:
            raise AssertionError("database rerun duplicated evidence")
        report["checks"].update(db_ingestion="PASS", db_rerun="PASS", db_counts=before)
        _dump(result_path, report)

        client = make_cell_sheet_client(credential_info)
        spreadsheet = client.open_by_key(COPY_ID)
        metadata = spreadsheet.fetch_sheet_metadata()
        if metadata["spreadsheetId"] != COPY_ID or metadata["properties"]["title"] != "Salinan dari Monitoring Finpay & LinkAja":
            raise RuntimeError("copied spreadsheet identity mismatch")
        existing = [item for item in metadata["sheets"] if item["properties"]["sheetId"] == TAB_ID
                    or item["properties"]["title"] == TAB_NAME]
        if existing and (previous is None or len(existing) != 1
                         or existing[0]["properties"]["sheetId"] != TAB_ID
                         or existing[0]["properties"]["title"] != TAB_NAME):
            raise RuntimeError("approved test tab already exists; refusing overwrite")
        if not existing:
            spreadsheet.batch_update({"requests": [{"addSheet": {"properties": {
                "sheetId": TAB_ID, "title": TAB_NAME, "gridProperties": {"rowCount": 200, "columnCount": 20},
            }}}]})
        report["test_tab_created"] = True
        _dump(result_path, report)
        for bundle in bundles:
            run(bundle, True)
        stable = _counts(connection)
        run(bundles[-1], True)
        if _counts(connection) != stable:
            raise AssertionError("sheet rerun duplicated immutable evidence")
        report["checks"].update(sheet_publish="PASS", sheet_rerun="PASS")
        publication = connection.execute(sql.SQL(
            "SELECT status,snapshot FROM {}.finpay_cell_publications_v1 WHERE target_id=%s ORDER BY epoch DESC LIMIT 1"
        ).format(sql.Identifier(SCHEMA)), (COPY_ID,)).fetchone()
        if publication[0] != "COMPLETE":
            raise AssertionError("native DB publication is not complete")
        matrix = publication[1]["matrix"]
        worksheet = spreadsheet.get_worksheet_by_id(TAB_ID)
        actual = worksheet.get(f"A1:T{len(matrix['values'])}", value_render_option="UNFORMATTED_VALUE")
        for row_index, row in enumerate(matrix["numeric_preview"]):
            for column, expected in enumerate(row):
                found = actual[row_index][column] if row_index < len(actual) and column < len(actual[row_index]) else ""
                if ("" if found is None else found) != expected:
                    raise AssertionError(f"evaluated value mismatch at row {row_index + 1}, column {column + 1}")
        report["checks"]["native_formula_values_numbers_text_and_blanks"] = "PASS"
        bank = next(item for item in matrix["editable_inputs"]
                    if item["report_date"] == "2026-10-14" and item["cell_code"] == "412111")
        spreadsheet.batch_update({"requests": [{"updateCells": {
            "range": {"sheetId": TAB_ID, "startRowIndex": bank["row"] - 1, "endRowIndex": bank["row"],
                      "startColumnIndex": bank["column"] - 1, "endColumnIndex": bank["column"]},
            "rows": [{"values": [{"userEnteredValue": {"numberValue": 777}}]}], "fields": "userEnteredValue",
        }}]})
        run(bundles[-1], True)
        reset = connection.execute(sql.SQL(
            "SELECT previous_value FROM {}.finpay_cell_manual_resets_v1 WHERE cell_code='412111' "
            "AND report_date='2026-10-14' ORDER BY epoch DESC LIMIT 1"
        ).format(sql.Identifier(SCHEMA))).fetchone()
        if not reset or reset[0] != 777 or _counts(connection) != before:
            raise AssertionError("native manual-reset audit or rerun identity failed")
        report["checks"]["manual_reset_and_audit"] = "PASS"
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error_class"] = type(exc).__name__
        response = getattr(exc, "response", None)
        if response is not None:
            report["http_status"] = response.status_code
            try:
                message = response.json().get("error", {}).get("message", "")
                report["api_message"] = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<email>", message)[:600]
            except Exception:
                pass
        elif isinstance(exc, (AssertionError, RuntimeError, ValueError)):
            report["diagnostic"] = str(exc)[:400]
        if connection is not None and not connection.closed:
            try:
                report["final_db_counts"] = _counts(connection)
            except Exception:
                pass
        raise
    finally:
        _dump(result_path, report)
        if connection is not None:
            connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--prepare-password", action="store_true")
    parser.add_argument("--prepare-inputs", action="store_true")
    parser.add_argument("--execute-approved-canary", action="store_true")
    parser.add_argument("--credential-file")
    parser.add_argument("--db-password-file")
    parser.add_argument("--approved-config-sha256")
    parser.add_argument("--approved-writer-sha256")
    parser.add_argument("--approved-bundle-sha256", action="append", default=[], help="DATE=SHA256, once per date")
    parser.add_argument("--approved-existing-report-sha256")
    parser.add_argument("--reconcile-confirmed-guard", action="store_true")
    parser.add_argument("--reconcile-rejected-restoration", action="store_true")
    args = parser.parse_args()
    if args.prepare_password:
        prepare_password(Path(args.output_dir))
    elif args.prepare_inputs and args.credential_file:
        root = Path(args.output_dir)
        writer = json.loads(Path(args.credential_file).read_text())["client_email"]
        config, bundles = _inputs(root, writer)
        print(json.dumps({"configuration_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                          "writer_sha256": hashlib.sha256(writer.encode()).hexdigest(),
                          "source_bundle_sha256": {bundle.parent.name: hashlib.sha256(bundle.read_bytes()).hexdigest()
                                                   for bundle in bundles},
                          "side_effects": "local synthetic files only; no clients or connections"}))
    elif (args.reconcile_confirmed_guard and args.credential_file and args.db_password_file
          and args.approved_existing_report_sha256 and args.approved_writer_sha256):
        reconcile_confirmed_guard(Path(args.output_dir), args.credential_file, args.db_password_file,
                                  args.approved_existing_report_sha256, args.approved_writer_sha256)
    elif (args.reconcile_rejected_restoration and args.credential_file and args.db_password_file
          and args.approved_existing_report_sha256 and args.approved_writer_sha256):
        reconcile_rejected_restoration(Path(args.output_dir), args.credential_file, args.db_password_file,
                                       args.approved_existing_report_sha256, args.approved_writer_sha256)
    elif (args.execute_approved_canary and args.credential_file and args.db_password_file
          and args.approved_config_sha256 and args.approved_writer_sha256):
        pins = {}
        for value in args.approved_bundle_sha256:
            if not re.fullmatch(r"2026-10-1[345]=[a-f0-9]{64}", value):
                parser.error("approved bundle pin must be an exact canary DATE=SHA256")
            day, digest = value.split("=", 1)
            if day in pins:
                parser.error("duplicate approved bundle date")
            pins[day] = digest
        try:
            execute(Path(args.output_dir), args.credential_file, args.db_password_file,
                    args.approved_config_sha256, args.approved_writer_sha256, pins,
                    args.approved_existing_report_sha256)
        except Exception:
            print("Canary stopped; sanitized details are in canary-result.json.")
            return 1
        print("Approved synthetic canary passed; detailed results are in canary-result.json.")
    else:
        parser.error("explicit preparation or approved execution arguments required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
