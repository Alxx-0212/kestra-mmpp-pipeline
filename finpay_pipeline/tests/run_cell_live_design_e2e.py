"""Explicitly invoked, fixed-scope real-source design test; never a scheduled job."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
import zipfile
from pathlib import Path

BOOK = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
ORIGINAL = "1Y_ZtlE-ugE5dFschxUl4OHPHihMgbluTg3g2mid-0FY"
WRITER_SHA256 = "2ab7954647e7cca1673131da541b0121e45d5c3226bcae5584e1b01dbce5d5b4"
SHARING_SHA256 = "1aa68640032ac83a24e70aed6f2cb4e8bd380ddc083488e251f5f8363706c03b"
DATABASE, SCHEMA, USER, PORT = "finpay_live_design_e2e", "cell_design_test", "finpay_live_design_test", 55433
CONTAINER = "finpay-cell-live-design-e2e-20261017"
VOLUME = CONTAINER + "-data"
DATES = ("2026-10-14", "2026-10-15", "2026-10-16")
ROSTER = {"412110_B": ("412111", "412112"), "422010_B": ("422011", "422012", "422013"),
    "422040_B": ("422041", "422042", "422043"), "422090_B": ("422091", "422092"),
    "422100_B": ("422101", "422102"), "422140_B": ("422141", "422142"),
    "422180_B": ("422181", "422182"), "422220_B": ("422221", "422222")}
ROLES = ("summary", "invoice", "unusual", "reversal")
TARGETS = {account: {role: {"worksheet_id": 1900174101 + index * 4 + offset,
    "title": f"{account[:-2]} {role.title()}"} for offset, role in enumerate(ROLES)}
    for index, account in enumerate(sorted(ROSTER))}
BUNDLE_LIMIT = 256 * 1024 * 1024
NOTICE = ("REAL-SOURCE DESIGN TEST ONLY. 14-16 October 2026 WITA. ALL OPENING BALANCES ARE ZERO TEST INPUTS, "
          "NOT FINANCE-APPROVED BALANCES. Green/COMPLETE means verified Sheet content, NOT accounting sign-off.")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump(path, value):
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    pending.replace(path)


def dump_once(path, value):
    """Recovery evidence is append-only: precheck and outcome are separate files."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def recovery_outcome(path, precheck, audit, *, transitioned, error=None):
    result = {**audit, "precheck_sha256": sha(precheck), "transition_confirmed": transitioned,
        "status": "COMPLETE" if error is None else "FAILED" if transitioned else "CAS_UNCONFIRMED"}
    if error is not None:
        result["error_class"] = type(error).__name__
    dump_once(path, result)


def private_root(root, *, create=False):
    if root.is_symlink():
        raise ValueError("live-test root cannot be a symlink")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("live-test root must be owner-only mode 0700")


def event(stage, **values):
    print(json.dumps({"stage": stage, **values}, sort_keys=True), flush=True)


def code_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = [*root.glob("*.py"), root / "migrations/003_finpay_cell_reporting_v1.sql", Path(__file__),
             root / "tests/cell_summary_oracle.py"]
    return {str(path.relative_to(root)): sha(path) for path in paths}


def private_destination(client):
    response = client.http_client.request("get", "https://www.googleapis.com/drive/v3/files/" + BOOK,
        params={"fields": "id,name,permissions(id,type,role),capabilities(canEdit)"}).json()
    permissions = sorted(response.get("permissions", []), key=lambda item: item["id"])
    digest = hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()
    if (response.get("id") != BOOK or response.get("name") != "Salinan dari Monitoring Finpay & LinkAja"
        or not response.get("capabilities", {}).get("canEdit") or digest != SHARING_SHA256
        or sorted((item["type"], item["role"]) for item in permissions) != [("user", "owner"), ("user", "writer")]):
        raise ValueError("private destination/sharing differs from the approved baseline")
    return digest


def design_client(info):
    from finpay_pipeline.cell_workflow import make_cell_sheet_client
    from requests.exceptions import ConnectionError, Timeout
    client = make_cell_sheet_client(info)
    request, last, confirmed = client.http_client.request, 0.0, 0
    allowed = {item["worksheet_id"] for group in TARGETS.values() for item in group.values()}

    def paced(*args, **kwargs):
        nonlocal last, confirmed
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
                response = request(*args, **kwargs)
                break
            except (Timeout, ConnectionError) as exc:
                if method != "get" or attempt == 2:
                    raise
                event("read_only_retry", attempt=attempt + 1, error_class=type(exc).__name__)
        confirmed += 1
        if confirmed % 100 == 0:
            event("google_http_confirmed", requests=confirmed)
        return response

    client.http_client.request = paced
    return client


