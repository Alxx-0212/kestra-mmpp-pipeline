"""Explicitly invoked synthetic canary; never discovered as a unit test."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import secrets
import time
import zipfile
from datetime import date, timedelta
from dataclasses import asdict
from pathlib import Path

import psycopg
from psycopg import sql

from finpay_pipeline.cell_calculation import CellReportConfig
from finpay_pipeline.cell_contracts import configured_cells, fingerprint, roster_fingerprint
from finpay_pipeline.cell_database import CELL_MIGRATION
from finpay_pipeline.cell_publication import ExistingSheetAdapter, _padded, _verify_final_protections, _verify_marker
from finpay_pipeline.cell_sheets import column_letter
from finpay_pipeline.cell_workflow import make_cell_sheet_client, run_cell_reporting_v2
from finpay_pipeline.digipos_scraper import HEADERS, _write_xhr_export

WORKBOOK = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
TARGETS = {"summary": (1900164101, "412110 Summary"),
           "invoice": (1900164102, "412110 Invoice"),
           "reversal": (1900164103, "412110 Reversal"),
           "unusual": (1900164104, "412110 Unusual")}
DATABASE = "finpay_cell_invoice_e2e"
SCHEMA = "cell_invoice_test"
ROLE = "finpay_cell_invoice_test"
PORT = 55433
DATES = tuple((date(2026, 10, 13) + timedelta(days=offset)).isoformat() for offset in range(3))


def synthetic_rows(day, code):
    if (day, code) == ("2026-10-14", "412112"):
        return []
    scale = 1 if code == "412111" else 2
    rows = []

    def add(identifier, label, credit, debit, remarks):
        rows.append({"No": len(rows) + 1, "Transaction Date": day + " 12:00:00",
            "Transaction ID": identifier, "Saldo Awal": 1_000_000,
            "Kredit": credit, "Debet": debit, "Saldo Akhir": 1_000_000 + credit - debit,
            "Transaction Type": "Credit" if credit else "Debit", "Transaction": label,
            "Nomor RS": "0800000000", "Remarks": remarks})

    base = "SYNTHETIC" + day.replace("-", "")
    add(base + "ST", "SELLTHRU", 10_000 * scale, 0, "Sellthru Sales Fee")
    add(base + "STFEE", "SELLTHRUFEE", 0, 100, "Platform fee sellthru rp. 100,-")
    add(base + "STSLSHLD", "SELLTHRUSALESHOLD", 0, 20,
        "Sales hold transaksi sellthru sejumlah 20 rupiah, dari 412110")
    qris_date = "11-10-2026" if code == "412111" else "12-10-2026"
    add(base + "QRIS", "QRISDUWIT", 3_000 * scale, 0,
        f"Disburse qris duwit atas transaksi pembayaran qris pada tanggal {qris_date} sejumlah rp {3000 * scale}")
    add(base + "REV", "REVERSAL", 2_000 * scale, 0,
        f"Biaya Pembelian Recharge sejumlah {2000 * scale} rupiah, dari 412110 ke {code}")
    add(base + "REVFEE", "REVERSAL", 20, 0, "Platform fee recharge rp. 20,-")
    add(base + "DUPE", "RECHARGE", 500, 0, "Transaksi Sellthru")
    duplicate = dict(rows[-1])
    duplicate["No"] += 1
    rows.append(duplicate)
    add(base + "NGRS", "RECHARGE", 1_200 * scale, 0,
        f"Biaya Pembelian Recharge sejumlah {1200 * scale} rupiah, dari 412110 ke {code}")
    add(base + "NGRSFEE", "RECHARGEFEE", 0, 20, "Platform fee recharge rp. 20,-")
    add(base + "PURCHASE", "RECHARGE", 800 * scale, 0,
        f"Biaya Pembelian Recharge Out Cluster sejumlah {800 * scale} rupiah, dari 412110 ke {code}")
    add(base + "ACCOUNTING", "DISBURSEMENT", 500, 0,
        "Sales fee payment untuk perdana sebanyak 5 sejumlah 500 rupiah")
    return rows


def configuration(writer):
    target = {"host": "127.0.0.1", "port": PORT, "database": DATABASE, "schema": SCHEMA, "user": ROLE}
    account = asdict(CellReportConfig("412110_B", ("412111", "412112"), DATES[0],
        DATES[0] + "T00:00:00+08:00", {"412111": 1_000_000, "412112": 2_000_000},
        "SYNTHETIC-INVOICE-CANARY-20261016"))
    account["cell_codes"] = list(account["cell_codes"])
    return {"schema_version": 2, "dummy_data_only": False, "data_classification": "SYNTHETIC_CANARY",
        "spreadsheet_id": WORKBOOK, "approved_v2_spreadsheet_ids": [WORKBOOK],
        "legacy_spreadsheet_id": "SYNTHETIC_V1_NOT_PUBLISHED",
        "writer_email": writer, "finance_emails": [writer], "accounts": [account],
        "database_target": target, "approved_database_targets": [target],
        "worksheet_targets": {"412110_B": {role: {"worksheet_id": ident, "title": title}
            for role, (ident, title) in TARGETS.items()}}}


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")


def prepare(root, credential_file):
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("canary preparation already exists; refusing implicit replacement")
    info = json.loads(credential_file.read_text())
    config = root / "configuration.json"
    dump(config, configuration(info["client_email"]))
    password = root / "db-password"
    with os.fdopen(os.open(password, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as handle:
        handle.write(secrets.token_urlsafe(32))
    cells = configured_cells({"412110_B": ("412111", "412112")})
    hashes = {}
    for day in DATES:
        directory = root / day
        directory.mkdir(exist_ok=True)
        entries = []
        for cell in cells:
            rows = synthetic_rows(day, cell.cell_code)
            entry = {**cell.to_dict(), "report_date": day, "status": "validated" if rows else "empty",
                "validated_rows": len(rows), "footer_total": len(rows), "artifact": None}
            if rows:
                entry["artifact"] = f"{cell.account}-{cell.cell_code}.xlsx"
                artifact = directory / entry["artifact"]
                _write_xhr_export(artifact, [[row[key] for key in HEADERS] for row in rows])
                entry["source_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            else:
                entry["source_sha256"] = fingerprint({**cell.to_dict(), "report_date": day, "data": []})
            entries.append(entry)
        manifest = {"schema_version": 1, "report_date": day,
            "roster_fingerprint": roster_fingerprint(cells), "cells": entries}
        bundle = directory / "cell_sources_v2.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for entry in entries:
                if entry["artifact"]:
                    archive.write(directory / entry["artifact"], arcname=entry["artifact"])
        hashes[day] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    pins = {"configuration": hashlib.sha256(config.read_bytes()).hexdigest(),
        "writer": hashlib.sha256(info["client_email"].encode()).hexdigest(),
        "bundles": hashes, "bundle_set": fingerprint(hashes)}
    dump(root / "pins.json", pins)
    print(json.dumps(pins, sort_keys=True))


def counts(connection):
    return {name: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
        sql.Identifier(SCHEMA), sql.Identifier("finpay_cell_" + name + "_v1"))).fetchone()[0]
        for name in ("sources", "ledger", "current", "calculations")}


def execute_canary(args):
    root = args.root
    config = root / "configuration.json"
    info = json.loads(args.credential_file.read_text())
    if (hashlib.sha256(config.read_bytes()).hexdigest() != args.approved_configuration_sha256
        or json.loads(config.read_text()) != configuration(info["client_email"])
        or hashlib.sha256(info["client_email"].encode()).hexdigest() != args.approved_writer_sha256):
        raise ValueError("canary config/principal does not match the fixed scope and independent pins")
    bundles = {day: root / day / "cell_sources_v2.zip" for day in DATES}
    hashes = {day: hashlib.sha256(bundle.read_bytes()).hexdigest() for day, bundle in bundles.items()}
    if fingerprint(hashes) != args.approved_bundle_set_sha256:
        raise ValueError("synthetic bundle set differs from independently supplied pin")
    report_path = root / "result.json"
    resume = bool(args.approved_complete_attempt_sha256)
    previous = None
    if report_path.exists():
        content = report_path.read_bytes()
        if not resume or hashlib.sha256(content).hexdigest() != args.approved_complete_attempt_sha256:
            raise ValueError("canary already attempted; no implicit retry or reconciliation")
        previous = json.loads(content)
        if (previous.get("status") != "FAILED" or previous.get("error_class") != "AssertionError"
            or previous.get("diagnostic") != "identical four-tab rerun changed epoch or duplicated evidence"
            or previous.get("workbook") != WORKBOOK
            or previous.get("configuration_sha256") != args.approved_configuration_sha256
            or previous.get("created_tab_ids") != sorted(value[0] for value in TARGETS.values())
            or previous.get("checks", {}).get("db_only_rerun") != "PASS"):
            raise ValueError("only the proven completed range-drift attempt can be resumed")
        with (root / "initial-failed-result.json").open("xb") as handle:
            handle.write(content)
    elif resume:
        raise ValueError("approved previous completed attempt is missing")
    report = {"status": "RUNNING", "workbook": WORKBOOK, "classification": "SYNTHETIC_CANARY",
        "configuration_sha256": args.approved_configuration_sha256, "checks": {},
        "links": {role: f"https://docs.google.com/spreadsheets/d/{WORKBOOK}/edit#gid={ident}"
                  for role, (ident, _) in TARGETS.items()}}
    if resume:
        report["resumed_from_sha256"] = args.approved_complete_attempt_sha256
        report["checks"].update(previous["checks"])
    dump(report_path, report)
    connection = None
    try:
        password = (root / "db-password").read_text().strip()
        connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname=DATABASE,
            user=ROLE, password=password, autocommit=True)
        if connection.execute("SELECT current_database(),current_user").fetchone() != (DATABASE, ROLE):
            raise ValueError("disposable database identity mismatch")
        exists = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMA,)).fetchone()
        if bool(exists) != resume:
            raise ValueError("canary schema ownership differs from the fixed fresh/resume scope")
        if not resume:
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
                connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
                connection.execute(CELL_MIGRATION.read_text(), prepare=False)
        else:
            operations = connection.execute(sql.SQL("SELECT status,error_class FROM {}.finpay_cell_target_operations_v1 "
                "WHERE target_id=%s").format(sql.Identifier(SCHEMA)), (WORKBOOK,)).fetchall()
            states = connection.execute(sql.SQL("SELECT status FROM {}.finpay_cell_publications_v1 WHERE target_id=%s"
                ).format(sql.Identifier(SCHEMA)), (WORKBOOK,)).fetchall()
            if operations != [("IDLE", None)] or not states or any(row[0] != "COMPLETE" for row in states):
                raise ValueError("prior canary is not synchronously complete/quiescent; no retry permitted")
        report["checks"]["native_migration_003"] = "PASS"

        def run(day, sheets, *, configuration_only=False):
            with contextlib.redirect_stdout(io.StringIO()):
                return run_cell_reporting_v2(bundles[day], config,
                    args.approved_configuration_sha256, hashes[day],
                    approved_configuration_sha256=args.approved_configuration_sha256,
                    dry_run=False, write_gsheet=sheets, database_password=password,
                    credentials_info=info if sheets else None, configuration_only=configuration_only,
                    sheet_client_factory=lambda credentials: client,
                    output_dir=root / ("sheets" if sheets else "db") / day)

        if not resume:
            for day in DATES:
                run(day, False)
        expected = {"sources": 6, "ledger": sum(len(synthetic_rows(day, code)) for day in DATES
            for code in ("412111", "412112")), "current": 6, "calculations": 6}
        if counts(connection) != expected:
            raise AssertionError("native ingestion counts differ from complete synthetic cohort")
        if not resume:
            run(DATES[-1], False)
        if counts(connection) != expected:
            raise AssertionError("DB-only rerun duplicated source evidence")
        report["checks"].update(db_ingestion="PASS", db_only_rerun="PASS")
        report["db_counts"] = expected
        dump(report_path, report)

        client = make_cell_sheet_client(info)
        request = client.http_client.request
        last_request = 0.0

        def paced_request(*a, **kw):
            nonlocal last_request
            time.sleep(max(0.0, last_request + 1.1 - time.monotonic()))
            last_request = time.monotonic()
            return request(*a, **kw)

        client.http_client.request = paced_request
        workbook = client.open_by_key(WORKBOOK)
        metadata = workbook.fetch_sheet_metadata()
        if metadata["spreadsheetId"] != WORKBOOK or metadata["properties"]["title"] != "Salinan dari Monitoring Finpay & LinkAja":
            raise ValueError("approved COPY workbook identity mismatch")
        existing = {item["properties"]["sheetId"]: item for item in metadata["sheets"]}
        ids, titles = {value[0] for value in TARGETS.values()}, {value[1] for value in TARGETS.values()}
        original = {ident: item for ident, item in existing.items() if not resume or ident not in ids}
        if not resume and (ids & set(original) or any(item["properties"]["title"] in titles for item in original.values())):
            raise ValueError("new canary ID/title already exists; refusing overwrite")
        if not resume:
            workbook.batch_update({"requests": [{"addSheet": {"properties": {
                "sheetId": ident, "title": title, "gridProperties": {"rowCount": 300, "columnCount": 100}}}}
                for ident, title in TARGETS.values()]})
        report["created_tab_ids"] = sorted(ids)
        dump(report_path, report)

        def latest():
            row = connection.execute(sql.SQL("SELECT epoch,status,snapshot,snapshot_fingerprint FROM {}.finpay_cell_publications_v1 "
                "WHERE target_id=%s ORDER BY epoch DESC LIMIT 1").format(sql.Identifier(SCHEMA)), (WORKBOOK,)).fetchone()
            if not row or row[1] != "COMPLETE":
                raise AssertionError("four-output DB publication did not complete")
            return row

        def verify():
            epoch, status, snapshot, digest = latest()
            for role, (ident, title) in TARGETS.items():
                saved = snapshot["worksheets"][role]
                worksheet = workbook.get_worksheet_by_id(ident)
                adapter = ExistingSheetAdapter(workbook, worksheet)
                matrix, values = saved["matrix"], saved["values"]
                if worksheet.title != title or _padded(adapter.read(len(values), len(values[0])), len(values), len(values[0])) != values:
                    raise AssertionError("native four-tab value/formula readback differs from DB")
                meta = adapter.metadata()
                _verify_marker(meta, {"status": status, "epoch": epoch, "fingerprint": digest})
                _verify_final_protections(next_target(role, info["client_email"]), matrix, meta)
                actual = worksheet.get(f"A1:{column_letter(len(values[0]))}{len(values)}", value_render_option="UNFORMATTED_VALUE")
                expected_values = matrix["numeric_preview"] if role == "summary" else values
                if _padded(actual, len(values), len(values[0])) != expected_values:
                    raise AssertionError("Google formula evaluation differs from numeric projection")
                if any(value.startswith(("#REF!", "#ERROR!", "#VALUE!", "#DIV/0!"))
                       for row in actual for value in row if isinstance(value, str)):
                    raise AssertionError("Google formula error in synthetic output")
            report["epoch"] = epoch
            return snapshot

        if resume:
            verify()
            report["checks"]["prior_completed_attempt_readback"] = "PASS"
        run(DATES[-1], True)
        snapshot = verify()
        epoch = latest()[0]
        run(DATES[-1], True)
        if latest()[0] != epoch or counts(connection) != expected:
            raise AssertionError("identical four-tab rerun changed epoch or duplicated evidence")
        verify()
        report["checks"].update(four_tab_publication="PASS", native_formulas_and_protections="PASS", sheet_rerun="PASS")
        inputs = [entry for entry in snapshot["worksheets"]["summary"]["matrix"]["editable_inputs"]
                  if (entry["report_date"], entry["cell_code"]) in {(DATES[1], "412111"), (DATES[2], "412112")}]
        if len(inputs) != 2:
            raise AssertionError("canary requires two independently editable bank inputs")
        baseline = [snapshot["worksheets"]["summary"]["values"][entry["row"] - 1][entry["column"] - 1]
                    for entry in inputs]
        edits = []
        for entry, value in zip(inputs, (333, 444)):
            edits.append({"updateCells": {"range": {"sheetId": TARGETS["summary"][0],
                "startRowIndex": entry["row"] - 1, "endRowIndex": entry["row"],
                "startColumnIndex": entry["column"] - 1, "endColumnIndex": entry["column"]},
                "rows": [{"values": [{"userEnteredValue": {"numberValue": value}}]}], "fields": "userEnteredValue"}})
        workbook.batch_update({"requests": edits})
        run(DATES[-1], True, configuration_only=True)
        preserved = verify()["worksheets"]["summary"]["values"]
        if [preserved[item["row"] - 1][item["column"] - 1] for item in inputs] != [333, 444]:
            raise AssertionError("configuration-only run did not preserve manual bank inputs")
        run(DATES[-1], True)
        reset_values = verify()["worksheets"]["summary"]["values"]
        if [reset_values[item["row"] - 1][item["column"] - 1] for item in inputs] != baseline:
            raise AssertionError("source refresh did not restore both bank-input defaults")
        resets = connection.execute(sql.SQL("SELECT report_date,cell_code,previous_value,replacement_value "
            "FROM {}.finpay_cell_manual_resets_v1").format(sql.Identifier(SCHEMA))).fetchall()
        expected_resets = sorted((entry["report_date"], entry["cell_code"], old, new)
            for entry, old, new in zip(inputs, (333, 444), baseline))
        if sorted((str(day), code, old, new) for day, code, old, new in resets) != expected_resets or counts(connection) != expected:
            raise AssertionError("source refresh did not audit both bank resets without evidence duplication")
        report["checks"]["manual_preservation_reset_and_audit"] = "PASS"
        final = {item["properties"]["sheetId"]: item for item in workbook.fetch_sheet_metadata()["sheets"]}
        if set(final) != set(original) | ids or any(final[ident] != value for ident, value in original.items()):
            raise AssertionError("pre-existing tab metadata changed")
        workbook.batch_update({"requests": [{"updateCells": {"range": {"sheetId": ident,
            "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": "SYNTHETIC CANARY ONLY. FinPay-only; LinkAja excluded. Two cells, 13-15 Oct 2026. Disposable DB is temporary; not production or settlement approval."}]}],
            "fields": "note"}} for ident in ids]})
        report["checks"]["existing_tabs_preserved"] = "PASS"
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error_class"] = type(exc).__name__
        response = getattr(exc, "response", None)
        if response is not None:
            report["http_status"] = response.status_code
        elif isinstance(exc, (AssertionError, RuntimeError, ValueError)):
            report["diagnostic"] = str(exc)[:300]
        raise RuntimeError("Synthetic canary stopped; inspect sanitized result.json, do not blindly retry") from None
    finally:
        if connection is not None:
            connection.close()
        dump(report_path, report)
    print(json.dumps(report, sort_keys=True))


def next_target(role, writer):
    from finpay_pipeline.cell_publication import CellSheetTarget
    return CellSheetTarget(WORKBOOK, TARGETS[role][0], writer, (writer,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--approved-configuration-sha256")
    parser.add_argument("--approved-writer-sha256")
    parser.add_argument("--approved-bundle-set-sha256")
    parser.add_argument("--approved-complete-attempt-sha256")
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.root, args.credential_file)
    else:
        if not all((args.approved_configuration_sha256, args.approved_writer_sha256, args.approved_bundle_set_sha256)):
            parser.error("run requires all three independently supplied pins")
        execute_canary(args)


if __name__ == "__main__":
    main()
