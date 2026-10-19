"""Manual Oct 1-17 backfill acquisition, fixed current-user approved scope. No cron."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

BOOK = "1PbHtxmdr558BEnb9UT047WIMpBMnPgGkC3J9TGHG6Cg"
BOOK_NAME = "Salinan dari Salinan dari Monitoring Finpay & LinkAja"
OLD_BOOK = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
DATABASE, SCHEMA, USER, PORT = "finpay_live_design_e2e", "cell_october_2026", "finpay_live_design_test", 55433
WRITER_SHA = "2ab7954647e7cca1673131da541b0121e45d5c3226bcae5584e1b01dbce5d5b4"
DATES = tuple((date(2026, 10, 1) + timedelta(days=index)).isoformat() for index in range(17))
ROSTER = {"412110_B": ("412111", "412112"), "422010_B": ("422011", "422012", "422013"),
    "422040_B": ("422041", "422042", "422043"), "422090_B": ("422091", "422092"),
    "422100_B": ("422101", "422102"), "422140_B": ("422141", "422142"),
    "422180_B": ("422181", "422182"), "422220_B": ("422221", "422222")}
ALIASES = {"412110_B": "PKY", "422090_B": "MRT", "422010_B": "BGI", "422220_B": "TNT",
    "422100_B": "TDR", "422180_B": "MRW", "422140_B": "KOTAMOB", "422040_B": "BOLMONG"}
ROLES = ("summary", "invoice", "unusual", "reversal")
TARGETS = {account: {role: {"worksheet_id": 1900184101 + index * 4 + slot,
    "title": ALIASES[account] if role == "summary" else f"{ALIASES[account]} {role.title()}"}
    for slot, role in enumerate(ROLES)} for index, account in enumerate(sorted(ROSTER))}
CACHE_PIN = "eadee07cf1b5dc0a8373ceae539bdce72f614b562c31dfb7a48ee7051b3a5a6e"
LIMIT = 256 * 1024 * 1024


def event(stage, **values):
    print(json.dumps({"stage": stage, **values}, sort_keys=True), file=sys.__stdout__, flush=True)


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def private_root(root, *, create=False):
    if root.is_symlink():
        raise ValueError("backfill root cannot be a symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o777 != 0o700:
        raise ValueError("backfill root must be private and owned")


def dump(path, value):
    pending = path.with_name(path.name + ".pending")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    pending.replace(path)


def code_hashes():
    package = Path(__file__).resolve().parents[1]
    return {str(path.relative_to(package)): sha(path) for path in
            [*package.glob("*.py"), package / "migrations/003_finpay_cell_reporting_v1.sql", Path(__file__)]}


def check(root, pin):
    private_root(root)
    if not pin or sha(root / "configuration.json") != pin:
        raise ValueError("backfill config pin mismatch")
    if json.loads((root / "acquisition-code-pins.json").read_text()) != code_hashes():
        raise ValueError("acquisition/application source changed after preparation")
    return json.loads((root / "configuration.json").read_text())


def prepare(root, credential, cache):
    from dataclasses import asdict
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    from finpay_pipeline.cell_calculation import CellReportConfig
    from finpay_pipeline.cell_contracts import OBSERVED_CELL_ROSTER
    from finpay_pipeline.cell_publication_v2 import CLUSTER_ALIASES, cell_worksheet_title
    from finpay_pipeline.cell_workflow import read_cell_configuration_v2
    private_root(root, create=True)
    if any(root.iterdir()) or ROSTER != OBSERVED_CELL_ROSTER or ALIASES != CLUSTER_ALIASES:
        raise ValueError("backfill root not fresh or verified mappings changed")
    info = json.loads(credential.read_text())
    writer = info["client_email"]
    if hashlib.sha256(writer.encode()).hexdigest() != WRITER_SHA:
        raise ValueError("backfill writer principal changed")
    session = AuthorizedSession(Credentials.from_service_account_info(info,
        scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"]))
    response = session.get("https://www.googleapis.com/drive/v3/files/" + BOOK,
        params={"fields": "id,name,permissions(id,type,role),capabilities(canEdit)"}, timeout=45)
    response.raise_for_status()
    metadata = response.json()
    permissions = sorted(metadata.get("permissions", []), key=lambda item: item["id"])
    if (metadata.get("id"), metadata.get("name"), metadata.get("capabilities", {}).get("canEdit")) != (BOOK, BOOK_NAME, True) or sorted(
            (item["type"], item["role"]) for item in permissions) != [("user", "owner"), ("user", "writer")]:
        raise ValueError("new destination is not the approved private editable workbook")
    accounts = []
    for account, codes in sorted(ROSTER.items()):
        values = asdict(CellReportConfig(account, codes, DATES[0], DATES[0] + "T00:00:00+08:00",
            {code: 0 for code in codes}, "OCTOBER-NEW-CELLS-2026"))
        values["cell_codes"] = list(values["cell_codes"])
        accounts.append(values)
        for role in ROLES:
            if TARGETS[account][role]["title"] != cell_worksheet_title(account, role, "cluster_alias"):
                raise ValueError("alias title contract changed")
    target = {"host": "127.0.0.1", "port": PORT, "database": DATABASE, "schema": SCHEMA, "user": USER}
    runtime = {"schema_version": 2, "data_classification": "PRODUCTION", "dummy_data_only": False,
        "spreadsheet_id": BOOK, "approved_v2_spreadsheet_ids": [BOOK], "legacy_spreadsheet_id": OLD_BOOK,
        "worksheet_naming": "cluster_alias", "writer_email": writer, "finance_emails": [writer],
        "accounts": accounts, "worksheet_targets": TARGETS, "database_target": target,
        "approved_database_targets": [target]}
    dump(root / "configuration.json", runtime)
    pin = sha(root / "configuration.json")
    read_cell_configuration_v2(root / "configuration.json", pin, pin)
    dump(root / "acquisition-code-pins.json", code_hashes())
    dump(root / "destination-pins.json", {"permissions_sha256": hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()})
    receipts = {}
    if sha(cache / "sources.json") != CACHE_PIN:
        raise ValueError("verified source-cache receipt changed")
    for day, item in json.loads((cache / "sources.json").read_text()).items():
        if day not in ("2026-10-14", "2026-10-15", "2026-10-16"):
            raise ValueError("unexpected reusable source date")
        source = cache / "sources" / day / "cell_sources_v2.zip"
        if sha(source) != item["sha256"]:
            raise ValueError("reusable immutable source bundle changed")
        destination = root / "sources" / day
        destination.mkdir(parents=True)
        shutil.copyfile(source, destination / "cell_sources_v2.zip")
        receipts[day] = {**item, "origin": "validated-cache-20261017"}
    dump(root / "sources.json", receipts)
    dump(root / "result.json", {"status": "PREPARED", "configuration_sha256": pin, "dates": DATES,
        "workbook": BOOK, "schema": SCHEMA, "opening": "user-stated September30 close zero / October1 start zero",
        "reused_cell_days": 54})
    event("prepared", configuration_sha256=pin, reused_cell_days=54, missing_cell_days=252)


def acquire(root, pin, secret_file):
    check(root, pin)
    state = json.loads((root / "result.json").read_text())
    if state["status"] not in ("PREPARED", "ACQUIRING", "ACQUISITION_FAILED"):
        raise ValueError("cannot overwrite completed backfill acquisition")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from digipos_cells import scrape_cell_roster
    from cell_contracts import configured_cells, roster_fingerprint, validate_complete_manifest
    password = next((base64.b64decode(line.split("=", 1)[1].strip() + "===").decode()
        for line in secret_file.read_text().splitlines() if line.strip().startswith("SECRET_DIGIPOS_PASSWORD=")), None)
    if not password:
        raise ValueError("approved portal credential reference missing")
    state["status"] = "ACQUIRING"
    dump(root / "result.json", state)
    receipts = json.loads((root / "sources.json").read_text())
    for day in DATES:
        final = root / "sources" / day / "cell_sources_v2.zip"
        if day in receipts:
            if sha(final) != receipts[day]["sha256"]:
                raise ValueError("completed source checkpoint changed")
            event("source_reused", report_date=day, rows=receipts[day]["rows"])
            continue
        event("source_day_started", report_date=day, isolated_account_workers=3)
        results = {}
        def account_job(account):
            manifest = scrape_cell_roster(day, password, {account: ROSTER[account]},
                str(root / "accounts" / account), googleapis_compatibility=True)
            event("source_account_validated", report_date=day, account=account,
                rows=sum(item["validated_rows"] for item in manifest["cells"]))
            return manifest
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(account_job, account): account for account in sorted(ROSTER)}
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
        cells = [item for account in sorted(results) for item in results[account]["cells"]]
        manifest = {"schema_version": 1, "report_date": day,
            "roster_fingerprint": roster_fingerprint(configured_cells(ROSTER)), "cells": cells}
        validate_complete_manifest(manifest, configured_cells(ROSTER))
        final.parent.mkdir(parents=True, exist_ok=True)
        dump(final.parent / "manifest.json", manifest)
        paths = [(root / "accounts" / item["account"] / day / item["artifact"], item["artifact"])
                 for item in cells if item["artifact"]]
        if any(path.resolve().parent != (root / "accounts" / item["account"] / day).resolve()
               for item in cells if item["artifact"] for path in [root / "accounts" / item["account"] / day / item["artifact"]]):
            raise ValueError("source artifact path escaped its account/day")
        if sum(path.stat().st_size + 4096 for path, name in paths) + len(json.dumps(manifest).encode()) > LIMIT:
            raise ValueError("backfill bundle exceeds runtime bound")
        pending = final.with_suffix(".pending.zip")
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for path, name in paths:
                archive.write(path, arcname=name)
        if pending.stat().st_size > LIMIT:
            raise ValueError("backfill archive exceeds runtime bound")
        pending.replace(final)
        receipts[day] = {"sha256": sha(final), "cells": 18,
            "rows": sum(item["validated_rows"] for item in cells), "empty_cells": sum(item["status"] == "empty" for item in cells),
            "origin": "live-portal"}
        dump(root / "sources.json", receipts)
        event("source_day_complete", report_date=day, **receipts[day])
    if set(receipts) != set(DATES) or sum(item["cells"] for item in receipts.values()) != 306:
        raise ValueError("backfill source coverage incomplete")
    state.update(status="ACQUIRED", source_receipt_sha256=sha(root / "sources.json"),
        source_summary=receipts, source_rows=sum(item["rows"] for item in receipts.values()))
    dump(root / "result.json", state)
    event("all_sources_complete", cell_days=306, rows=state["source_rows"], source_receipt_sha256=state["source_receipt_sha256"])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "acquire"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration-sha256")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--portal-secret-file", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            prepare(args.root, args.credential_file, args.cache_root)
        else:
            acquire(args.root, args.configuration_sha256, args.portal_secret_file)
    except Exception as exc:
        path = args.root / "result.json"
        if path.exists():
            state = json.loads(path.read_text())
            if state.get("status") == "ACQUIRING":
                state.update(status="ACQUISITION_FAILED", error_class=type(exc).__name__)
                dump(path, state)
        raise RuntimeError(f"backfill {args.phase} stopped: {type(exc).__name__}; preserve checkpoints") from None


if __name__ == "__main__":
    main()