def configuration(writer):
    from dataclasses import asdict
    from finpay_pipeline.cell_calculation import CellReportConfig
    from finpay_pipeline.cell_contracts import OBSERVED_CELL_ROSTER
    from finpay_pipeline.cell_workflow import MAX_BUNDLE_BYTES
    if ROSTER != OBSERVED_CELL_ROSTER or BUNDLE_LIMIT != MAX_BUNDLE_BYTES:
        raise ValueError("approved roster/bundle contract differs from runtime")
    target = {"host": "127.0.0.1", "port": PORT, "database": DATABASE, "schema": SCHEMA, "user": USER}
    accounts = []
    for account, codes in sorted(ROSTER.items()):
        item = asdict(CellReportConfig(account, codes, DATES[0], DATES[0] + "T00:00:00+08:00",
            {code: 0 for code in codes}, "LIVE-DESIGN-TEST-20261017"))
        item["cell_codes"] = list(item["cell_codes"])
        accounts.append(item)
    return {"schema_version": 2, "data_classification": "PRODUCTION", "dummy_data_only": False,
        "spreadsheet_id": BOOK, "legacy_spreadsheet_id": ORIGINAL, "approved_v2_spreadsheet_ids": [BOOK],
        "writer_email": writer, "finance_emails": [writer], "accounts": accounts,
        "worksheet_targets": TARGETS, "database_target": target, "approved_database_targets": [target]}


