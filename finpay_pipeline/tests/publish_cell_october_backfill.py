"""Serial persistence/publication for the approved October 1-17 backfill."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import traceback
import time
import uuid
from pathlib import Path

from finpay_pipeline.tests import run_cell_october_backfill as run

NOTICE = ("OCTOBER 1-17 2026 BACKFILL. Opening declared by operator: September30 closing balance ZERO, "
          "October1 00:00 WITA opening ZERO for new cluster cells. COMPLETE means verified publication, not production commissioning.")


def dump_once(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def receipt(root, pin):
    if not pin or run.sha(root / "sources.json") != pin:
        raise ValueError("independent backfill receipt pin mismatch")
    result = json.loads((root / "result.json").read_text())
    sources = json.loads((root / "sources.json").read_text())
    allowed_status = result.get("status") == "ACQUIRED" or (
        result.get("status") == "PUBLISHING" and (root / "original-tab-metadata.json").exists()) or (
        result.get("status") == "FAILED"
        and result.get("source_receipt_sha256") == pin
        and (root / "original-tab-metadata.json").exists())
    if not allowed_status or result.get("source_summary") != sources or set(sources) != set(run.DATES):
        raise ValueError("complete acquired backfill receipt required")
    if sum(item["cells"] for item in sources.values()) != 306:
        raise ValueError("backfill source-cell coverage incomplete")
    for day in run.DATES:
        if run.sha(root / "sources" / day / "cell_sources_v2.zip") != sources[day]["sha256"]:
            raise ValueError("immutable backfill bundle changed")
    return sources


def reconcile_known_pending(root, connection, target, runtime, configs, targets, client, workbook,
                            password, expected_counts):
    """Resume only the acknowledged style-readback failure on PKY epoch 1."""
    from psycopg import sql
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_database import cell_publication_session
    from finpay_pipeline.cell_publication import (ExistingSheetAdapter, GUARD_PREFIX, MAX_REQUEST_BYTES,
        _editors_match, _marker_request, _protection_requests, _status_requests, _verify_final_protections,
        _verify_marker, _padded)
    from finpay_pipeline.cell_publication_v2 import _verify_style, _verify_values

    schema = sql.Identifier(run.SCHEMA)
    original_path = root / "original-tab-metadata.json"
    if not original_path.exists():
        raise ValueError("known pending recovery requires the captured original-tab baseline")
    original = json.loads(original_path.read_text())
    live_metadata = workbook.fetch_sheet_metadata()
    allowed = {item["worksheet_id"] for group in run.TARGETS.values() for item in group.values()}
    live = {str(item["properties"]["sheetId"]): item for item in live_metadata["sheets"]}
    if set(live) != set(original) | {str(value) for value in allowed}:
        raise ValueError("known pending recovery found an unexpected tab set")
    if any(live.get(key) != value for key, value in original.items()):
        raise ValueError("pre-existing test workbook tab metadata changed")
    for account, group in run.TARGETS.items():
        for role, item in group.items():
            if live[str(item["worksheet_id"])]["properties"]["title"] != item["title"]:
                raise ValueError("known pending recovery found an alias ID/title mismatch")

    rows = connection.execute(sql.SQL("SELECT epoch,status,error_class,snapshot_fingerprint,snapshot "
        "FROM {}.finpay_cell_publications_v1 WHERE target_id=%s ORDER BY cluster_id,epoch").format(schema),
        (run.BOOK,)).fetchall()
    operations = connection.execute(sql.SQL("SELECT cluster_id,operation_id,status,error_class "
        "FROM {}.finpay_cell_target_operations_v1 WHERE target_id=%s").format(schema), (run.BOOK,)).fetchall()
    if (len(rows) != 1 or rows[0][:3] != (1, "PENDING", "ValueError")
        or len(operations) != 1 or operations[0][0] != "412110"
        or operations[0][2:] != ("IDLE", "ValueError") or counts_for_schema(connection) != expected_counts):
        raise ValueError("state differs from the specifically approved PKY style-readback failure")
    epoch, _, _, digest, snapshot = rows[0]
    if fingerprint({"cluster_id": "412110", "target_id": run.BOOK,
        "range_start": str(connection.execute(sql.SQL("SELECT range_start FROM {}.finpay_cell_publications_v1 WHERE cluster_id=%s AND target_id=%s AND epoch=1").format(schema),("412110",run.BOOK)).fetchone()[0]),
        "range_end": str(connection.execute(sql.SQL("SELECT range_end FROM {}.finpay_cell_publications_v1 WHERE cluster_id=%s AND target_id=%s AND epoch=1").format(schema),("412110",run.BOOK)).fetchone()[0]),
        "snapshot": snapshot}) != digest:
        raise ValueError("pending recovery snapshot fingerprint differs")

    old_operation = operations[0][1]
    config, role_targets = configs["412110_B"], targets["412110_B"]
    adapters, metas, guards = {}, {}, {}
    marker = {"status": "PENDING", "epoch": epoch, "fingerprint": digest}
    for role, saved in snapshot["worksheets"].items():
        approved = role_targets[role]
        if (saved["worksheet_id"], saved["title"]) != (approved.worksheet_id, approved.title):
            raise ValueError("saved pending role target mismatch")
        adapter = adapters[role] = ExistingSheetAdapter(workbook,
            workbook.get_worksheet_by_id(approved.worksheet_id))
        meta = metas[role] = adapter.metadata()
        if meta["properties"].get("title") != approved.title:
            raise ValueError("pending target title changed")
        _verify_marker(meta, marker)
        _verify_values(adapter, saved)
        _verify_style(adapter, saved["matrix"])
        guards_found = [item for item in meta.get("protectedRanges", [])
                        if item.get("description") == GUARD_PREFIX + old_operation]
        if (len(guards_found) != 1 or guards_found[0].get("range") != {"sheetId": approved.worksheet_id}
            or guards_found[0].get("warningOnly", False) or guards_found[0].get("unprotectedRanges")
            or not _editors_match(guards_found[0].get("editors", {}), [approved.writer_email], meta.get("implicitOwnerEmails", []))):
            raise ValueError("pending recovery full-sheet guard is missing or changed")
        if len(meta.get("protectedRanges", [])) != 1:
            raise ValueError("pending recovery found unexpected protection ranges")
        guards[role] = guards_found[0]["protectedRangeId"]

    recovery_id = uuid.uuid4().hex
    precheck_path, outcome_path = root / "pky-known-pending-precheck.json", root / "pky-known-pending-outcome.json"
    if precheck_path.exists() or outcome_path.exists():
        raise ValueError("pending recovery already attempted; no blind retry")
    precheck = {"status": "PRECHECKED", "cluster": "412110", "epoch": epoch,
        "old_operation": old_operation, "fingerprint": digest, "failed_reason": "ValueError",
        "all_four_values_and_styles": True, "all_four_full_sheet_guards": True,
        "db_counts": expected_counts, "existing_tabs_preserved": True}
    dump_once(precheck_path, precheck)
    uncertain = False

    def send(requests):
        nonlocal uncertain
        if len(json.dumps({"requests": requests}).encode()) > MAX_REQUEST_BYTES:
            raise ValueError("recovery request exceeds configured bound")
        uncertain = True
        response = workbook.batch_update({"requests": requests})
        uncertain = False
        return response

    try:
        with cell_publication_session(target, {target}, "412110", run.BOOK, password=password,
                                      approved_sheet_ids={run.BOOK}, write_gsheet=True) as store:
            with store.connection.transaction():
                store.begin_target_operation(recovery_id)
                changed = store.connection.execute(sql.SQL("UPDATE {}.finpay_cell_publications_v1 SET status='IN_FLIGHT' "
                    "WHERE cluster_id=%s AND target_id=%s AND epoch=%s AND status='PENDING' "
                    "AND error_class='ValueError' AND snapshot_fingerprint=%s RETURNING epoch").format(schema),
                    ("412110", run.BOOK, epoch, digest)).fetchone()
                if changed != (epoch,):
                    raise ValueError("pending publication compare-and-swap did not match")
            finish = []
            complete_marker = {"status": "COMPLETE", "epoch": epoch, "fingerprint": digest}
            for role, saved in snapshot["worksheets"].items():
                approved = role_targets[role].sheet_target()
                finish.extend(_protection_requests(approved, saved["matrix"], metas[role], guards[role]))
                finish.extend([_marker_request(approved, metas[role], complete_marker),
                               *_status_requests(approved, complete_marker)])
            send(finish)
            for role, saved in snapshot["worksheets"].items():
                meta = adapters[role].metadata()
                _verify_marker(meta, complete_marker)
                _verify_final_protections(role_targets[role].sheet_target(), saved["matrix"], meta)
                _verify_values(adapters[role], saved)
                _verify_style(adapters[role], saved["matrix"])
            store.complete(epoch, verified_fingerprint=digest)
            store.finish_target_operation(recovery_id)
    except Exception as exc:
        try:
            state = connection.execute(sql.SQL("SELECT status FROM {}.finpay_cell_publications_v1 "
                "WHERE cluster_id=%s AND target_id=%s AND epoch=%s").format(schema),
                ("412110", run.BOOK, epoch)).fetchone()
            if state and state[0] == "IN_FLIGHT":
                if uncertain:
                    connection.execute(sql.SQL("UPDATE {}.finpay_cell_publications_v1 SET status='UNKNOWN',error_class=%s "
                        "WHERE cluster_id=%s AND target_id=%s AND epoch=%s").format(schema),
                        (type(exc).__name__, "412110", run.BOOK, epoch))
                    connection.execute(sql.SQL("UPDATE {}.finpay_cell_target_operations_v1 SET status='UNKNOWN',error_class=%s "
                        "WHERE cluster_id=%s AND target_id=%s AND operation_id=%s").format(schema),
                        (type(exc).__name__, "412110", run.BOOK, recovery_id))
                else:
                    connection.execute(sql.SQL("UPDATE {}.finpay_cell_publications_v1 SET status='PENDING',error_class=%s "
                        "WHERE cluster_id=%s AND target_id=%s AND epoch=%s").format(schema),
                        (type(exc).__name__, "412110", run.BOOK, epoch))
                    connection.execute(sql.SQL("UPDATE {}.finpay_cell_target_operations_v1 SET status='IDLE',error_class=%s "
                        "WHERE cluster_id=%s AND target_id=%s AND operation_id=%s").format(schema),
                        (type(exc).__name__, "412110", run.BOOK, recovery_id))
        except Exception:
            pass
        dump_once(outcome_path, {**precheck, "status": "FAILED", "error_class": type(exc).__name__,
            "uncertain_write": uncertain, "precheck_sha256": run.sha(precheck_path)})
        raise
    dump_once(outcome_path, {**precheck, "status": "COMPLETE", "recovery_operation": recovery_id,
        "precheck_sha256": run.sha(precheck_path)})
    run.event("known_pending_recovered", cluster="412110", epoch=epoch)


def counts_for_schema(connection):
    from psycopg import sql
    return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
        sql.Identifier(run.SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
        for name in ("sources", "ledger", "current", "calculations")}


def validate_scope(runtime, acquired, configuration_sha256):
    """The acquired receipt and fixed operator scope, not CLI flags, bind writes."""
    if acquired.get("configuration_sha256") != configuration_sha256:
        raise ValueError("publisher config differs from the acquired prepare-time pin")
    target = {"host": "127.0.0.1", "port": run.PORT, "database": run.DATABASE,
              "schema": run.SCHEMA, "user": run.USER}
    if (runtime.get("spreadsheet_id") != run.BOOK or runtime.get("legacy_spreadsheet_id") != run.OLD_BOOK
        or runtime.get("approved_v2_spreadsheet_ids") != [run.BOOK]
        or runtime.get("worksheet_targets") != run.TARGETS or runtime.get("worksheet_naming") != "cluster_alias"
        or runtime.get("database_target") != target or runtime.get("approved_database_targets") != [target]
        or runtime.get("data_classification") != "PRODUCTION" or runtime.get("dummy_data_only") is not False
        or runtime.get("finance_emails") != [runtime.get("writer_email")]):
        raise ValueError("publisher configuration escaped the approved destination/schema/identity scope")
    accounts = runtime.get("accounts", [])
    if len(accounts) != 8 or {item.get("account") for item in accounts} != set(run.ROSTER):
        raise ValueError("publisher account coverage differs from approved roster")
    for item in accounts:
        codes = run.ROSTER[item["account"]]
        if (item.get("cell_codes") != list(codes) or item.get("first_report_date") != run.DATES[0]
            or item.get("opening_boundary") != run.DATES[0] + "T00:00:00+08:00"
            or item.get("opening_balances") != {code: 0 for code in codes}
            or any(type(value) is not int for value in item.get("opening_balances", {}).values())):
            raise ValueError("publisher opening boundary/balances escaped the approved zero-opening scope")


def private_destination(client, root):
    response = client.http_client.request("get", "https://www.googleapis.com/drive/v3/files/" + run.BOOK,
        params={"fields": "id,name,permissions(id,type,role),capabilities(canEdit)"}).json()
    permissions = sorted(response.get("permissions", []), key=lambda item: item["id"])
    digest = hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()
    expected = json.loads((root / "destination-pins.json").read_text())["permissions_sha256"]
    if (response.get("id"), response.get("name"), response.get("capabilities", {}).get("canEdit")) != (run.BOOK, run.BOOK_NAME, True) or digest != expected:
        raise ValueError("backfill destination/sharing changed")


def sheet_client(info):
    from requests.exceptions import Timeout, ConnectionError
    from finpay_pipeline.cell_workflow import make_cell_sheet_client
    client = make_cell_sheet_client(info)
    original, last = client.http_client.request, 0.0
    allowed = {item["worksheet_id"] for group in run.TARGETS.values() for item in group.values()}
    def request(*args, **kwargs):
        nonlocal last
        for item in (kwargs.get("json") or {}).get("requests", []):
            update = item.get("updateCells", {})
            if update.get("range", {}).get("sheetId") in allowed and update.get("fields") == "note":
                for row in update.get("rows", []):
                    for cell in row.get("values", []):
                        cell["note"] = cell.get("note", "") + "\n" + NOTICE
        method = str(args[0] if args else kwargs.get("method", "")).lower()
        for attempt in range(3):
            time.sleep(max(0, last + 1.1 - time.monotonic()))
            last = time.monotonic()
            try:
                return original(*args, **kwargs)
            except (Timeout, ConnectionError):
                if method != "get" or attempt == 2:
                    raise
                run.event("backfill_read_retry", attempt=attempt + 1)
    client.http_client.request = request
    return client


def execute(args):
    import psycopg
    from psycopg import sql
    from finpay_pipeline.cell_contracts import parse_cell_amount
    from finpay_pipeline.cell_database import CELL_MIGRATION, CellDatabaseTarget, cell_publication_session
    from finpay_pipeline.cell_workflow import prepare_cell_bundle_v2, run_cell_reporting_v2, read_cell_configuration_v2
    from finpay_pipeline.cell_publication_v2 import build_cell_cluster_matrices_v2
    from finpay_pipeline.cell_publication import _padded
    from finpay_pipeline.cell_sheets import column_letter, cell_grid_capacity
    root = args.root
    runtime = run.check(root, args.configuration_sha256)
    sources = receipt(root, args.source_receipt_sha256)
    validate_scope(runtime, json.loads((root / "result.json").read_text()), args.configuration_sha256)
    if run.sha(Path(__file__)) != args.publisher_sha256:
        raise ValueError("accepted publisher code pin mismatch")
    oracle_path = Path(__file__).with_name("cell_summary_oracle.py")
    if run.sha(oracle_path) != args.oracle_sha256:
        raise ValueError("accepted independent oracle code pin mismatch")
    from finpay_pipeline.tests.cell_summary_oracle import audit_summary
    target = CellDatabaseTarget(**runtime["database_target"])
    if (target.host, target.port, target.database, target.schema, target.user) != (
            "127.0.0.1", run.PORT, run.DATABASE, run.SCHEMA, run.USER):
        raise ValueError("backfill DB target differs from exact approved scope")
    info = json.loads(args.credential_file.read_text())
    if hashlib.sha256(info["client_email"].encode()).hexdigest() != run.WRITER_SHA or info["client_email"] != runtime["writer_email"]:
        raise ValueError("backfill writer principal mismatch")
    client = sheet_client(info)
    private_destination(client, root)
    _, configs, targets = read_cell_configuration_v2(root / "configuration.json", args.configuration_sha256, args.configuration_sha256)
    password = args.database_password_file.read_text().strip()
    connection = psycopg.connect(host=target.host, port=target.port, dbname=target.database,
        user=target.user, password=password, autocommit=True)
    progress_path = root / "persistence-progress.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"days": {}}
    expected_rows = sum(item["rows"] for item in sources.values())
    expected_counts = {"sources": 306, "ledger": expected_rows, "current": 306, "calculations": 306}
    def counts():
        return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(run.SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
            for name in expected_counts}
    try:
        if connection.execute("SELECT current_database(),current_user").fetchone() != (run.DATABASE, run.USER):
            raise ValueError("backfill database identity mismatch")
        exists = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (run.SCHEMA,)).fetchone()
        if exists and not progress.get("schema_initialized"):
            raise ValueError("existing schema has no owned initialization receipt")
        if not exists:
            if progress.get("schema_initialized"):
                raise ValueError("owned schema disappeared")
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(run.SCHEMA)))
                connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(run.SCHEMA)))
                connection.execute(CELL_MIGRATION.read_text(), prepare=False)
            progress.update(schema_initialized=True, configuration_sha256=args.configuration_sha256)
            run.dump(progress_path, progress)
        if progress["configuration_sha256"] != args.configuration_sha256:
            raise ValueError("schema ownership/configuration receipt mismatch")
        for day in run.DATES:
            if day in progress["days"]:
                if progress["days"][day] != sources[day]["sha256"]:
                    raise ValueError("persisted source checkpoint changed")
                continue
            with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                _, _, manifest, batches = prepare_cell_bundle_v2(root / "sources" / day / "cell_sources_v2.zip",
                    root / "configuration.json", args.configuration_sha256, sources[day]["sha256"],
                    approved_configuration_sha256=args.configuration_sha256)
                if sum(len(fact["raw_rows"]) for batch in batches.values() for fact in batch) != sources[day]["rows"]:
                    raise ValueError("source/DB-stage row count mismatch")
                for account in sorted(configs):
                    with cell_publication_session(target, {target}, account[:-2], runtime["spreadsheet_id"], password=password, write_gsheet=False, approved_sheet_ids=set()) as store:
                        store.commit_cluster_day(batches[account], configs[account].cells)
            progress["days"][day] = sources[day]["sha256"]
            run.dump(progress_path, progress)
            run.event("backfill_db_day_complete", report_date=day, source_rows=sources[day]["rows"])
        if counts() != expected_counts:
            raise ValueError("backfill persistence coverage mismatch")
        audits, planned_cells = {}, 0
        for account, config in sorted(configs.items()):
            with cell_publication_session(target, {target}, account[:-2], runtime["spreadsheet_id"], password=password, write_gsheet=False, approved_sheet_ids=set()) as store:
                rows = store.current_results(config.first_report_date, run.DATES[-1])
            identities = {item.cell_code: item.to_dict() for item in config.cells}
            facts = [{"identity": identities[item["cell_code"]], "report_date": str(item["report_date"]),
                "generation_id": item["generation_id"], "calculation_id": item["calculation_id"],
                "config_id": item["config_id"], "rules_version": config.rules_version, "calculation": item["result"]} for item in rows]
            matrices = build_cell_cluster_matrices_v2(config, facts, run.DATES[-1])
            audits[account] = audit_summary(config, facts, matrices["summary"])
            for matrix in matrices.values():
                values = matrix["values"]
                cap = cell_grid_capacity(len(values), len(values[0]), {"rowCount": 300, "columnCount": 100})
                planned_cells += cap["rowCount"] * cap["columnCount"]
        raw_errors = 0
        for (row,) in connection.execute(sql.SQL("SELECT raw_row FROM {}.finpay_cell_ledger_v1").format(sql.Identifier(run.SCHEMA))):
            opening, credit, debit, closing = [parse_cell_amount(row.get(key), key) for key in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir")]
            raw_errors += closing != opening + credit - debit
        if raw_errors:
            raise ValueError("raw source balance-equation errors require review")
        run.dump(root / "financial-candidate-audit.json", {"accounts": audits, "raw_balance_errors": raw_errors,
            "declared_opening_authority": "current user: September30 close zero", "planned_grid_cells": planned_cells})
        private_destination(client, root)
        workbook = client.open_by_key(run.BOOK)
        metadata = workbook.fetch_sheet_metadata()
        current_tabs = {str(item["properties"]["sheetId"]): item for item in metadata["sheets"]}
        allowed = {item["worksheet_id"] for group in run.TARGETS.values() for item in group.values()}
        titles = {item["title"] for group in run.TARGETS.values() for item in group.values()}
        target_ids = {str(value) for value in allowed}
        existing_target_ids = set(current_tabs) & target_ids
        if existing_target_ids:
            original_path = root / "original-tab-metadata.json"
            if existing_target_ids != target_ids or not original_path.exists():
                raise ValueError("partial/unowned backfill tab set; no overwrite or retry")
            original = json.loads(original_path.read_text())
            if set(current_tabs) != set(original) | target_ids or any(current_tabs.get(key) != value for key, value in original.items()):
                raise ValueError("pre-existing destination tabs differ from captured baseline")
            if any(current_tabs[str(item["worksheet_id"])]["properties"]["title"] != item["title"]
                   for group in run.TARGETS.values() for item in group.values()):
                raise ValueError("existing alias target title differs from approved ID")
            # Resume only the exact known PKY PENDING validation failure, or the
            # post-recovery state before the remaining clusters were published.
            latest = connection.execute(sql.SQL("SELECT cluster_id,epoch,status,error_class FROM {}.finpay_cell_publications_v1 "
                "WHERE target_id=%s ORDER BY cluster_id,epoch").format(sql.Identifier(run.SCHEMA)), (run.BOOK,)).fetchall()
            if len(latest) == 1 and latest[0][0:3] == ("412110", 1, "PENDING") and latest[0][3] == "ValueError":
                reconcile_known_pending(root, connection, target, runtime, configs, targets, client, workbook,
                                        password, expected_counts)
            elif not (len(latest) == 1 and latest[0][0:3] == ("412110", 1, "COMPLETE")
                      and connection.execute(sql.SQL("SELECT status FROM {}.finpay_cell_target_operations_v1 "
                          "WHERE target_id=%s").format(sql.Identifier(run.SCHEMA)), (run.BOOK,)).fetchone() == ("IDLE",)):
                raise ValueError("existing tabs are not in an approved resumable state")
            run.event("existing_alias_tabs_resumed", tabs=32)
        else:
            original = current_tabs
            allocated = sum(item["properties"].get("gridProperties", {}).get("rowCount", 0) *
                item["properties"].get("gridProperties", {}).get("columnCount", 0) for item in metadata["sheets"])
            if allocated + planned_cells > 10_000_000:
                raise ValueError("new workbook exceeds Google cell capacity")
            if any(value["properties"]["title"] in titles for value in original.values()):
                raise ValueError("fresh backfill title collision; no overwrite/retry")
            if not (root / "original-tab-metadata.json").exists():
                run.dump(root / "original-tab-metadata.json", original)
            elif json.loads((root / "original-tab-metadata.json").read_text()) != original:
                raise ValueError("fresh destination differs from captured original-tab state")
        state = json.loads((root / "result.json").read_text())
        state.update(status="PUBLISHING", source_receipt_sha256=args.source_receipt_sha256,
                     publisher_sha256=args.publisher_sha256, db_counts=counts())
        run.dump(root / "result.json", state)
        if not existing_target_ids:
            workbook.batch_update({"requests": [{"addSheet": {"properties": {"sheetId": item["worksheet_id"],
                "title": item["title"], "gridProperties": {"rowCount": 300, "columnCount": 100}}}}
                for group in run.TARGETS.values() for item in group.values()]})
            provisioned = {item["properties"]["sheetId"]: item["properties"]["title"]
                           for item in workbook.fetch_sheet_metadata()["sheets"]}
            if any(provisioned.get(item["worksheet_id"]) != item["title"]
                   for group in run.TARGETS.values() for item in group.values()):
                raise ValueError("provisioned alias IDs/titles differ from approved targets")
            run.event("backfill_tabs_created", tabs=32, planned_grid_cells=planned_cells)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_cell_reporting_v2(root / "sources" / run.DATES[-1] / "cell_sources_v2.zip", root / "configuration.json",
                args.configuration_sha256, sources[run.DATES[-1]]["sha256"], approved_configuration_sha256=args.configuration_sha256,
                database_password=password, credentials_info=info, dry_run=False, write_gsheet=True, configuration_only=True,
                sheet_client_factory=lambda data: client, output_dir=root / "publication")
        epochs = {}
        for account in sorted(configs):
            row = connection.execute(sql.SQL("SELECT epoch,status,snapshot FROM {}.finpay_cell_publications_v1 "
                "WHERE target_id=%s AND cluster_id=%s ORDER BY epoch DESC LIMIT 1").format(sql.Identifier(run.SCHEMA)),
                (run.BOOK, account[:-2])).fetchone()
            if not row or row[1] != "COMPLETE":
                raise ValueError("backfill cluster did not complete")
            epochs[account] = row[0]
            for role, item in row[2]["worksheets"].items():
                values = item["values"]
                sheet = workbook.get_worksheet_by_id(item["worksheet_id"])
                actual = sheet.get(f"A1:{column_letter(len(values[0]))}{len(values)}", value_render_option="UNFORMATTED_VALUE")
                expected = item["matrix"]["numeric_preview"] if role == "summary" else values
                if _padded(actual, len(values), len(values[0])) != expected:
                    raise ValueError("backfill evaluated Google values differ from DB")
            run.event("backfill_cluster_verified", account=account, alias=run.ALIASES[account], epoch=row[0])
        private_destination(client, root)
        final = {str(item["properties"]["sheetId"]): item for item in workbook.fetch_sheet_metadata()["sheets"]}
        if len(final) != len(original) + 32 or any(final.get(key) != value for key, value in original.items()):
            raise ValueError("existing destination tab metadata changed")
        if counts() != expected_counts:
            raise ValueError("publication changed immutable backfill coverage")
        state.update(status="PASS", db_counts=counts(), epochs=epochs, independent_candidate_audit="PASS",
            evaluated_formulas="PASS", native_styles_values_protections="PASS", existing_tabs_sharing="PRESERVED",
            links={account: {role: f"https://docs.google.com/spreadsheets/d/{run.BOOK}/edit#gid={item['worksheet_id']}"
                for role, item in group.items()} for account, group in run.TARGETS.items()})
        run.dump(root / "result.json", state)
        run.event("october_backfill_pass", cell_days=306, rows=expected_rows, tabs=32)
    finally:
        connection.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--source-receipt-sha256", required=True)
    parser.add_argument("--publisher-sha256", required=True)
    parser.add_argument("--oracle-sha256", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--database-password-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        execute(args)
    except Exception as exc:
        traceback.print_exc()
        path = args.root / "result.json"
        if path.exists():
            state = json.loads(path.read_text())
            if state.get("status") == "PUBLISHING":
                state.update(status="FAILED", error_class=type(exc).__name__)
                run.dump(path, state)
        raise RuntimeError(f"backfill stopped: {type(exc).__name__}; retain checkpoint/state, no blind write retry") from None


if __name__ == "__main__":
    main()