def prepare(root, credential):
    from finpay_pipeline.cell_workflow import read_cell_configuration_v2
    private_root(root, create=True)
    if any(root.iterdir()):
        raise ValueError("live-test root is not fresh; no replacement permitted")
    writer = json.loads(credential.read_text())["client_email"]
    if hashlib.sha256(writer.encode()).hexdigest() != WRITER_SHA256:
        raise ValueError("writer differs from independently known principal")
    config = root / "configuration.json"
    dump(config, configuration(writer))
    digest = sha(config)
    read_cell_configuration_v2(config, digest, digest)
    with os.fdopen(os.open(root / "db-password", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        handle.write(secrets.token_urlsafe(32))
    dump(root / "code-pins.json", code_hashes())
    dump(root / "result.json", {"status": "PREPARED", "configuration_sha256": digest,
        "scope": "REAL_SOURCE_DESIGN_TEST_ZERO_OPENINGS", "workbook": BOOK, "dates": DATES,
        "container": CONTAINER, "volume": VOLUME, "retain_test_database": True,
        "checks": {}, "runner_sha256": sha(Path(__file__))})
    event("prepared", configuration_sha256=digest, accounts=8, cells=18, tabs=32)


def check_inputs(root, expected):
    private_root(root)
    if not expected or sha(root / "configuration.json") != expected:
        raise ValueError("configuration differs from independently supplied checksum")
    if json.loads((root / "code-pins.json").read_text()) != code_hashes():
        raise ValueError("pipeline source changed after run preparation")
    return json.loads((root / "configuration.json").read_text())


def source_receipt(root, expected):
    private_root(root)
    if not expected or sha(root / "sources.json") != expected:
        raise ValueError("source receipt pin mismatch")
    receipt = json.loads((root / "sources.json").read_text())
    checkpoint = json.loads((root / "result.json").read_text())
    if checkpoint.get("status") != "ACQUIRED" or checkpoint.get("source_summary") != receipt:
        raise ValueError("source receipt differs from the acquisition checkpoint")
    if set(receipt) != set(DATES):
        raise ValueError("incomplete source receipt dates")
    for item in receipt.values():
        if (set(item) != {"sha256", "rows", "cells", "empty_cells"}
            or type(item["cells"]) is not int or item["cells"] != 18
            or type(item["rows"]) is not int or item["rows"] < 0
            or type(item["empty_cells"]) is not int or not 0 <= item["empty_cells"] <= 18
            or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("invalid source receipt counts/checksums")
    return receipt


def acquire(root, expected, secret_file):
    check_inputs(root, expected)
    state = json.loads((root / "result.json").read_text())
    if state["status"] != "PREPARED" and not (state["status"] == "FAILED" and state.get("failed_phase") == "acquire"):
        raise ValueError("source acquisition cannot replace an acquired/publishing run")
    state["status"] = "ACQUIRING"
    dump(root / "result.json", state)
    # The existing browser image is deliberately minimal. Use the same production
    # scraper core standalone, then its standard flat ZIP contract across images.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from digipos_cells import scrape_cell_roster
    from cell_contracts import configured_cells, validate_complete_manifest, sha256_file_bounded
    password = None
    for line in secret_file.read_text().splitlines():
        if line.strip().startswith("SECRET_DIGIPOS_PASSWORD="):
            password = base64.b64decode(line.split("=", 1)[1].strip() + "===").decode()
            break
    if not password:
        raise ValueError("approved portal password reference is missing")
    sources = root / "sources"
    sources.mkdir(exist_ok=True)
    results = json.loads((root / "sources.json").read_text()) if (root / "sources.json").exists() else {}
    for day in DATES:
        if day in results:
            if sha(sources / day / "cell_sources_v2.zip") != results[day]["sha256"]:
                raise ValueError("immutable completed source bundle changed")
            event("validated_source_reused", report_date=day, **results[day])
            continue
        event("scrape_started", report_date=day)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            manifest = scrape_cell_roster(day, password, ROSTER, str(sources), googleapis_compatibility=True)
        validate_complete_manifest(manifest, configured_cells(ROSTER))
        directory = sources / day
        dump(directory / "manifest.json", manifest)
        size = len(json.dumps(manifest).encode()) + 4096
        for entry in manifest["cells"]:
            if entry["artifact"]:
                path = directory / entry["artifact"]
                if path.resolve().parent != directory.resolve():
                    raise ValueError("source artifact escaped approved directory")
                size += path.stat().st_size + 4096
        if size > BUNDLE_LIMIT:
            raise ValueError("source bundle exceeds existing runtime bound")
        pending = directory / "cell_sources_v2.pending.zip"
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for entry in manifest["cells"]:
                if entry["artifact"]:
                    archive.write(directory / entry["artifact"], arcname=entry["artifact"])
        digest = sha256_file_bounded(pending, BUNDLE_LIMIT)
        pending.replace(directory / "cell_sources_v2.zip")
        results[day] = {"sha256": digest, "rows": sum(entry["validated_rows"] for entry in manifest["cells"]),
            "cells": len(manifest["cells"]), "empty_cells": sum(entry["status"] == "empty" for entry in manifest["cells"])}
        dump(root / "sources.json", results)
        event("scrape_validated", report_date=day, **results[day])
    dump(root / "sources.json", results)
    result = json.loads((root / "result.json").read_text())
    result.update(status="ACQUIRED", source_summary=results)
    dump(root / "result.json", result)


def publish(root, expected, credential, receipt_sha256):
    import psycopg
    from psycopg import sql
    from finpay_pipeline.cell_database import CELL_MIGRATION
    from finpay_pipeline.cell_publication import _padded
    from finpay_pipeline.cell_sheets import column_letter
    from finpay_pipeline.cell_workflow import prepare_cell_bundle_v2, run_cell_reporting_v2
    runtime = check_inputs(root, expected)
    if json.loads((root / "result.json").read_text())["status"] != "ACQUIRED":
        raise ValueError("no implicit publish retry; all source acquisition must complete first")
    info = json.loads(credential.read_text())
    if runtime != configuration(info["client_email"]) or hashlib.sha256(info["client_email"].encode()).hexdigest() != WRITER_SHA256:
        raise ValueError("fixed target/principal scope mismatch")
    sources = source_receipt(root, receipt_sha256)
    for day in DATES:
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            _, _, manifest, batches = prepare_cell_bundle_v2(root / "sources" / day / "cell_sources_v2.zip", root / "configuration.json",
                expected, sources[day]["sha256"], approved_configuration_sha256=expected)
        if (len(manifest["cells"]) != sources[day]["cells"]
            or sum(item["status"] == "empty" for item in manifest["cells"]) != sources[day]["empty_cells"]
            or sum(len(fact["raw_rows"]) for batch in batches.values() for fact in batch) != sources[day]["rows"]):
            raise ValueError("prepared source counts differ from pinned acquisition receipt")
    result = json.loads((root / "result.json").read_text())
    result.update(status="PUBLISHING", source_receipt_sha256=receipt_sha256,
                  publisher_runner_sha256=sha(Path(__file__)))
    dump(root / "result.json", result)
    password = (root / "db-password").read_text().strip()
    connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname=DATABASE, user=USER,
        password=password, autocommit=True)
    try:
        if connection.execute("SELECT current_database(),current_user").fetchone() != (DATABASE, USER):
            raise ValueError("isolated DB identity mismatch")
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMA,)).fetchone():
            raise ValueError("schema is not fresh; no reset or fabricated baseline")
        with connection.transaction():
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
            connection.execute(CELL_MIGRATION.read_text(), prepare=False)

        def run(day, sheets):
            with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                return run_cell_reporting_v2(root / "sources" / day / "cell_sources_v2.zip", root / "configuration.json",
                    expected, sources[day]["sha256"], approved_configuration_sha256=expected,
                    dry_run=False, write_gsheet=sheets, database_password=password,
                    credentials_info=info if sheets else None, sheet_client_factory=lambda data: client,
                    output_dir=root / ("sheets" if sheets else "db") / day)

        for day in DATES:
            run(day, False)
            event("database_day_staged", report_date=day)

        def counts():
            return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
                for name in ("sources", "ledger", "current", "calculations")}

        expected_counts = {"sources": 54, "ledger": sum(item["rows"] for item in sources.values()),
                           "current": 54, "calculations": 54}
        if counts() != expected_counts:
            raise AssertionError("real-source DB coverage mismatch")
        result["db_counts"] = expected_counts
        result["checks"].update(complete_source_barrier="PASS", native_migration_003="PASS", native_ingestion="PASS")
        dump(root / "result.json", result)

        client = design_client(info)
        allowed = {item["worksheet_id"] for group in TARGETS.values() for item in group.values()}
        private_destination(client)
        workbook = client.open_by_key(BOOK)
        metadata = workbook.fetch_sheet_metadata()
        if metadata["spreadsheetId"] != BOOK or metadata["properties"]["title"] != "Salinan dari Monitoring Finpay & LinkAja":
            raise ValueError("approved copied workbook identity mismatch")
        original = {item["properties"]["sheetId"]: item for item in metadata["sheets"]}
        titles = {item["title"] for group in TARGETS.values() for item in group.values()}
        if allowed & set(original) or any(item["properties"]["title"] in titles for item in original.values()):
            raise ValueError("fresh target ID/title collision; refusing overwrite")
        workbook.batch_update({"requests": [{"addSheet": {"properties": {"sheetId": item["worksheet_id"],
            "title": item["title"], "gridProperties": {"rowCount": 300, "columnCount": 100}}}}
            for group in TARGETS.values() for item in group.values()]})
        dump(root / "original-tab-metadata.json", original)
        result["created_tab_ids"] = sorted(allowed)
        dump(root / "result.json", result)
        event("fresh_tabs_created", tabs=32)
        run(DATES[-1], True)

        def publications():
            rows = connection.execute(sql.SQL("SELECT cluster_id,epoch,status,snapshot,snapshot_fingerprint "
                "FROM {}.finpay_cell_publications_v1 WHERE target_id=%s ORDER BY cluster_id,epoch DESC"
                ).format(sql.Identifier(SCHEMA)), (BOOK,)).fetchall()
            latest = {}
            for row in rows:
                latest.setdefault(row[0], row)
            if set(latest) != {account[:-2] for account in ROSTER} or any(row[2] != "COMPLETE" for row in latest.values()):
                raise AssertionError("not all eight four-tab epochs completed")
            return latest

        before = publications()
        run(DATES[-1], True)
        after = publications()
        if {key: (row[1], row[4]) for key, row in before.items()} != {key: (row[1], row[4]) for key, row in after.items()} or counts() != expected_counts:
            raise AssertionError("real-source rerun changed epochs or duplicated evidence")
        result["checks"].update(four_role_styles_values_protections="PASS", unchanged_rerun="PASS")
        result["epochs"] = {key: row[1] for key, row in after.items()}
        dump(root / "result.json", result)
        for account, group in TARGETS.items():
            snapshot = after[account[:-2]][3]
            for role, target in group.items():
                saved = snapshot["worksheets"][role]
                values = saved["values"]
                sheet = workbook.get_worksheet_by_id(target["worksheet_id"])
                actual = sheet.get(f"A1:{column_letter(len(values[0]))}{len(values)}", value_render_option="UNFORMATTED_VALUE")
                expected_values = saved["matrix"]["numeric_preview"] if role == "summary" else values
                if _padded(actual, len(values), len(values[0])) != expected_values:
                    raise AssertionError("real-source evaluated values differ from DB projection")
                notes = client.http_client.request("get", f"https://sheets.googleapis.com/v4/spreadsheets/{BOOK}",
                    params={"ranges": f"'{target['title']}'!A1",
                        "fields": "sheets(properties(sheetId),data(rowData(values(note))))"}).json()
                note_sheet = next(item for item in notes["sheets"] if item["properties"]["sheetId"] == target["worksheet_id"])
                note = note_sheet["data"][0]["rowData"][0]["values"][0].get("note", "")
                if NOTICE not in note:
                    raise AssertionError("zero-opening test provenance is missing from the Sheet")
            event("evaluated_cluster_verified", account=account)
        final = {item["properties"]["sheetId"]: item for item in workbook.fetch_sheet_metadata()["sheets"]}
        if set(final) != set(original) | allowed or any(final[key] != value for key, value in original.items()):
            raise AssertionError("pre-existing worksheet metadata changed")
        result["checks"].update(evaluated_formulas="PASS", existing_tabs_preserved="PASS")
        private_destination(client)
        result["checks"]["private_sharing_and_test_labels"] = "PASS"
        result["links"] = {account: {role: f"https://docs.google.com/spreadsheets/d/{BOOK}/edit#gid={item['worksheet_id']}"
            for role, item in group.items()} for account, group in TARGETS.items()}
        result["status"] = "PASS"
        dump(root / "result.json", result)
        event("e2e_pass", rows=expected_counts["ledger"], sources=54, tabs=32)
    finally:
        connection.close()


def resume(root, expected, credential, receipt_sha256, failed_sha256):
    """Operator-approved recovery of this one sealed style failure, then refresh."""
    import psycopg
    from psycopg import sql
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_database import CellDatabaseTarget, CellStore
    from finpay_pipeline.cell_publication import (ExistingSheetAdapter, GUARD_PREFIX, MAX_REQUEST_BYTES,
        _editors_match, _format_requests, _marker_request, _protection_requests, _status_requests,
        _verify_final_protections, _verify_marker, _padded)
    from finpay_pipeline.cell_publication_v2 import _verify_style, _verify_values
    from finpay_pipeline.cell_invoice_sheets import invoice_format_requests
    from finpay_pipeline.cell_detail_sheets import detail_format_requests
    from finpay_pipeline.cell_workflow import read_cell_configuration_v2, run_cell_reporting_v2
    from finpay_pipeline.cell_sheets import column_letter

    runtime = check_inputs(root, expected)
    failed_path = root / "initial-failed-result.json"
    current_path = root / "result.json"
    original = failed_path if failed_path.exists() else current_path
    if failed_sha256 != "75e8cbb9cec71f9284b249ac0421b899cb35cdbab1b00e80935898d11ef3dc49" or sha(original) != failed_sha256:
        raise ValueError("recovery is restricted to the independently pinned failed attempt")
    failed = json.loads(original.read_text())
    if failed.get("status") != "FAILED" or failed.get("error_class") != "ValueError" or failed.get("created_tab_ids") != list(range(1900174101, 1900174133)):
        raise ValueError("failed-attempt ownership/scope mismatch")
    if sha(root / "sources.json") != receipt_sha256 or receipt_sha256 != "eadee07cf1b5dc0a8373ceae539bdce72f614b562c31dfb7a48ee7051b3a5a6e":
        raise ValueError("recovery source receipt pin mismatch")
    sources = json.loads((root / "sources.json").read_text())
    if sources != failed["source_summary"] or set(sources) != set(DATES):
        raise ValueError("recovery differs from the acquired checkpoint")
    for day in DATES:
        if sha(root / "sources" / day / "cell_sources_v2.zip") != sources[day]["sha256"]:
            raise ValueError("acquired source bundle changed")
    info = json.loads(credential.read_text())
    if runtime != configuration(info["client_email"]) or hashlib.sha256(info["client_email"].encode()).hexdigest() != WRITER_SHA256:
        raise ValueError("operator target/principal differs from approved scope")
    _, _, targets = read_cell_configuration_v2(root / "configuration.json", expected, expected)
    client = design_client(info)
    private_destination(client)
    workbook = client.open_by_key(BOOK)
    password = (root / "db-password").read_text().strip()
    target = CellDatabaseTarget(**runtime["database_target"])
    connection = psycopg.connect(host=target.host, port=target.port, dbname=target.database,
        user=target.user, password=password, autocommit=True)
    store = CellStore(connection, target, "412110", BOOK)
    audit_path = root / "recovery-412110-epoch1-precheck.json"
    outcome_path = root / "recovery-412110-epoch1-outcome.json"
    audit, transitioned, uncertain, precheck_written = {}, False, False, False

    def counts():
        return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
            for name in ("sources", "ledger", "current", "calculations")}

    def send(requests):
        nonlocal uncertain
        store.assert_alive()
        if len(json.dumps({"requests": requests}).encode()) > MAX_REQUEST_BYTES:
            raise ValueError("recovery request exceeds bounded size")
        uncertain = True
        result = workbook.batch_update({"requests": requests})
        uncertain = False
        store.assert_alive()
        return result

    try:
        if connection.execute("SELECT current_database(),current_user").fetchone() != (DATABASE, USER):
            raise ValueError("recovery database identity mismatch")
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        for namespace, value, attribute in (("finpay-cell-publisher-v1", BOOK, "locked"),
                                             ("finpay-cell-cluster-v1", "412110", "cluster_locked")):
            if connection.execute("SELECT pg_try_advisory_lock(hashtext(%s),hashtext(%s))", (namespace, value)).fetchone() != (True,):
                raise RuntimeError("recovery target/cluster is not exclusively locked")
            setattr(store, attribute, True)
        if connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND backend_type='client backend' AND pid<>pg_backend_pid()").fetchone()[0]:
            raise RuntimeError("another test-DB client prevents quiescence proof")
        backup_digest = connection.execute("SELECT encode(sha256(pg_read_binary_file(%s)),'hex')",
            ("/var/lib/postgresql/finpay-live-design-before-reconcile-20261018.dump",)).fetchone()[0]
        if backup_digest != "2dbdd6abec2cdf0bc5acd8174a9506b9776e0c0d84b92cac32dc90e23b8be086":
            raise ValueError("verified pre-recovery backup changed")
        row = connection.execute(sql.SQL("SELECT epoch,status,snapshot_fingerprint,snapshot,range_start,range_end,error_class "
            "FROM {}.finpay_cell_publications_v1 WHERE cluster_id='412110' AND target_id=%s ORDER BY epoch DESC LIMIT 1"
            ).format(sql.Identifier(SCHEMA)), (BOOK,)).fetchone()
        operation = connection.execute(sql.SQL("SELECT operation_id,status,error_class FROM {}.finpay_cell_target_operations_v1 "
            "WHERE cluster_id='412110' AND target_id=%s").format(sql.Identifier(SCHEMA)), (BOOK,)).fetchone()
        if not row or row[:2] != (1, "UNKNOWN") or row[6] != "ValueError" or not operation or operation[1:] != ("UNKNOWN", "ValueError"):
            raise ValueError("recovery is not the approved known epoch/operation failure")
        epoch, _, digest, snapshot, first, last, _ = row
        if fingerprint({"cluster_id": "412110", "target_id": BOOK, "range_start": str(first),
            "range_end": str(last), "snapshot": snapshot}) != digest:
            raise ValueError("saved recovery snapshot fingerprint mismatch")
        expected_counts = {"sources": 54, "ledger": 36504, "current": 54, "calculations": 54}
        if counts() != expected_counts:
            raise ValueError("retained source/calculation coverage changed")
        marker = {"status": "PENDING", "epoch": epoch, "fingerprint": digest}
        adapters, metadata, guards = {}, {}, {}
        for role, item in snapshot["worksheets"].items():
            approved = targets["412110_B"][role]
            if (item["worksheet_id"], item["title"]) != (approved.worksheet_id, approved.title):
                raise ValueError("recovery snapshot target mismatch")
            sheet = workbook.get_worksheet_by_id(approved.worksheet_id)
            if sheet.title != approved.title:
                raise ValueError("recovery worksheet title mismatch")
            adapter = adapters[role] = ExistingSheetAdapter(workbook, sheet)
            meta = metadata[role] = adapter.metadata()
            _verify_marker(meta, marker)
            _verify_values(adapter, item)
            found = [entry for entry in meta.get("protectedRanges", [])
                     if entry.get("description") == GUARD_PREFIX + operation[0]]
            if (len(found) != 1 or found[0].get("range") != {"sheetId": approved.worksheet_id}
                or found[0].get("warningOnly", False) or found[0].get("unprotectedRanges")
                or not _editors_match(found[0].get("editors", {}), [approved.writer_email], meta.get("implicitOwnerEmails", []))):
                raise ValueError("recovery whole-sheet guard is not verified")
            guards[role] = found[0]["protectedRangeId"]
        if audit_path.exists() or outcome_path.exists():
            raise ValueError("recovery already attempted; no implicit retry")
        if not failed_path.exists():
            with failed_path.open("xb") as handle:
                handle.write(current_path.read_bytes())
        audit = {"status": "PRECHECKED", "cluster": "412110", "epoch": 1, "operation_id": operation[0],
            "original_error": "ValueError", "snapshot_fingerprint": digest, "failed_attempt_sha256": failed_sha256,
            "backup": "/var/lib/postgresql/finpay-live-design-before-reconcile-20261018.dump",
            "backup_sha256": "2dbdd6abec2cdf0bc5acd8174a9506b9776e0c0d84b92cac32dc90e23b8be086", "db_counts": expected_counts}
        dump_once(audit_path, audit)
        precheck_written = True
        with connection.transaction():
            changed = connection.execute(sql.SQL("UPDATE {}.finpay_cell_target_operations_v1 SET status='IN_FLIGHT' "
                "WHERE target_id=%s AND cluster_id='412110' AND operation_id=%s AND status='UNKNOWN' AND error_class='ValueError' RETURNING operation_id"
                ).format(sql.Identifier(SCHEMA)), (BOOK, operation[0])).fetchone()
            changed_epoch = connection.execute(sql.SQL("UPDATE {}.finpay_cell_publications_v1 SET status='IN_FLIGHT' "
                "WHERE target_id=%s AND cluster_id='412110' AND epoch=1 AND status='UNKNOWN' AND snapshot_fingerprint=%s RETURNING epoch"
                ).format(sql.Identifier(SCHEMA)), (BOOK, digest)).fetchone()
            if not changed or changed_epoch != (1,):
                raise ValueError("recovery compare-and-swap lost its baseline")
        transitioned = True
        requests = []
        for role, item in snapshot["worksheets"].items():
            approved, matrix = targets["412110_B"][role], item["matrix"]
            requests.extend({"unmergeCells": {"range": merge}} for merge in metadata[role].get("merges", []))
            requests.extend(_format_requests(approved.sheet_target(), matrix) if role == "summary" else
                invoice_format_requests(approved.worksheet_id, matrix) if role == "invoice" else
                detail_format_requests(approved.worksheet_id, matrix))
        chunk, size, index = [], 16, 0
        for request in requests:
            length = len(json.dumps(request).encode()) + 2
            if length + 16 > MAX_REQUEST_BYTES:
                raise ValueError("one recovery format request exceeds bound")
            if size + length > MAX_REQUEST_BYTES:
                send(chunk)
                store.confirm_chunk(1, f"recovery-format-{index}")
                chunk, size, index = [], 16, index + 1
            chunk.append(request)
            size += length
        if chunk:
            send(chunk)
            store.confirm_chunk(1, f"recovery-format-{index}")
        finish = []
        marker["status"] = "COMPLETE"
        for role, item in snapshot["worksheets"].items():
            _verify_values(adapters[role], item)
            _verify_style(adapters[role], item["matrix"])
            approved = targets["412110_B"][role].sheet_target()
            finish.extend(_protection_requests(approved, item["matrix"], metadata[role], guards[role]))
            finish.extend([_marker_request(approved, adapters[role].metadata(), marker), *_status_requests(approved, marker)])
        send(finish)
        for role, item in snapshot["worksheets"].items():
            meta = adapters[role].metadata()
            _verify_marker(meta, marker)
            _verify_final_protections(targets["412110_B"][role].sheet_target(), item["matrix"], meta)
            _verify_values(adapters[role], item)
            _verify_style(adapters[role], item["matrix"])
        store.complete(1, verified_fingerprint=digest)
        store.finish_target_operation(operation[0])
        audit["final_operation"] = "IDLE"
        recovery_outcome(outcome_path, audit_path, audit, transitioned=True)
        event("guarded_epoch_reconciled", cluster="412110", epoch=1)
    except Exception as exc:
        if transitioned:
            uncertain = uncertain or isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
            try:
                (store.mark_unknown if uncertain else store.mark_pending)(1, type(exc).__name__)
                store.finish_target_operation(operation[0], **{"unknown_error" if uncertain else "known_error": type(exc).__name__})
            except Exception:
                pass
        if precheck_written and not outcome_path.exists():
            recovery_outcome(outcome_path, audit_path, audit, transitioned=transitioned, error=exc)
        raise
    finally:
        store.release()
        connection.close()

    return refresh_existing(root, expected, credential, receipt_sha256, failed_sha256, sha(outcome_path))


def refresh_existing(root, expected, credential, receipt_sha256, failed_sha256, recovery_sha256):
    """Continue only from a verified completed recovery, never repeat its CAS."""
    import psycopg
    from psycopg import sql
    from finpay_pipeline.cell_database import CellDatabaseTarget
    from finpay_pipeline.cell_publication import _padded
    from finpay_pipeline.cell_sheets import column_letter
    from finpay_pipeline.cell_workflow import run_cell_reporting_v2
    runtime = check_inputs(root, expected)
    current_path = root / "result.json"
    failed_path, outcome_path = root / "initial-failed-result.json", root / "recovery-412110-epoch1-outcome.json"
    if failed_sha256 != "75e8cbb9cec71f9284b249ac0421b899cb35cdbab1b00e80935898d11ef3dc49" or sha(failed_path) != failed_sha256:
        raise ValueError("refresh failed-attempt pin mismatch")
    if not recovery_sha256 or sha(outcome_path) != recovery_sha256:
        raise ValueError("refresh completed-recovery pin mismatch")
    outcome = json.loads(outcome_path.read_text())
    if (outcome.get("status"), outcome.get("transition_confirmed"), outcome.get("final_operation")) != ("COMPLETE", True, "IDLE"):
        raise ValueError("refresh requires completed guarded recovery")
    failed = json.loads(failed_path.read_text())
    if sha(root / "sources.json") != receipt_sha256 or receipt_sha256 != "eadee07cf1b5dc0a8373ceae539bdce72f614b562c31dfb7a48ee7051b3a5a6e":
        raise ValueError("refresh source receipt mismatch")
    sources = json.loads((root / "sources.json").read_text())
    if sources != failed["source_summary"]:
        raise ValueError("refresh differs from acquired source checkpoint")
    info = json.loads(credential.read_text())
    if runtime != configuration(info["client_email"]) or hashlib.sha256(info["client_email"].encode()).hexdigest() != WRITER_SHA256:
        raise ValueError("refresh target/principal mismatch")
    target = CellDatabaseTarget(**runtime["database_target"])
    password = (root / "db-password").read_text().strip()
    expected_counts = {"sources": 54, "ledger": 36504, "current": 54, "calculations": 54}
    connection = psycopg.connect(host=target.host, port=target.port, dbname=target.database, user=target.user,
        password=password, autocommit=True)

    def counts():
        return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
            for name in ("sources", "ledger", "current", "calculations")}

    try:
        original = connection.execute(sql.SQL("SELECT status,snapshot_fingerprint FROM {}.finpay_cell_publications_v1 "
            "WHERE cluster_id='412110' AND target_id=%s AND epoch=1").format(sql.Identifier(SCHEMA)), (BOOK,)).fetchone()
        unresolved = connection.execute(sql.SQL("SELECT count(*) FROM {}.finpay_cell_publications_v1 WHERE target_id=%s "
            "AND status IN ('PENDING','IN_FLIGHT','UNKNOWN')").format(sql.Identifier(SCHEMA)), (BOOK,)).fetchone()[0]
        operation = connection.execute(sql.SQL("SELECT status FROM {}.finpay_cell_target_operations_v1 WHERE target_id=%s"
            ).format(sql.Identifier(SCHEMA)), (BOOK,)).fetchone()
        if original != ("COMPLETE", outcome["snapshot_fingerprint"]) or unresolved or operation != ("IDLE",) or counts() != expected_counts:
            raise ValueError("refresh has an unresolved or changed database baseline")
    finally:
        connection.close()
    client = design_client(info)
    private_destination(client)
    workbook = client.open_by_key(BOOK)

    result = dict(failed)
    result.update(status="REFRESHING", initial_failed_result_sha256=failed_sha256,
                  recovery_sha256=recovery_sha256, publisher_runner_sha256=sha(Path(__file__)))
    result.pop("error_class", None)
    result.pop("failed_phase", None)
    dump(current_path, result)
    def refresh():
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            return run_cell_reporting_v2(root / "sources" / DATES[-1] / "cell_sources_v2.zip", root / "configuration.json",
                expected, sources[DATES[-1]]["sha256"], approved_configuration_sha256=expected,
                dry_run=False, write_gsheet=True, configuration_only=True, database_password=password,
                credentials_info=info, sheet_client_factory=lambda data: client, output_dir=root / "feedback-refresh")
    first_run, second_run = refresh(), refresh()
    if [(item["account"], item["epoch"], item["fingerprint"]) for item in first_run["accounts"]] != [
        (item["account"], item["epoch"], item["fingerprint"]) for item in second_run["accounts"]]:
        raise AssertionError("new design revision rerun changed epochs")
    connection = psycopg.connect(host=target.host, port=target.port, dbname=target.database, user=target.user,
        password=password, autocommit=True)
    try:
        if counts() != expected_counts:
            raise AssertionError("refresh changed retained immutable evidence counts")
        for account in sorted(ROSTER):
            row = connection.execute(sql.SQL("SELECT epoch,status,snapshot FROM {}.finpay_cell_publications_v1 "
                "WHERE cluster_id=%s AND target_id=%s ORDER BY epoch DESC LIMIT 1").format(sql.Identifier(SCHEMA)),
                (account[:-2], BOOK)).fetchone()
            if not row or row[1] != "COMPLETE":
                raise AssertionError("not every cluster completed the new revision")
            for role, item in row[2]["worksheets"].items():
                sheet = workbook.get_worksheet_by_id(item["worksheet_id"])
                values = item["values"]
                actual = sheet.get(f"A1:{column_letter(len(values[0]))}{len(values)}", value_render_option="UNFORMATTED_VALUE")
                wanted = item["matrix"]["numeric_preview"] if role == "summary" else values
                if _padded(actual, len(values), len(values[0])) != wanted:
                    raise AssertionError("refreshed Google evaluation differs from saved projection")
            event("feedback_cluster_verified", account=account, epoch=row[0])
        private_destination(client)
        original_tabs = json.loads((root / "original-tab-metadata.json").read_text())
        current_tabs = {str(item["properties"]["sheetId"]): item for item in workbook.fetch_sheet_metadata()["sheets"]}
        if len(current_tabs) != len(original_tabs) + 32 or any(current_tabs.get(key) != value for key, value in original_tabs.items()):
            raise AssertionError("original worksheet metadata changed")
        result.update(status="PASS", db_counts=counts(), epochs={item["account"][:-2]: item["epoch"] for item in second_run["accounts"]})
        result["checks"].update(guarded_reconciliation="PASS", all_32_styles_values_protections="PASS",
            evaluated_formulas="PASS", unchanged_rerun="PASS", existing_tabs_preserved="PASS", private_sharing="PASS")
        result["links"] = {account: {role: f"https://docs.google.com/spreadsheets/d/{BOOK}/edit#gid={item['worksheet_id']}"
            for role, item in group.items()} for account, group in TARGETS.items()}
        dump(current_path, result)
        event("feedback_e2e_pass", tabs=32, rows=36504)
    finally:
        connection.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "acquire", "publish", "resume", "refresh"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration-sha256")
    parser.add_argument("--source-receipt-sha256")
    parser.add_argument("--failed-attempt-sha256")
    parser.add_argument("--recovery-outcome-sha256")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--portal-secret-file", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            prepare(args.root, args.credential_file)
        elif args.phase == "acquire":
            acquire(args.root, args.configuration_sha256, args.portal_secret_file)
        elif args.phase == "publish":
            publish(args.root, args.configuration_sha256, args.credential_file, args.source_receipt_sha256)
        elif args.phase == "resume":
            resume(args.root, args.configuration_sha256, args.credential_file, args.source_receipt_sha256, args.failed_attempt_sha256)
        else:
            refresh_existing(args.root, args.configuration_sha256, args.credential_file,
                args.source_receipt_sha256, args.failed_attempt_sha256, args.recovery_outcome_sha256)
    except Exception as exc:
        path = args.root / "result.json"
        if path.exists():
            result = json.loads(path.read_text())
            if result["status"] in ({"REFRESHING"} if args.phase in ("resume", "refresh") else
                                    {{"acquire": "ACQUIRING", "publish": "PUBLISHING"}.get(args.phase)}):
                result.update(status="FAILED", failed_phase=args.phase, error_class=type(exc).__name__)
                response = getattr(exc, "response", None)
                if response is not None:
                    result["http_status"] = response.status_code
                dump(path, result)
        raise RuntimeError(f"{args.phase} stopped ({type(exc).__name__}); preserve state, no blind publish retry") from None


if __name__ == "__main__":
    main()
