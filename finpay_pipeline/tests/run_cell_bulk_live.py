"""Opt-in, fixed-scope October bulk run; credentials and evidence stay private."""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import string
import time
import zipfile
from urllib.parse import unquote, urlparse


START, END = "2026-10-01", "2026-10-19"
RUN_ID = "bulk20261020v1"
ROOT_NAME = "cell_" + RUN_ID
RUN_NAME = "finpay-" + RUN_ID
DB_NAME, DB_USER, DB_MIGRATOR = "finpay_" + RUN_ID, "finpay_bulk_app", "finpay_bulk_migrator"
ACCOUNTS = ("412110_B", "422010_B", "422040_B", "422090_B", "422100_B", "422140_B", "422180_B", "422220_B")
WORKBOOK = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
TITLE = "Salinan dari Monitoring Finpay & LinkAja"
NAMESPACE, FLOW_ID = "finance.finpay." + RUN_ID, "finpay_cell_" + RUN_ID
RECOVERY_FLOW_ID = "finpay_cell_alias_publish_remaining20261020v3"
REPORT_IMAGE = "finpay-cell-reporting:bulk-20261020-alias-v2"
SCRAPER_IMAGE = "finpay-digipos-cell-scraper:bulk-20261020-alias-v2"
BULK_TASKS = ("preflight_cell_bulk_targets", "acquire_cell_bulk", "preprocess_cell_bulk",
              "persist_cell_bulk", "publish_cell_bulk")
spec = importlib.util.spec_from_file_location("_finpay_private_ops", Path(__file__).with_name("run_cell_single_day_e2e.py"))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
ops.EVIDENCE_DIRNAME = ROOT_NAME
ops.RUN_NAME = ops.NETWORK = RUN_NAME
ops.PG_CONTAINER, ops.KESTRA_CONTAINER = RUN_NAME + "-pg", RUN_NAME + "-kestra"
ops.DB_NAME, ops.DB_USER, ops.DB_MIGRATOR = DB_NAME, DB_USER, DB_MIGRATOR
ops.PG_ALIAS = RUN_NAME + "-postgres"
ops.NAMESPACE, ops.FLOW_ID = NAMESPACE, FLOW_ID
ops.FLOW_IMAGE, ops.SCRAPER_IMAGE = REPORT_IMAGE, SCRAPER_IMAGE


def scope():
    return {"start_date": START, "end_date": END, "accounts": list(ACCOUNTS),
            "workbook_id": WORKBOOK, "workbook_title": TITLE, "database": DB_NAME, "schema": "public"}


def save(state):
    ops.save_state(ops.evidence_root(), state)


def prepare():
    ops.prepare()
    root = ops.evidence_root()
    state = ops.load_state(root)
    for name in ("server-data", "postgres-data", "acquisition", "ops"):
        (root / name).mkdir(mode=0o700)
    configuration = f'''datasources:
  h2:
    url: jdbc:h2:file:/state/metadata;DB_CLOSE_ON_EXIT=FALSE
    username: kestra
    password: ""
    driver-class-name: org.h2.Driver
kestra:
  secrets:
    type: env
  server:
    basic-auth:
      enabled: true
      username: "${{KESTRA_BASIC_AUTH_USERNAME}}"
      password: "${{KESTRA_BASIC_AUTH_PASSWORD}}"
  repository:
    type: h2
  queue:
    type: h2
  storage:
    type: local
    local:
      base-path: /state/storage
  tasks:
    tmp-dir:
      path: {root}/taskworkdir/tmp
  plugins:
    configurations:
      - type: io.kestra.plugin.scripts.runner.docker.Docker
        values:
          volumeEnabled: true
          pullPolicy: IF_NOT_PRESENT
          networkMode: {RUN_NAME}
'''
    ops.secure_file(root / "server.yml", configuration.encode("utf-8"))
    state["prepared_at"] = time.time()
    state["scope"] = scope()
    save(state)
    ops.emit("bulk_prepared", resources=state["resources"], accounts=len(ACCOUNTS), dates=19)


def client_from_environment():
    from finpay_pipeline.cell_workflow import make_cell_sheet_client
    return make_cell_sheet_client(ops.google_credentials())


def provision_tabs():
    from finpay_pipeline.cell_format.settings import CELL_ROSTER
    from finpay_pipeline.cell_publication_v2 import ROLES, cell_worksheet_title

    root = ops.evidence_root()
    state = ops.load_state(root)
    ops.require_state(state, "PREPARED")
    if set(CELL_ROSTER) != set(ACCOUNTS):
        raise ops.Blocked("runtime_roster_differs_from_approved_eight_accounts")
    client = client_from_environment()
    drive, meta = ops.drive_metadata(client), ops.workbook_metadata(client)
    if (drive.get("id"), drive.get("name"), meta.get("spreadsheetId"), meta.get("properties", {}).get("title")) != (WORKBOOK, TITLE, WORKBOOK, TITLE):
        raise ops.Blocked("workbook_identity_mismatch")
    if not drive.get("capabilities", {}).get("canEdit"):
        raise ops.Blocked("writer_cannot_edit_workbook")
    expected = {account: {role: cell_worksheet_title(account, role, "cluster_alias") for role in ROLES} for account in ACCOUNTS}
    by_title = collections.defaultdict(list)
    used = set()
    for sheet in meta.get("sheets", []):
        by_title[sheet["properties"]["title"]].append(sheet)
        used.add(int(sheet["properties"]["sheetId"]))
    targets, existing, requests = {}, {}, []
    next_id = 1910200000
    for account in ACCOUNTS:
        targets[account] = {}
        for role, title in expected[account].items():
            matches = by_title[title]
            if len(matches) > 1:
                raise ops.Blocked("duplicate_target_title")
            if matches:
                sheet = matches[0]
                full = ops.full_sheet(client, sheet["properties"])
                populated = any(cell.get("userEnteredValue") or cell.get("note") for row in ops.grid_rows(full) for cell in row.get("values", []))
                if populated or sheet.get("protectedRanges") or sheet.get("merges") or sheet.get("developerMetadata"):
                    raise ops.Blocked("existing_managed_target_requires_its_matching_database")
                sheet_id = int(sheet["properties"]["sheetId"])
                existing[str(sheet_id)] = full
            else:
                while next_id in used:
                    next_id += 1
                sheet_id = next_id
                used.add(sheet_id)
                requests.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title,
                    "gridProperties": {"rowCount": 1000, "columnCount": 20}}}})
            targets[account][role] = sheet_id
    target_ids = {value for roles in targets.values() for value in roles.values()}
    permissions = ops.permission_list(drive)
    writer = ops.google_credentials()["client_email"]
    finance = sorted({item["emailAddress"] for item in permissions if item.get("type") == "user"
                      and item.get("role") in ("owner", "writer") and item.get("emailAddress")
                      and item.get("emailAddress") != writer})
    if not finance:
        raise ops.Blocked("no_existing_human_owner_or_finance_editor")
    unrelated_grids = {}
    for sheet in meta.get("sheets", []):
        sheet_id = int(sheet["properties"]["sheetId"])
        if sheet_id not in target_ids:
            grid = ops.full_sheet(client, sheet["properties"])
            ops.write_json(root / f"unrelated-sheet-{sheet_id}.json", grid)
            unrelated_grids[str(sheet_id)] = ops.digest(grid)
    backup = {"scope": scope(), "metadata": meta, "permissions": permissions, "existing_target_grids": existing,
              "unrelated_grid_sha256": unrelated_grids,
              "other_tabs_sha256": ops.digest(ops.other_tabs_snapshot(meta, target_ids)),
              "permissions_sha256": ops.digest(permissions), "target_ids": targets}
    ops.write_json(root / "workbook-backup.json", backup)
    state["backup_sha256"] = ops.sha256_bytes((root / "workbook-backup.json").read_bytes())
    if json.loads((root / "workbook-backup.json").read_text()) != backup:
        raise ops.Blocked("backup_readback_mismatch")
    ops.write_json(root / "profile.json", {"workbook_id": WORKBOOK, "legacy_workbook_id": ops.LEGACY_WORKBOOK_ID,
        "writer_email": writer, "finance_emails": finance, "targets": targets, "titles": expected,
        "roster": {account: list(CELL_ROSTER[account]) for account in ACCOUNTS}})
    state["target_ids"] = targets
    state["state"] = "TABS_CREATE_IN_FLIGHT"
    save(state)
    try:
        if requests:
            client.open_by_key(WORKBOOK).batch_update({"requests": requests})
    except Exception:
        state["state"] = "TABS_CREATE_UNKNOWN"
        save(state)
        raise ops.Blocked("tab_creation_requires_read_only_reconciliation") from None
    after = ops.workbook_metadata(client)
    actual = {int(sheet["properties"]["sheetId"]): sheet["properties"]["title"] for sheet in after.get("sheets", [])}
    if any(actual.get(targets[a][r]) != expected[a][r] for a in ACCOUNTS for r in ROLES):
        raise ops.Blocked("created_tab_identity_readback_mismatch")
    if ops.digest(ops.other_tabs_snapshot(after, target_ids)) != backup["other_tabs_sha256"]:
        raise ops.Blocked("unrelated_metadata_changed_during_creation")
    if ops.digest(ops.permission_list(ops.drive_metadata(client))) != backup["permissions_sha256"]:
        raise ops.Blocked("sharing_changed_during_creation")
    state.update(state="BACKUP_VERIFIED", created_tabs=len(requests), selected_tabs=len(target_ids),
                 finance_editor_count=len(finance), target_profile_sha256=ops.sha256_bytes((root / "profile.json").read_bytes()))
    save(state)
    ops.emit("bulk_tabs_initialized", created=len(requests), selected=len(target_ids), unrelated_tabs=len(after["sheets"]) - len(target_ids))


def backup_unrelated():
    """Capture unchanged workbook grids after a failure before acquisition."""
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if state.get("execution_state") != "FAILED" or {task["id"] for task in state.get("native_tasks", [])} != {BULK_TASKS[0]}:
        raise ops.Blocked("unrelated_backup_requires_preflight_only_failure")
    if any(task.get("state") != "FAILED" for task in state["native_tasks"]):
        raise ops.Blocked("unrelated_backup_execution_state_changed")
    client = client_from_environment()
    meta, drive = ops.workbook_metadata(client), ops.drive_metadata(client)
    baseline = ops.read_json(root / "workbook-backup.json")
    target_ids = {int(value) for group in state["target_ids"].values() for value in group.values()}
    if ops.digest(ops.other_tabs_snapshot(meta, target_ids)) != baseline["other_tabs_sha256"]:
        raise ops.Blocked("unrelated_tab_metadata_changed_before_source_run")
    if ops.digest(ops.permission_list(drive)) != baseline["permissions_sha256"]:
        raise ops.Blocked("workbook_sharing_changed_before_source_run")
    original_other = {int(sheet["properties"]["sheetId"]) for sheet in baseline["metadata"].get("sheets", [])
                      if int(sheet["properties"]["sheetId"]) not in target_ids}
    current = {int(sheet["properties"]["sheetId"]): sheet["properties"] for sheet in meta.get("sheets", [])}
    if not original_other or not original_other <= (set(current) - target_ids):
        raise ops.Blocked("original_unrelated_tabs_missing")
    hashes = {}
    for sheet_id in sorted(original_other):
        path = root / f"unrelated-sheet-{sheet_id}.json"
        if path.exists():
            raise ops.Blocked("unrelated_backup_already_exists")
        grid = ops.full_sheet(client, current[sheet_id])
        ops.write_json(path, grid, exclusive=True)
        hashes[str(sheet_id)] = ops.digest(grid)
    baseline["unrelated_grid_sha256"] = hashes
    ops.write_json(root / "workbook-backup.json", baseline)
    state["unrelated_grid_sha256"] = hashes
    state["backup_sha256"] = ops.sha256_bytes((root / "workbook-backup.json").read_bytes())
    save(state)
    ops.emit("unrelated_worksheet_backup_complete", sheets=len(hashes), grids="private_checksum_verified")


_AUDIT = {"last_request": 0.0, "requests": 0, "mutations": 0, "sheet_ids": set(), "protected": {}}


def scoped_sheet_client(credentials_info):
    """Allow only this workbook's 32 targets; pace requests below per-user quota."""
    from finpay_pipeline.cell_workflow import make_cell_sheet_client
    client = make_cell_sheet_client(credentials_info)
    root = ops.evidence_root()
    profile = ops.read_json(root / "profile.json")
    allowed = {int(value) for group in profile["targets"].values() for value in group.values()}
    titles = {value for group in profile["titles"].values() for value in group.values()}
    original = client.http_client.request
    metadata = ops.workbook_metadata(client)
    for sheet in metadata.get("sheets", []):
        sheet_id = int(sheet["properties"]["sheetId"])
        for item in sheet.get("protectedRanges", []):
            _AUDIT["protected"][int(item["protectedRangeId"])] = sheet_id

    def ids(value):
        found = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "sheetId":
                    found.add(int(item))
                else:
                    found.update(ids(item))
        elif isinstance(value, list):
            for item in value:
                found.update(ids(item))
        return found

    def title_id(a1):
        title = unquote(a1).split("!", 1)[0].strip("'").replace("''", "'")
        if title not in titles:
            raise ops.Blocked("publication_value_range_outside_approved_tabs")
        return {int(profile["targets"][account][role]) for account in ACCOUNTS
                for role, value in profile["titles"][account].items() if value == title}

    def request(method, endpoint, *args, **kwargs):
        mutating = method.lower() not in ("get", "head", "options")
        body, touched = kwargs.get("json"), set()
        if mutating:
            parsed = urlparse(endpoint)
            prefix = "/v4/spreadsheets/" + WORKBOOK
            if parsed.hostname != "sheets.googleapis.com" or not parsed.path.startswith(prefix):
                raise ops.Blocked("publication_mutation_outside_approved_workbook")
            suffix = unquote(parsed.path[len(prefix):])
            if suffix == ":batchUpdate":
                permitted = {"repeatCell", "updateCells", "updateBorders", "mergeCells", "unmergeCells",
                             "updateDimensionProperties", "autoResizeDimensions", "updateSheetProperties",
                             "addProtectedRange", "updateProtectedRange", "deleteProtectedRange"}
                if not isinstance(body, dict) or not isinstance(body.get("requests"), list):
                    raise ops.Blocked("publication_mutation_shape_unknown")
                for item in body["requests"]:
                    if len(item) != 1 or not set(item) <= permitted:
                        raise ops.Blocked("publication_request_kind_not_scope_reviewed")
                    current = ids(item)
                    name, values = next(iter(item.items()))
                    if not current and name in ("deleteProtectedRange", "updateProtectedRange"):
                        identifier = values.get("protectedRangeId", values.get("protectedRange", {}).get("protectedRangeId"))
                        if identifier not in _AUDIT["protected"]:
                            raise ops.Blocked("publication_protection_owner_unknown")
                        current = {_AUDIT["protected"][identifier]}
                    if not current or not current <= allowed:
                        raise ops.Blocked("publication_mutation_outside_approved_tabs")
                    touched.update(current)
            elif suffix.startswith("/values/"):
                touched = title_id(suffix[len("/values/"):])
            elif suffix == "/values:batchUpdate":
                for item in (body or {}).get("data", []):
                    touched.update(title_id(item.get("range", "")))
                if not touched:
                    raise ops.Blocked("publication_value_ranges_missing")
            else:
                raise ops.Blocked("publication_endpoint_not_scope_reviewed")
        delay = 1.1 - (time.monotonic() - _AUDIT["last_request"])
        if delay > 0:
            time.sleep(delay)
        _AUDIT["last_request"] = time.monotonic()
        response = original(method, endpoint, *args, **kwargs)
        _AUDIT["requests"] += 1
        if mutating:
            _AUDIT["mutations"] += 1
            _AUDIT["sheet_ids"].update(touched)
            if body and "requests" in body:
                for item, reply in zip(body["requests"], response.json().get("replies", [])):
                    if "addProtectedRange" in item:
                        added = reply.get("addProtectedRange", {}).get("protectedRange", {})
                        if "protectedRangeId" in added:
                            _AUDIT["protected"][int(added["protectedRangeId"])] = item["addProtectedRange"]["protectedRange"]["range"]["sheetId"]
            ops.write_json(root / "audit" / "publication-mutation-audit.json", {
                "requests": _AUDIT["requests"], "mutations": _AUDIT["mutations"],
                "sheet_ids": sorted(_AUDIT["sheet_ids"]), "allowed_sheet_ids": sorted(allowed),
                "scope_enforced": True,
            })
        return response

    client.http_client.request = request
    return client


def render_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    ops.require_state(state, "DATABASE_READY", "FLOW_RENDERED", "EXECUTION_TERMINAL")
    source = Path(os.environ["E2E_WORKFLOW_DIR"]) / "finpay_cell_bulk_v1.yml"
    text = source.read_text()
    text = text.replace("id: finpay_cell_bulk_v1\n", f"id: {FLOW_ID}\n", 1)
    text = text.replace("namespace: finance.finpay\n", f"namespace: {NAMESPACE}\n", 1)
    text = text.replace("finpay-cell-reporting:2.0", REPORT_IMAGE).replace("finpay-digipos-cell-scraper:2.0", SCRAPER_IMAGE)
    profile = ops.read_json(root / "profile.json")
    variables = (
        "variables:\n"
        "  cell_cluster_group: \"ALL\"\n"
        f"  cell_spreadsheet_id: {json.dumps(WORKBOOK)}\n"
        f"  cell_spreadsheet_name: {json.dumps(TITLE)}\n"
        f"  legacy_spreadsheet_id: {json.dumps(ops.LEGACY_WORKBOOK_ID)}\n"
        f"  cell_worksheet_ids_json: {json.dumps(json.dumps(profile['targets'], separators=(',', ':')))}\n"
        "  create_spreadsheet_if_missing: false\n"
        "  create_worksheets_if_missing: false\n"
    )
    text, variable_count = re.subn(r"(?m)^variables:\n(?:^  [^\n]*\n)+", variables, text, count=1)
    if variable_count != 1:
        raise ops.Blocked("isolated_flow_target_variable_block_missing")
    replacements = {
        'CELL_FORMAT_WRITER_EMAIL: "{{ secret(\'FINPAY_CELL_WRITER_EMAIL\') }}"':
            "CELL_FORMAT_WRITER_EMAIL: " + json.dumps(profile["writer_email"]),
        'CELL_FORMAT_FINANCE_EMAILS: "{{ secret(\'FINPAY_CELL_FINANCE_EMAILS\') }}"':
            "CELL_FORMAT_FINANCE_EMAILS: '" + json.dumps(profile["finance_emails"], separators=(",", ":")) + "'",
    }
    for old, new in replacements.items():
        if old not in text:
            raise ops.Blocked("isolated_flow_private_target_env_marker_missing")
        text = text.replace(old, new)
    prefix = ["from finpay_pipeline.cell_format import settings as _settings",
              "_settings.CELL_FORMAT_NAMING_SCHEME = 'cluster_alias'",
              f"_settings.CELL_FORMAT_SPREADSHEET_ID = {WORKBOOK!r}",
              f"_settings.LEGACY_SPREADSHEET_ID = {ops.LEGACY_WORKBOOK_ID!r}",
              f"_settings.CELL_FORMAT_WRITER_EMAIL = {profile['writer_email']!r}",
              f"_settings.CELL_FORMAT_FINANCE_EMAILS = {profile['finance_emails']!r}",
              f"_settings.CELL_FORMAT_WORKSHEET_IDS = {profile['targets']!r}"]
    seen = []

    def task(match):
        identifier, body = match.group(1), match.group(2)
        seen.append(identifier)
        body = re.sub(r"(?m)^    taskRunner:\n(?:^[ \t]{6,}[^\n]*\n|^\n)*", "", body)
        runner = ["    taskRunner:", "      type: io.kestra.plugin.scripts.runner.docker.Docker",
                  "      pullPolicy: IF_NOT_PRESENT",
                  "      networkMode: " + ("host" if identifier == "acquire_cell_bulk" else RUN_NAME)]
        body, count = re.subn(r"(?m)^(    containerImage:[^\n]*\n)",
                             lambda item: item.group(1) + "\n".join(runner) + "\n", body)
        if count != 1 or body.count("    taskRunner:") != 1 or "    env:\n" not in body:
            raise ops.Blocked("isolated_bulk_task_structure_changed")
        body = body.replace("    script: |\n", "    script: |\n" +
                            "".join("      " + line + "\n" for line in prefix), 1)
        return "  - id: " + identifier + "\n" + body

    header, task_source = text.split("\ntasks:\n", 1)
    text = header + "\ntasks:\n" + re.sub(
        r"(?ms)^  - id: ([^\n]+)\n(.*?)(?=^  - id: |\Z)", task, task_source,
    )
    if tuple(seen) != BULK_TASKS:
        raise ops.Blocked("isolated_bulk_task_set_changed")
    ops.secure_file(root / "isolated-bulk-flow.yml", text.encode("utf-8"))
    state["flow_sha256"] = ops.sha256_bytes(text.encode())
    state["state"] = "FLOW_RENDERED"
    save(state)


def configure_server():
    root = ops.evidence_root()
    lines = (root / "server.yml").read_text().splitlines()
    values = {}
    for line in (root / "kestra.env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("KESTRA_BASIC_AUTH_USERNAME", "KESTRA_BASIC_AUTH_PASSWORD"):
            if key in values or not value:
                raise ops.Blocked("isolated_server_basic_auth_config_invalid")
            values[key] = value
    if set(values) != {"KESTRA_BASIC_AUTH_USERNAME", "KESTRA_BASIC_AUTH_PASSWORD"}:
        raise ops.Blocked("isolated_server_basic_auth_config_missing")
    section_indent, seen = None, set()
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if line.strip() == "basic-auth:":
            section_indent = indent
            continue
        if section_indent is None or (line.strip() and indent <= section_indent):
            if section_indent is not None and line.strip() and indent <= section_indent:
                section_indent = None
            continue
        key, separator, _ = line.strip().partition(":")
        env_key = {"username": "KESTRA_BASIC_AUTH_USERNAME", "password": "KESTRA_BASIC_AUTH_PASSWORD"}.get(key)
        if separator and env_key in values:
            lines[index] = line[:len(line) - len(line.lstrip())] + key + ": " + json.dumps(values[env_key])
            seen.add(env_key)
    if seen != set(values):
        raise ops.Blocked("isolated_server_basic_auth_section_invalid")
    configuration = "\n".join(lines) + "\n"
    if "${KESTRA_BASIC_AUTH_" in configuration:
        raise ops.Blocked("isolated_server_basic_auth_placeholder_unresolved")
    ops.secure_file(root / "server.yml", configuration.encode("utf-8"))
    save(ops.load_state(root))
    ops.emit("isolated_server_configured", auth="private_config_file", permissions="0600")


def fix_auth():
    root = ops.evidence_root()
    username = "finpay-e2e@example.test"
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(40))
        if (any(char.isupper() for char in password) and any(char.islower() for char in password)
                and any(char.isdigit() for char in password)):
            break
    for filename, changes in (
        ("kestra.env", {"KESTRA_BASIC_AUTH_USERNAME": username, "KESTRA_BASIC_AUTH_PASSWORD": password}),
        ("controller.env", {"E2E_KESTRA_USER": username, "E2E_KESTRA_PASSWORD": password}),
    ):
        path = root / filename
        lines, seen = [], set()
        for line in path.read_text().splitlines():
            key = line.partition("=")[0]
            if key in changes:
                if key in seen:
                    raise ops.Blocked("duplicate_isolated_auth_variable")
                line = key + "=" + changes[key]
                seen.add(key)
            lines.append(line)
        if seen != set(changes):
            raise ops.Blocked("isolated_auth_variable_missing")
        ops.secure_file(path, ("\n".join(lines) + "\n").encode())
    configure_server()
    ops.emit("isolated_auth_refreshed", username="valid_test_email", password_policy="satisfied")


def register_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    ops.require_state(state, "FLOW_RENDERED")
    import requests
    base = os.environ["E2E_KESTRA_URL"]
    auth = (os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"])
    source = (root / "isolated-bulk-flow.yml").read_bytes()
    flow_url = base + f"/api/v1/main/flows/{NAMESPACE}/{FLOW_ID}"
    current = requests.get(flow_url, auth=auth, timeout=30)
    if current.status_code == 200:
        response = requests.put(flow_url, data=source,
                                headers={"Content-Type": "application/x-yaml"}, auth=auth, timeout=120)
    elif current.status_code == 404:
        response = requests.post(base + "/api/v1/main/flows", data=source,
                                 headers={"Content-Type": "application/x-yaml"}, auth=auth, timeout=120)
    else:
        raise ops.Blocked("native_flow_preupdate_read_failed_" + str(current.status_code))
    if response.status_code not in (200, 201):
        raise ops.Blocked("native_flow_registration_rejected_" + str(response.status_code))
    actual = requests.get(flow_url, auth=auth, timeout=60)
    if actual.status_code != 200 or actual.json().get("id") != FLOW_ID:
        raise ops.Blocked("native_flow_registration_readback_failed")
    state["state"] = "FLOW_REGISTERED"
    save(state)
    ops.emit("bulk_flow_registered", namespace=NAMESPACE, flow_id=FLOW_ID)


def execute():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    ops.require_state(state, "FLOW_REGISTERED")
    import requests
    state["state"] = "EXECUTION_POST_IN_FLIGHT"
    save(state)
    response = requests.post(os.environ["E2E_KESTRA_URL"] + f"/api/v1/main/executions/{NAMESPACE}/{FLOW_ID}",
        auth=(os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"]), timeout=120,
        files={key: (None, value) for key, value in {
            "start_date": START, "end_date": END, "cluster_id": "ALL", "scrape_workers": "2",
            "dry_run": "false", "write_gsheet": "true"}.items()})
    if response.status_code not in (200, 201):
        state["state"] = "EXECUTION_POST_REJECTED"
        save(state)
        raise ops.Blocked("native_execution_post_rejected_" + str(response.status_code))
    state.update(state="EXECUTION_RUNNING", execution_id=response.json()["id"], execution_started_at=time.time())
    save(state)
    ops.emit("bulk_execution_started", execution_id=state["execution_id"], cohorts=152, cell_days=342)


def status():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if not state.get("execution_id"):
        ops.emit("bulk_state", state=state["state"], resources=state.get("resources"))
        return
    import requests
    response = requests.get(os.environ["E2E_KESTRA_URL"] + "/api/v1/main/executions/" + state["execution_id"],
        auth=(os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"]), timeout=60)
    if response.status_code != 200:
        raise ops.Blocked("native_execution_readback_failed")
    result = response.json()
    ops.write_json(root / "execution-receipt.json", result)
    current = result.get("state", {}).get("current")
    tasks = [{"id": item["taskId"], "state": item.get("state", {}).get("current"),
              "duration": item.get("state", {}).get("duration")}
             for item in result.get("taskRunList", [])]
    state.update(execution_state=current, native_tasks=tasks)
    if current in ("SUCCESS", "FAILED", "KILLED", "WARNING", "CANCELLED"):
        state["state"] = "EXECUTION_TERMINAL"
    save(state)
    ops.emit("bulk_execution_status", execution_id=state["execution_id"], state=current, tasks=tasks)


def inspect_pending_publication():
    """Read-only diagnosis of the single failed first-cluster Sheets epoch."""
    import psycopg
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_publication import (
        GUARD_PREFIX, ExistingSheetAdapter, CellWorksheetTarget, _marker_entries,
        _verify_final_protections,
    )
    from finpay_pipeline.cell_publication_v2 import _verify_style, _verify_values

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if state.get("execution_id") != "24umLdqU273WDvk334WGQ9" or state.get("execution_state") != "FAILED":
        raise ops.Blocked("pending_inspection_requires_this_terminal_failed_execution")
    profile = ops.read_json(root / "profile.json")
    cluster, account = "412110", "412110_B"
    with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
            dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
        identity = connection.execute("SELECT current_database(),current_user,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)").fetchone()
        if identity != (DB_NAME, DB_USER, False):
            raise ops.Blocked("pending_inspection_database_identity_mismatch")
        rows = connection.execute("SELECT epoch,range_start::text,range_end::text,snapshot_fingerprint,status,error_class,confirmed_chunks,snapshot FROM finpay_cell_publications_v1 WHERE cluster_id=%s AND target_id=%s ORDER BY epoch", (cluster, WORKBOOK)).fetchall()
        target_op = connection.execute("SELECT operation_id,status,error_class FROM finpay_cell_target_operations_v1 WHERE target_id=%s", (WORKBOOK,)).fetchone()
        if len(rows) != 1 or rows[0][0:6] != (1, START, END, rows[0][3], "PENDING", "APIError"):
            raise ops.Blocked("pending_publication_state_not_exactly_reconciled_candidate")
        epoch, first, last, publication_hash, status_value, error_class, confirmed, snapshot = rows[0]
        expected_hash = fingerprint({"cluster_id": cluster, "target_id": WORKBOOK,
            "range_start": first, "range_end": last, "snapshot": snapshot})
        if publication_hash != expected_hash or not target_op or target_op[1:] != ("IDLE", "APIError"):
            raise ops.Blocked("pending_publication_fingerprint_or_operation_state_mismatch")
        summary = {"epoch": epoch, "status": status_value, "error_class": error_class,
                   "confirmed_chunk_count": len(confirmed), "fingerprint_valid": True}
        database_snapshot = snapshot

    client = client_from_environment()
    drive, workbook_meta = ops.drive_metadata(client), ops.workbook_metadata(client)
    backup = ops.read_json(root / "workbook-backup.json")
    all_target_ids = {int(value) for group in profile["targets"].values() for value in group.values()}
    unrelated_metadata_ok = ops.digest(ops.other_tabs_snapshot(workbook_meta, all_target_ids)) == backup["other_tabs_sha256"]
    sharing_unchanged = ops.digest(ops.permission_list(drive)) == backup["permissions_sha256"]
    if not unrelated_metadata_ok or not sharing_unchanged:
        raise ops.Blocked("workbook_scope_or_sharing_changed_during_publication")
    workbook_tabs = {int(item["properties"]["sheetId"]): item["properties"] for item in workbook_meta.get("sheets", [])}
    unrelated_content_unchanged = True
    for sheet_id, expected_hash in backup.get("unrelated_grid_sha256", {}).items():
        props = workbook_tabs.get(int(sheet_id))
        if props is None or ops.digest(ops.full_sheet(client, props)) != expected_hash:
            unrelated_content_unchanged = False
            break
    if not unrelated_content_unchanged:
        raise ops.Blocked("unrelated_worksheet_content_changed")
    owners = [item["emailAddress"] for item in ops.permission_list(drive)
              if item.get("role") == "owner" and item.get("emailAddress")]
    roles = {}
    for role, raw_id in profile["targets"][account].items():
        sheet_id, title = int(raw_id), profile["titles"][account][role]
        worksheet = client.open_by_key(WORKBOOK).get_worksheet_by_id(sheet_id)
        if worksheet.title != title or worksheet.id != sheet_id:
            raise ops.Blocked("pending_target_sheet_identity_mismatch")
        target = CellWorksheetTarget(role, WORKBOOK, sheet_id, title,
            profile["writer_email"], tuple(profile["finance_emails"]))
        adapter = ExistingSheetAdapter(client.open_by_key(WORKBOOK), worksheet)
        metadata = adapter.metadata()
        saved = database_snapshot["worksheets"][role]
        marker_items = _marker_entries(metadata)
        marker = json.loads(marker_items[0]["metadataValue"]) if len(marker_items) == 1 else None
        guards = [item for item in metadata.get("protectedRanges", [])
                  if item.get("description", "").startswith(GUARD_PREFIX)]
        values_ok = style_ok = protection_ok = False
        value_error = style_error = protection_error = None
        try:
            _verify_values(adapter, saved)
            values_ok = True
        except Exception as exc:
            value_error = type(exc).__name__
        try:
            _verify_style(adapter, saved["matrix"])
            style_ok = True
        except Exception as exc:
            style_error = type(exc).__name__
        try:
            _verify_final_protections(target.sheet_target(), saved["matrix"], metadata)
            protection_ok = True
        except Exception as exc:
            protection_error = type(exc).__name__
        props = {**metadata["properties"], "gridProperties": {
            **metadata["properties"].get("gridProperties", {}),
            "rowCount": len(saved["values"]),
            "columnCount": len(saved["values"][0]),
        }}
        ops.emit("pending_tab_precheck", role=role, rows=len(saved["values"]),
                 columns=len(saved["values"][0]), bounded_cells=len(saved["values"]) * len(saved["values"][0]))
        try:
            full = ops.full_sheet(client, props)
        except ops.Blocked as exc:
            raise ops.Blocked(f"pending_readback_grid_blocked_{role}_{len(saved['values'])}x{len(saved['values'][0])}") from None
        actual_rows = ops.grid_rows(full)
        matrix, formula_errors, formula_results, formula_mismatches = [], 0, 0, 0
        numeric = saved["matrix"].get("numeric_preview")
        for row_idx, expected_row in enumerate(saved["values"]):
            source = actual_rows[row_idx].get("values", []) if row_idx < len(actual_rows) else []
            actual_row = []
            for col_idx, expected_cell in enumerate(expected_row):
                cell = source[col_idx] if col_idx < len(source) else {}
                entered, effective = cell.get("userEnteredValue", {}), cell.get("effectiveValue", {})
                actual_row.append(ops.google_value(entered))
                if "formulaValue" in entered:
                    if "errorValue" in effective:
                        formula_errors += 1
                    elif role == "summary" and numeric is not None:
                        formula_results += 1
                        formula_mismatches += not ops.value_equal(numeric[row_idx][col_idx], ops.google_value(effective))
            matrix.append(actual_row)
        first_cell = (actual_rows[0].get("values", [{}])[0] if actual_rows and actual_rows[0].get("values") else {})
        note_metadata = adapter.spreadsheet.fetch_sheet_metadata(params={
            "includeGridData": "true", "ranges": f"'{title}'!A1:A1",
            "fields": "sheets(properties(sheetId,tabColorStyle),data(rowData(values(note))))",
        })
        note_sheets = note_metadata.get("sheets", [])
        if len(note_sheets) != 1 or note_sheets[0].get("properties", {}).get("sheetId") != sheet_id:
            raise ops.Blocked("publication_note_readback_identity_mismatch")
        note_row = note_sheets[0].get("data", [{}])[0].get("rowData", [{}])[0].get("values", [{}])[0]
        note = note_row.get("note", "")
        tab_color = note_sheets[0]["properties"].get("tabColorStyle", {}).get("rgbColor", {})
        note_state = "COMPLETE" if note.startswith(f"Sheet contents VERIFIED; epoch {epoch}.") else (
            "INCOMPLETE" if "INCOMPLETE" in note else "OTHER")
        opening_zero = None
        if role == "summary":
            preview = saved["matrix"].get("numeric_preview", [])
            blocks = [block for block in saved["matrix"].get("blocks", []) if block.get("cell_code")]
            opening_zero = len(blocks) == len(profile["roster"][account])
            for block in blocks:
                saldo_column = int(block["start_column"]) - 1 + 2
                opening_zero &= len(preview) > 2 and len(preview[2]) > saldo_column and preview[2][saldo_column] == 0
                opening_zero &= len(actual_rows) > 2 and len(actual_rows[2].get("values", [])) > saldo_column
                if len(actual_rows) > 2 and len(actual_rows[2].get("values", [])) > saldo_column:
                    opening_zero &= ops.google_value(actual_rows[2]["values"][saldo_column].get("userEnteredValue", {})) == 0
        roles[role] = {
            "sheet_id": sheet_id, "title_match": True, "marker_status": (marker or {}).get("status"),
            "marker_epoch_match": (marker or {}).get("epoch") == epoch,
            "marker_fingerprint_match": (marker or {}).get("fingerprint") == publication_hash,
            "guard_count": len(guards), "guard_operation_match": len(guards) == 1 and guards[0].get("description") == GUARD_PREFIX + target_op[0],
            "marker_note_state": note_state, "tab_color_green": tab_color.get("green", 0) > 0.5,
            "initial_saldo_zero": opening_zero,
            "values_ok": values_ok, "values_error": value_error, "readback_sha256": ops.digest(matrix),
            "style_ok": style_ok, "style_error": style_error,
            "final_protection_ok": protection_ok, "protection_error": protection_error,
            "formula_errors": formula_errors, "formula_results_checked": formula_results,
            "formula_result_mismatches": formula_mismatches,
        }
    result = {"execution_id": state["execution_id"], "database_publication": summary,
        "target_operation_status": target_op[1], "roles": roles,
        "read_only": True, "workbook_id": WORKBOOK, "cluster_id": cluster,
        "unrelated_metadata_unchanged": unrelated_metadata_ok,
        "unrelated_content_unchanged": unrelated_content_unchanged,
        "sharing_unchanged": sharing_unchanged}
    ops.write_json(root / "pending-publication-diagnosis.json", result)
    ops.emit("pending_publication_diagnosed", database_state=status_value,
        confirmed_chunks=summary["confirmed_chunk_count"], roles={key: {
            "marker": value["marker_status"], "values_ok": value["values_ok"],
            "style_ok": value["style_ok"], "protection_ok": value["final_protection_ok"],
            "formula_errors": value["formula_errors"],
            "formula_mismatches": value["formula_result_mismatches"],
            "initial_saldo_zero": value["initial_saldo_zero"]} for key, value in roles.items()},
        unrelated_content_unchanged=unrelated_content_unchanged, sharing_unchanged=sharing_unchanged)


def finalize_pending_publication():
    """Complete DB status only after an exact read-only four-tab verification."""
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_database import CellDatabaseTarget, cell_publication_session

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if state.get("execution_id") != "24umLdqU273WDvk334WGQ9" or state.get("execution_state") != "FAILED":
        raise ops.Blocked("reconciliation_requires_the_one_failed_bulk_execution")
    target, cluster = CellDatabaseTarget(os.environ["E2E_PG_HOST"],
        int(os.environ["E2E_PG_PORT"]), DB_NAME, "public", DB_USER), "412110"
    recovery_id = RUN_ID + "-readback-" + secrets.token_hex(8)
    with cell_publication_session(target, {target}, cluster, WORKBOOK,
            password=os.environ["E2E_PG_ROLE_PASSWORD"], approved_sheet_ids={WORKBOOK},
            write_gsheet=True) as store:
        latest = store.latest_publication(sheet_only=True)
        target_op = store.connection.execute(
            "SELECT operation_id,status,error_class FROM finpay_cell_target_operations_v1 WHERE target_id=%s",
            (WORKBOOK,),
        ).fetchone()
        if (not latest or latest["epoch"] != 1 or latest["status"] != "PENDING"
                or not target_op or target_op[1:] != ("IDLE", "APIError")):
            raise ops.Blocked("pending_publication_changed_before_readback_reconciliation")
        snapshot = latest["snapshot"]
        if snapshot.get("schema_version") != 2 or snapshot.get("spreadsheet_id") != WORKBOOK:
            raise ops.Blocked("pending_snapshot_identity_mismatch")
        results = store.current_results(START, END)
        if len(results) != 38:
            raise ops.Blocked("412110_history_coverage_not_38_cell_days")
        grouped = collections.defaultdict(list)
        for row in results:
            day = row["report_date"].isoformat() if hasattr(row["report_date"], "isoformat") else str(row["report_date"])
            grouped[day].append(row["calculation_id"])
        if {day: fingerprint(sorted(ids)) for day, ids in grouped.items()} != snapshot.get("source_calculations"):
            raise ops.Blocked("persisted_history_differs_from_pending_sheet_snapshot")
        expected_dates = [(f"2026-10-{day:02d}", "PENDING") for day in range(1, 20)]
        actual_dates = store.connection.execute(
            "SELECT report_date::text,status FROM finpay_cell_publication_dates_v1 "
            "WHERE cluster_id=%s AND target_id=%s AND epoch=1 ORDER BY report_date",
            (cluster, WORKBOOK),
        ).fetchall()
        if actual_dates != expected_dates:
            raise ops.Blocked("pending_publication_date_status_coverage_mismatch")
        # Re-read Sheets under the DB target lock; this performs no Google writes.
        inspect_pending_publication()
        proof = ops.read_json(root / "pending-publication-diagnosis.json")
        if (proof["database_publication"]["fingerprint_valid"] is not True
                or proof["database_publication"]["status"] != "PENDING"
                or proof["database_publication"]["error_class"] != "APIError"
                or proof["target_operation_status"] != "IDLE"
                or not proof["unrelated_metadata_unchanged"]
                or not proof["unrelated_content_unchanged"]
                or not proof["sharing_unchanged"]):
            raise ops.Blocked("pending_readback_proof_scope_or_state_invalid")
        for role, evidence in proof["roles"].items():
            if (evidence["marker_status"] != "COMPLETE" or not evidence["marker_epoch_match"]
                    or not evidence["marker_fingerprint_match"] or evidence["guard_count"] != 0
                    or not evidence["values_ok"] or not evidence["style_ok"]
                    or not evidence["final_protection_ok"] or evidence["formula_errors"] != 0
                    or evidence["formula_result_mismatches"] != 0 or evidence["marker_note_state"] != "COMPLETE"
                    or not evidence["tab_color_green"]):
                raise ops.Blocked("four_tab_readback_not_complete_for_" + role)
        if not proof["roles"]["summary"]["initial_saldo_zero"]:
            raise ops.Blocked("october_initial_sheet_saldo_not_zero")
        with store.connection.transaction():
            store.begin_target_operation(recovery_id)
            store.begin_sheet_write(1)
            store.complete(1, verified_fingerprint=latest["fingerprint"])
            store.finish_target_operation(recovery_id)
    receipt = {"action": "db_confirmation_after_independent_readback", "cluster_id": cluster,
        "epoch": 1, "fingerprint": latest["fingerprint"], "google_writes": 0,
        "verified_roles": sorted(proof["roles"]), "unrelated_content_unchanged": True,
        "sharing_unchanged": True, "target_operation_final": "IDLE",
        "readback_proof_sha256": ops.sha256_bytes((root / "pending-publication-diagnosis.json").read_bytes())}
    ops.write_json(root / "pending-publication-reconciliation.json", receipt)
    state["pending_publication_reconciled"] = True
    save(state)
    ops.emit("pending_publication_reconciled", cluster_id=cluster, epoch=1,
        google_writes=0, roles=sorted(proof["roles"]))


def rename_tabs_to_cluster_aliases():
    """Rename only the 32 initialized target tabs; preserve IDs and contents."""
    import psycopg
    from finpay_pipeline.cell_publication_v2 import CLUSTER_ALIASES, GUARD_PREFIX, ROLES, cell_worksheet_title
    from finpay_pipeline.cell_publication import PUBLICATION_METADATA_KEY

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    receipt = ops.read_json(root / "pending-publication-reconciliation.json")
    if receipt.get("cluster_id") != "412110" or receipt.get("epoch") != 1 or receipt.get("google_writes") != 0:
        raise ops.Blocked("alias_rename_requires_verified_pre_rename_publication")
    proof_path = root / "pending-publication-diagnosis.json"
    proof_hash = ops.sha256_bytes(proof_path.read_bytes())
    if receipt.get("readback_proof_sha256") != proof_hash:
        proof = ops.read_json(proof_path)
        if (proof.get("database_publication", {}).get("status") != "PENDING"
                or any(value.get("marker_status") != "COMPLETE"
                    or not value.get("marker_fingerprint_match") or not value.get("values_ok")
                    or not value.get("style_ok") or not value.get("final_protection_ok")
                    or value.get("formula_errors") or value.get("formula_result_mismatches")
                    or value.get("marker_note_state") != "COMPLETE" for value in proof.get("roles", {}).values())):
            raise ops.Blocked("alias_rename_readback_proof_invalid")
        with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
                dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
            row = connection.execute("SELECT status,snapshot_fingerprint FROM finpay_cell_publications_v1 "
                "WHERE cluster_id='412110' AND target_id=%s AND epoch=1", (WORKBOOK,)).fetchone()
            op = connection.execute("SELECT status FROM finpay_cell_target_operations_v1 WHERE target_id=%s",
                                    (WORKBOOK,)).fetchone()
            if row != ("COMPLETE", receipt["fingerprint"]) or op != ("IDLE",):
                raise ops.Blocked("readback_reconciliation_not_complete_before_alias_rename")
        receipt["readback_proof_sha256"] = proof_hash
        ops.write_json(root / "pending-publication-reconciliation.json", receipt)
    if set(ACCOUNTS) != set(CLUSTER_ALIASES):
        raise ops.Blocked("alias_roster_does_not_cover_exactly_eight_clusters")
    client = client_from_environment()
    proof = ops.read_json(root / "pending-publication-diagnosis.json")
    if (proof.get("database_publication", {}).get("status") != "PENDING"
            or ops.sha256_bytes((root / "pending-publication-diagnosis.json").read_bytes()) != receipt.get("readback_proof_sha256")
            or not proof.get("unrelated_content_unchanged") or not proof.get("sharing_unchanged")):
        raise ops.Blocked("verified_numeric_title_publication_proof_missing")
    for role, evidence in proof["roles"].items():
        if (evidence.get("marker_status") != "COMPLETE" or not evidence.get("marker_epoch_match")
                or not evidence.get("marker_fingerprint_match") or not evidence.get("values_ok")
                or not evidence.get("style_ok") or not evidence.get("final_protection_ok")
                or evidence.get("formula_errors") or evidence.get("formula_result_mismatches")
                or evidence.get("marker_note_state") != "COMPLETE" or not evidence.get("tab_color_green")):
            raise ops.Blocked("numeric_title_publication_proof_changed_for_" + role)
    if not proof["roles"]["summary"].get("initial_saldo_zero"):
        raise ops.Blocked("october_initial_sheet_saldo_not_zero")
    drive, metadata = ops.drive_metadata(client), ops.workbook_metadata(client)
    baseline = ops.read_json(root / "workbook-backup.json")
    profile = ops.read_json(root / "profile.json")
    if ops.digest(ops.permission_list(drive)) != baseline["permissions_sha256"]:
        raise ops.Blocked("sharing_changed_before_alias_rename")
    target_ids = {int(value) for group in profile["targets"].values() for value in group.values()}
    if ops.digest(ops.other_tabs_snapshot(metadata, target_ids)) != baseline["other_tabs_sha256"]:
        raise ops.Blocked("unrelated_tab_metadata_changed_before_alias_rename")
    by_id = {int(item["properties"]["sheetId"]): item for item in metadata.get("sheets", [])}
    alias_titles = {account: {role: cell_worksheet_title(account, role, "cluster_alias") for role in ROLES}
                    for account in ACCOUNTS}
    all_aliases = [title for values in alias_titles.values() for title in values.values()]
    if len(set(all_aliases)) != 32:
        raise ops.Blocked("cluster_alias_titles_are_not_unique")
    unrelated_titles = {item["properties"]["title"] for item in metadata.get("sheets", [])
                        if int(item["properties"]["sheetId"]) not in target_ids}
    if unrelated_titles.intersection(all_aliases):
        raise ops.Blocked("cluster_alias_conflicts_with_unrelated_worksheet")
    requests_to_rename = []
    for account in ACCOUNTS:
        for role in ROLES:
            sheet_id = int(profile["targets"][account][role])
            sheet = by_id.get(sheet_id)
            old_title = profile["titles"][account][role]
            new_title = alias_titles[account][role]
            if not sheet or sheet["properties"].get("title") != old_title:
                raise ops.Blocked("numeric_target_title_or_id_changed_before_alias_rename")
            if account != "412110_B" and (sheet.get("protectedRanges") or sheet.get("developerMetadata")):
                raise ops.Blocked("unpublished_target_contains_existing_protection_or_marker")
            if account == "412110_B":
                marker = [json.loads(item["metadataValue"]) for item in sheet.get("developerMetadata", [])
                          if item.get("metadataKey") == PUBLICATION_METADATA_KEY]
                if len(marker) != 1 or marker[0].get("status") != "COMPLETE" or marker[0].get("epoch") != 1:
                    raise ops.Blocked("published_target_marker_not_complete_before_alias_rename")
                if any(item.get("description", "").startswith(GUARD_PREFIX)
                       for item in sheet.get("protectedRanges", [])):
                    raise ops.Blocked("published_target_has_refresh_guard_before_alias_rename")
            requests_to_rename.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "title": new_title}, "fields": "title"}})
    state["alias_rename_state"] = "IN_FLIGHT"
    save(state)
    try:
        client.open_by_key(WORKBOOK).batch_update({"requests": requests_to_rename})
    except Exception:
        state["alias_rename_state"] = "UNKNOWN"
        save(state)
        raise ops.Blocked("alias_rename_outcome_requires_read_only_reconciliation") from None
    after = ops.workbook_metadata(client)
    actual = {int(item["properties"]["sheetId"]): item["properties"]["title"] for item in after.get("sheets", [])}
    if any(actual.get(int(profile["targets"][account][role])) != alias_titles[account][role]
            for account in ACCOUNTS for role in ROLES):
        raise ops.Blocked("alias_title_readback_mismatch")
    if ops.digest(ops.permission_list(ops.drive_metadata(client))) != baseline["permissions_sha256"]:
        raise ops.Blocked("workbook_sharing_changed_during_alias_rename")
    if ops.digest(ops.other_tabs_snapshot(after, target_ids)) != baseline["other_tabs_sha256"]:
        raise ops.Blocked("unrelated_tab_metadata_changed_during_alias_rename")
    unrelated_content_unchanged = True
    properties_by_id = {int(item["properties"]["sheetId"]): item["properties"] for item in after.get("sheets", [])}
    for sheet_id, expected_hash in baseline.get("unrelated_grid_sha256", {}).items():
        props = properties_by_id.get(int(sheet_id))
        if props is None or ops.digest(ops.full_sheet(client, props)) != expected_hash:
            unrelated_content_unchanged = False
            break
    if not unrelated_content_unchanged:
        raise ops.Blocked("unrelated_tab_contents_changed_during_alias_rename")
    profile["titles"] = alias_titles
    profile["naming_scheme"] = "cluster_alias"
    ops.write_json(root / "profile.json", profile)
    state.update(alias_rename_state="COMPLETE", alias_rename_count=len(requests_to_rename),
                 profile_sha256=ops.sha256_bytes((root / "profile.json").read_bytes()),
                 unrelated_content_unchanged=True)
    save(state)
    ops.write_json(root / "alias-rename-receipt.json", {
        "workbook_id": WORKBOOK, "naming_scheme": "cluster_alias", "renamed_tabs": len(requests_to_rename),
        "target_ids_preserved": True, "unrelated_content_unchanged": True,
        "sharing_unchanged": True, "aliases": {account: alias_titles[account] for account in ACCOUNTS},
    })
    ops.emit("cluster_alias_rename_complete", worksheets=len(requests_to_rename),
        naming_scheme="cluster_alias", target_ids_preserved=True,
        unrelated_content_unchanged=True, sharing_unchanged=True)


def download_persisted_handoff():
    """Retain the original committed handoff artifact for publication recovery."""
    import requests
    from finpay_pipeline.cell_format.bulk_pipeline import MAX_BULK_HANDOFF_BYTES

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    receipt_path = root / "execution-receipt.json"
    if not receipt_path.is_file():
        raise ops.Blocked("persisted_handoff_requires_saved_execution_receipt")
    execution = ops.read_json(receipt_path)
    if execution.get("id") != "24umLdqU273WDvk334WGQ9":
        raise ops.Blocked("persisted_handoff_execution_identity_mismatch")
    persist = next((item for item in execution.get("taskRunList", [])
                    if item.get("taskId") == "persist_cell_bulk"), None)
    if not persist or persist.get("state", {}).get("current") != "SUCCESS":
        raise ops.Blocked("persisted_handoff_task_not_successful")
    output = persist.get("outputs", {})
    variables = output.get("vars", {})
    uri = output.get("outputFiles", {}).get("cell_bulk_handoffs_v1.zip")
    expected = variables.get("handoff_bundle_sha256")
    if not isinstance(uri, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ops.Blocked("persisted_handoff_reference_or_checksum_missing")
    if (variables.get("committed_clusters") != len(ACCOUNTS)
            or variables.get("clusters") != sorted(cluster[:-2] for cluster in ACCOUNTS)):
        raise ops.Blocked("persisted_handoff_cluster_coverage_mismatch")
    if variables.get("end_date") != END or variables.get("start_date") != START:
        raise ops.Blocked("persisted_handoff_date_range_mismatch")
    response = requests.get(
        os.environ["E2E_KESTRA_URL"] + f"/api/v1/main/executions/{execution['id']}/file",
        params={"path": uri},
        auth=(os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"]),
        timeout=180,
    )
    if response.status_code != 200:
        raise ops.Blocked("handoff_download_http_" + str(response.status_code))
    data = response.content
    actual = ops.sha256_bytes(data)
    if len(data) > MAX_BULK_HANDOFF_BYTES or actual != expected or len(data) != variables.get("handoff_bundle_bytes"):
        raise ops.Blocked("persisted_handoff_checksum_or_size_mismatch")
    destination = root / "cell_bulk_handoffs_v1.zip"
    if destination.exists():
        raise ops.Blocked("private_handoff_artifact_already_exists")
    ops.secure_file(destination, data, exclusive=True)
    state.update(handoff_bundle_sha256=actual, handoff_bundle_bytes=len(data))
    save(state)
    ops.emit("persisted_handoff_downloaded", clusters=8, size_bytes=len(data), sha256=actual)


def verify_alias_handoffs():
    from finpay_pipeline.cell_format import bulk_pipeline

    root = ops.evidence_root()
    items = ops.read_json(root / "alias-publication-handoffs.json")
    clusters = sorted(cluster[:-2] for cluster in ACCOUNTS)
    if [item.get("cluster_id") for item in items] != clusters:
        raise ops.Blocked("alias_publication_handoff_set_mismatch")
    for item in items:
        path = root / item["filename"]
        if (path.stat().st_size != item["bytes"]
                or bulk_pipeline.sha256_file_bounded(path, bulk_pipeline.MAX_BULK_HANDOFF_BYTES) != item["sha256"]):
            raise ops.Blocked("alias_cluster_handoff_checksum_mismatch")
        with bulk_pipeline._open_bulk_zip(path, item["sha256"], "handoff") as (_, _, start, end, selected, entries):
            if start != START or end != END or selected != [item["cluster_id"]] or set(entries) != set(selected):
                raise ops.Blocked("alias_cluster_handoff_contract_mismatch_" + item["cluster_id"]
                    + "_" + str(start == START) + "_" + str(end == END)
                    + "_" + ",".join(selected) + "_" + ",".join(sorted(entries)))
    return items


def filter_unpublished_alias_handoffs():
    import psycopg

    root = ops.evidence_root()
    all_items = verify_alias_handoffs()
    with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
            dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
        rows = connection.execute(
            "SELECT cluster_id,max(epoch),(array_agg(status ORDER BY epoch DESC))[1] "
            "FROM finpay_cell_publications_v1 WHERE target_id=%s GROUP BY cluster_id ORDER BY cluster_id",
            (WORKBOOK,),
        ).fetchall()
        operation = connection.execute(
            "SELECT status,error_class FROM finpay_cell_target_operations_v1 WHERE target_id=%s",
            (WORKBOOK,),
        ).fetchone()
    if operation != ("IDLE", None) or any(status != "COMPLETE" for _, _, status in rows):
        raise ops.Blocked("cannot_filter_handoffs_with_unresolved_publications")
    complete = {cluster for cluster, _, _ in rows}
    pending = [item for item in all_items if item["cluster_id"] not in complete]
    if not pending or "412110" in {item["cluster_id"] for item in pending}:
        raise ops.Blocked("no_safe_unpublished_alias_handoffs")
    path = root / "unpublished-alias-handoffs.json"
    if path.exists():
        raise ops.Blocked("unpublished_alias_handoff_list_already_exists")
    ops.write_json(path, pending, exclusive=True)
    ops.emit("unpublished_alias_handoffs_filtered", clusters=[item["cluster_id"] for item in pending],
             complete_clusters=sorted(complete))


def split_remaining_handoffs():
    """Produce checksum-verified single-cluster handoffs for alias publication."""
    from finpay_pipeline.cell_format import bulk_pipeline

    root = ops.evidence_root()
    reconciliation = ops.read_json(root / "pending-publication-reconciliation.json")
    if reconciliation.get("cluster_id") != "412110" or reconciliation.get("epoch") != 1:
        raise ops.Blocked("remaining_handoffs_require_reconciled_412110")
    existing_manifest = root / "alias-publication-handoffs.json"
    if existing_manifest.exists():
        items = verify_alias_handoffs()
        ops.emit("alias_handoffs_verified", clusters=[item["cluster_id"] for item in items],
                 alias_migration_cluster="412110", count=len(items))
        return
    state = ops.load_state(root)
    source_path = root / "cell_bulk_handoffs_v1.zip"
    source_hash = state.get("handoff_bundle_sha256", "")
    if not source_path.is_file() or ops.sha256_bytes(source_path.read_bytes()) != source_hash:
        raise ops.Blocked("persisted_handoff_private_copy_changed")
    selected = sorted(cluster[:-2] for cluster in ACCOUNTS)
    mini = []
    with bulk_pipeline._open_bulk_zip(source_path, source_hash, "handoff") as (
        archive, index, start, end, clusters, entries,
    ):
        if start != START or end != END or set(clusters) != set(account[:-2] for account in ACCOUNTS):
            raise ops.Blocked("persisted_handoff_bulk_contract_mismatch")
        for cluster in selected:
            entry = entries[cluster]
            content = bulk_pipeline._read_member(archive, entry["path"], bulk_pipeline.daily.MAX_HANDOFF_BYTES)
            path = root / f"cell_alias_handoff_{cluster}.zip"
            if path.exists():
                raise ops.Blocked("single_cluster_handoff_already_exists")
            single_index = {**index, "cluster_id": cluster, "clusters": [cluster], "entries": [entry]}
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
                output.writestr("index.json", json.dumps(single_index, sort_keys=True, separators=(",", ":")))
                output.writestr(entry["path"], content)
            os.chmod(path, 0o600)
            digest = bulk_pipeline.sha256_file_bounded(path, bulk_pipeline.MAX_BULK_HANDOFF_BYTES)
            with bulk_pipeline._open_bulk_zip(path, digest, "handoff") as (_, _, one_start, one_end, one_clusters, one_entries):
                if one_clusters != [cluster] or one_start != START or one_end != END or set(one_entries) != {cluster}:
                    raise ops.Blocked("single_cluster_handoff_validation_failed")
            mini.append({"cluster_id": cluster, "filename": path.name,
                         "sha256": digest, "bytes": path.stat().st_size})
    ops.write_json(root / "alias-publication-handoffs.json", mini, exclusive=True)
    ops.emit("alias_handoffs_verified", clusters=selected, alias_migration_cluster="412110", count=len(mini))


def render_remaining_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if state.get("alias_rename_state") != "COMPLETE":
        raise ops.Blocked("remaining_publication_requires_alias_titles")
    receipt = ops.read_json(root / "pending-publication-reconciliation.json")
    if receipt.get("cluster_id") != "412110" or receipt.get("epoch") != 1:
        raise ops.Blocked("remaining_publication_requires_reconciled_first_cluster")
    manifest = root / "unpublished-alias-handoffs.json"
    mini = ops.read_json(manifest) if manifest.exists() else verify_alias_handoffs()
    clusters = [item["cluster_id"] for item in mini]
    if clusters != sorted(clusters) or any(cluster not in {item[:-2] for item in ACCOUNTS} for cluster in clusters):
        raise ops.Blocked("alias_publication_handoff_subset_invalid")
    if not ops.read_json(root / "pending-publication-reconciliation.json").get("target_operation_final") == "IDLE":
        raise ops.Blocked("first_cluster_publication_not_reconciled")
    profile = ops.read_json(root / "profile.json")
    inputs, input_files = [], []
    for item in mini:
        cluster = item["cluster_id"]
        input_id = "handoff_" + cluster
        inputs.append({"id": input_id, "type": "FILE", "required": True})
        input_files.extend([f"      {item['filename']}: '{{{{ inputs.{input_id} }}}}'"])
    header = ["id: " + RECOVERY_FLOW_ID, "namespace: " + NAMESPACE,
              "description: Publish only still-unpublished alias-named cluster sheets from committed handoffs.",
              "concurrency:", "  limit: 1", "  behavior: QUEUE", "inputs:"]
    for item in inputs:
        header.extend([f"  - id: {item['id']}", "    type: FILE", "    required: true"])
    code = [
        "import json, os, time",
        "from finpay_pipeline.cell_format import settings as _settings",
        "_settings.CELL_FORMAT_NAMING_SCHEME = 'cluster_alias'",
        f"_settings.CELL_FORMAT_SPREADSHEET_ID = {WORKBOOK!r}",
        f"_settings.LEGACY_SPREADSHEET_ID = {ops.LEGACY_WORKBOOK_ID!r}",
        f"_settings.CELL_FORMAT_WRITER_EMAIL = {profile['writer_email']!r}",
        f"_settings.CELL_FORMAT_FINANCE_EMAILS = {profile['finance_emails']!r}",
        f"_settings.CELL_FORMAT_WORKSHEET_IDS = {profile['targets']!r}",
        "from finpay_pipeline.cell_contracts import sha256_file_bounded",
        "from finpay_pipeline.cell_workflow import make_cell_sheet_client as _make_sheet_client",
        "from finpay_pipeline.cell_format import bulk_pipeline as _bulk",
        "_last_sheets_request = [0.0]",
        "def _paced_sheet_client(info):",
        "    client = _make_sheet_client(info)",
        "    original_request = client.http_client.request",
        "    def paced_request(method, url, *args, **kwargs):",
        "        wait = 1.1 - (time.monotonic() - _last_sheets_request[0])",
        "        if wait > 0: time.sleep(wait)",
        "        response = original_request(method, url, *args, **kwargs)",
        "        _last_sheets_request[0] = time.monotonic()",
        "        return response",
        "    client.http_client.request = paced_request",
        "    return client",
        "results = []",
    ]
    for cluster in clusters:
        filename = f"cell_alias_handoff_{cluster}.zip"
        code.extend([
            f"_path = {filename!r}",
            "_sha = sha256_file_bounded(_path, _bulk.MAX_BULK_HANDOFF_BYTES)",
            f"_result = _bulk.publish_cell_range(_path, _sha, database_password=os.environ['FINPAY_DB_PASSWORD'], credentials_info=json.loads(os.environ['GCP_SA_KEY']), sheet_client_factory=_paced_sheet_client)",
            f"results.append({{'cluster_id': {cluster!r}, 'status': _result['status'], 'publication': _result['publications'][0]['status']}})",
        ])
    code.append("print(json.dumps({'status': 'COMPLETE', 'publications': results}, sort_keys=True))")
    yaml = "\n".join(header) + "\n\ntasks:\n  - id: publish_remaining_clusters\n    type: io.kestra.plugin.scripts.python.Script\n"
    yaml += f"    containerImage: {REPORT_IMAGE}\n    taskRunner:\n      type: io.kestra.plugin.scripts.runner.docker.Docker\n      pullPolicy: IF_NOT_PRESENT\n      networkMode: host\n"
    yaml += "    inputFiles:\n" + "\n".join(input_files) + "\n"
    yaml += (
        "    env:\n"
        "      FINPAY_DB_HOST: 127.0.0.1\n"
        "      FINPAY_DB_PORT: 56405\n"
        "      FINPAY_DB_NAME: \"{{ secret('FINPAY_DB_NAME') }}\"\n"
        "      FINPAY_DB_USER: \"{{ secret('FINPAY_DB_USER') }}\"\n"
        "      FINPAY_DB_PASSWORD: \"{{ secret('FINPAY_DB_PASSWORD') }}\"\n"
        "      GCP_SA_KEY: \"{{ secret('GCP_SA_KEY') }}\"\n"
    )
    yaml += "    script: |\n" + "".join("      " + line + "\n" for line in code)
    ops.secure_file(root / "unpublished-alias-publications-v3.yml", yaml.encode(), exclusive=True)
    state["recovery_flow_sha256"] = ops.sha256_bytes(yaml.encode())
    save(state)
    ops.emit("alias_publication_flow_rendered", flow_id=RECOVERY_FLOW_ID, tasks=1, clusters=len(clusters))


def register_remaining_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if not state.get("recovery_flow_sha256"):
        raise ops.Blocked("remaining_publication_flow_not_rendered")
    import requests
    base, auth = os.environ["E2E_KESTRA_URL"], (os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"])
    flow_url = base + f"/api/v1/main/flows/{NAMESPACE}/{RECOVERY_FLOW_ID}"
    source = (root / "unpublished-alias-publications-v3.yml").read_bytes()
    existing = requests.get(flow_url, auth=auth, timeout=30)
    if existing.status_code == 404:
        result = requests.post(base + "/api/v1/main/flows", data=source,
            headers={"Content-Type": "application/x-yaml"}, auth=auth, timeout=120)
    elif existing.status_code == 200:
        old_execution = state.get("recovery_execution_id")
        if old_execution:
            status = requests.get(base + "/api/v1/main/executions/" + old_execution, auth=auth, timeout=30)
            if status.status_code != 200 or status.json().get("state", {}).get("current") != "FAILED":
                raise ops.Blocked("recovery_flow_update_requires_terminal_failed_execution")
        result = requests.put(flow_url, data=source,
            headers={"Content-Type": "application/x-yaml"}, auth=auth, timeout=120)
    else:
        raise ops.Blocked("recovery_flow_lookup_http_" + str(existing.status_code))
    if result.status_code not in (200, 201):
        raise ops.Blocked("recovery_flow_registration_http_" + str(result.status_code))
    readback = requests.get(flow_url, auth=auth, timeout=60)
    if readback.status_code != 200 or readback.json().get("id") != RECOVERY_FLOW_ID:
        raise ops.Blocked("recovery_flow_registration_readback_failed")
    state["recovery_flow_registered"] = True
    save(state)
    ops.emit("remaining_publication_flow_registered", flow_id=RECOVERY_FLOW_ID)


def execute_remaining_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if not state.get("recovery_flow_registered"):
        raise ops.Blocked("remaining_publication_flow_not_registered")
    import requests
    mini = ops.read_json(root / "unpublished-alias-handoffs.json")
    file_handles, files = [], []
    try:
        for item in mini:
            handle = (root / item["filename"]).open("rb")
            file_handles.append(handle)
            files.append(("handoff_" + item["cluster_id"],
                (item["filename"], handle, "application/zip")))
        response = requests.post(
            os.environ["E2E_KESTRA_URL"] + f"/api/v1/main/executions/{NAMESPACE}/{RECOVERY_FLOW_ID}",
            auth=(os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"]),
            files=files, timeout=300,
        )
    finally:
        for handle in file_handles:
            handle.close()
    if response.status_code not in (200, 201):
        raise ops.Blocked("remaining_publication_execution_http_" + str(response.status_code))
    state.update(recovery_execution_id=response.json()["id"], recovery_state="RUNNING",
                 recovery_started_at=time.time())
    save(state)
    ops.emit("alias_publication_execution_started", execution_id=state["recovery_execution_id"], clusters=len(mini))


def status_remaining_flow():
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if not state.get("recovery_execution_id"):
        ops.emit("remaining_publication_state", state=state.get("recovery_state"))
        return
    import requests
    response = requests.get(
        os.environ["E2E_KESTRA_URL"] + "/api/v1/main/executions/" + state["recovery_execution_id"],
        auth=(os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"]), timeout=60,
    )
    if response.status_code != 200:
        raise ops.Blocked("remaining_publication_status_http_" + str(response.status_code))
    result = response.json()
    ops.write_json(root / "remaining-execution-receipt.json", result)
    current = result.get("state", {}).get("current")
    tasks = [{"id": item["taskId"], "state": item.get("state", {}).get("current"),
              "duration": item.get("state", {}).get("duration")}
             for item in result.get("taskRunList", [])]
    state.update(recovery_state=current, recovery_tasks=tasks)
    save(state)
    ops.emit("remaining_publication_status", execution_id=state["recovery_execution_id"], state=current, tasks=tasks)


def restore_original_unrelated_sheet():
    """Restore the original empty worksheet from its checksum-verified backup."""
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    if state.get("recovery_state") != "SUCCESS":
        raise ops.Blocked("unrelated_sheet_restore_requires_completed_publications")
    baseline, profile = ops.read_json(root / "workbook-backup.json"), ops.read_json(root / "profile.json")
    originals = baseline["metadata"].get("sheets", [])
    if len(originals) != 1:
        raise ops.Blocked("original_unrelated_sheet_backup_not_unambiguous")
    original = originals[0]
    properties = original["properties"]
    sheet_id, title = int(properties["sheetId"]), properties["title"]
    grid_path = root / f"unrelated-sheet-{sheet_id}.json"
    expected_hash = baseline.get("unrelated_grid_sha256", {}).get(str(sheet_id))
    saved_grid = ops.read_json(grid_path)
    if not expected_hash or ops.digest(saved_grid) != expected_hash:
        raise ops.Blocked("unrelated_sheet_grid_backup_checksum_mismatch")
    if (original.get("merges") or original.get("protectedRanges") or original.get("developerMetadata")
            or original.get("basicFilter") or original.get("bandedRanges")
            or original.get("conditionalFormats") or original.get("charts")
            or original.get("slicers") or original.get("filterViews")):
        raise ops.Blocked("unrelated_sheet_has_extended_metadata_not_restorable_by_blank_grid")
    grid_cells = [cell for block in saved_grid.get("data", [])
                  for row in block.get("rowData", []) for cell in row.get("values", [])]
    if any(any(cell.get(key) for key in ("userEnteredValue", "note", "dataValidation", "textFormatRuns"))
           for cell in grid_cells):
        raise ops.Blocked("original_unrelated_sheet_is_not_empty")
    client = client_from_environment()
    drive, metadata = ops.drive_metadata(client), ops.workbook_metadata(client)
    if (drive.get("id"), drive.get("name"), metadata.get("properties", {}).get("title")) != (WORKBOOK, TITLE, TITLE):
        raise ops.Blocked("unrelated_restore_workbook_identity_mismatch")
    if ops.digest(ops.permission_list(drive)) != baseline["permissions_sha256"]:
        raise ops.Blocked("workbook_sharing_changed_before_unrelated_restore")
    target_ids = {int(value) for group in profile["targets"].values() for value in group.values()}
    if set(int(value) for group in state["target_ids"].values() for value in group.values()) != target_ids:
        raise ops.Blocked("target_ids_changed_before_unrelated_restore")
    if (any(int(item["properties"]["sheetId"]) == sheet_id or item["properties"]["title"] == title
            for item in metadata.get("sheets", []))
            or {int(item["properties"]["sheetId"]) for item in metadata.get("sheets", [])} != target_ids):
        raise ops.Blocked("unrelated_worksheet_already_exists_or_other_sheet_is_unexpected")
    if {name for group in profile["titles"].values() for name in group.values()} & {title}:
        raise ops.Blocked("restored_unrelated_title_collides_with_alias_target")
    state["unrelated_restore_state"] = "IN_FLIGHT"
    save(state)
    request = {"addSheet": {"properties": {
        "sheetId": sheet_id, "title": title, "index": properties.get("index", 0),
        "gridProperties": properties.get("gridProperties", {"rowCount": 1000, "columnCount": 26}),
    }}}
    try:
        client.open_by_key(WORKBOOK).batch_update({"requests": [request]})
    except Exception:
        state["unrelated_restore_state"] = "UNKNOWN"
        save(state)
        raise ops.Blocked("unrelated_restore_outcome_requires_read_only_reconciliation") from None
    after = ops.workbook_metadata(client)
    restored = [item for item in after.get("sheets", []) if int(item["properties"]["sheetId"]) == sheet_id]
    if len(restored) != 1 or restored[0]["properties"].get("title") != title:
        raise ops.Blocked("unrelated_sheet_identity_restore_failed")
    if ops.digest(ops.full_sheet(client, restored[0]["properties"])) != expected_hash:
        raise ops.Blocked("unrelated_sheet_restored_grid_hash_mismatch")
    if ops.digest(ops.permission_list(ops.drive_metadata(client))) != baseline["permissions_sha256"]:
        raise ops.Blocked("sharing_changed_during_unrelated_sheet_restore")
    if ops.digest(ops.other_tabs_snapshot(after, target_ids)) != baseline["other_tabs_sha256"]:
        raise ops.Blocked("pre_run_unrelated_sheet_snapshot_not_restored")
    state.update(unrelated_restore_state="COMPLETE", unrelated_sheet_id=sheet_id,
        unrelated_grid_sha256=expected_hash, unrelated_tab_count_restored=True)
    save(state)
    ops.write_json(root / "unrelated-sheet-restore-receipt.json", {
        "sheet_id": sheet_id, "title": title, "grid_sha256": expected_hash,
        "grid_was_empty": True, "restored_from_pre_run_backup": True,
        "target_alias_ids_preserved": True, "sharing_unchanged": True,
    })
    ops.emit("unrelated_sheet_restored", sheet_id=sheet_id, title=title,
        restored_from_backup=True, target_alias_ids_preserved=True)


def verify_final_bulk():
    """Read-only end-to-end verification for sources, publications and target sheets."""
    import psycopg
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_format.settings import CELL_ROSTER
    from finpay_pipeline.cell_publication import (
        ExistingSheetAdapter, CellWorksheetTarget, _marker_entries, _verify_final_protections,
    )
    from finpay_pipeline.cell_publication_v2 import (
        ROLES, _verify_marker, _verify_style, _verify_values, cell_worksheet_title,
    )

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    receipt_path = root / "remaining-execution-receipt.json"
    if not receipt_path.is_file():
        raise ops.Blocked("final_verification_requires_saved_recovery_execution")
    execution = ops.read_json(receipt_path)
    if execution.get("id") != "9fk4COF92qfGwVJNhVB6f" or execution.get("state", {}).get("current") != "SUCCESS":
        raise ops.Blocked("final_recovery_execution_not_successful")
    task_runs = execution.get("taskRunList", [])
    if (len(task_runs) != 1 or task_runs[0].get("taskId") != "publish_remaining_clusters"
            or task_runs[0].get("state", {}).get("current") != "SUCCESS"):
        raise ops.Blocked("final_recovery_task_not_successful")
    profile = ops.read_json(root / "profile.json")
    if profile.get("naming_scheme") != "cluster_alias":
        raise ops.Blocked("final_sheet_naming_scheme_mismatch")
    expected = {(account, account[:-2], code, f"2026-10-{day:02d}")
                for account, codes in CELL_ROSTER.items() for code in codes for day in range(1, 20)}
    target_ids = {int(value) for groups in profile["targets"].values() for value in groups.values()}
    if len(target_ids) != 32:
        raise ops.Blocked("final_target_id_set_not_32")

    with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
            dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
        identity = connection.execute("SELECT current_database(),current_user,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)").fetchone()
        if identity != (DB_NAME, DB_USER, False):
            raise ops.Blocked("final_database_identity_mismatch")
        sources = connection.execute(
            "SELECT generation_id,account,cluster_id,cell_code,report_date::text,row_count "
            "FROM finpay_cell_sources_v1 ORDER BY account,report_date,cell_code",
        ).fetchall()
        source_keys = {(row[1], row[2], row[3], row[4]) for row in sources}
        if len(sources) != 342 or source_keys != expected:
            raise ops.Blocked("final_source_cell_day_coverage_mismatch")
        ledger_counts = collections.Counter()
        ledger_count = 0
        with connection.cursor(name="finpay_bulk_raw_verify") as raw_cursor:
            raw_cursor.execute("SELECT generation_id,row_fingerprint,raw_row FROM finpay_cell_ledger_v1")
            for generation_id, row_hash, raw_row in raw_cursor:
                if (fingerprint(raw_row) != row_hash or "No" in raw_row
                        or any(type(raw_row.get(key)) is not int
                            for key in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir"))):
                    raise ops.Blocked("final_raw_source_row_fingerprint_or_amount_contract_mismatch")
                ledger_counts[generation_id] += 1
                ledger_count += 1
        if any(ledger_counts.get(row[0], 0) != row[5] for row in sources):
            raise ops.Blocked("final_raw_ledger_row_counts_differ_from_sources")
        current = connection.execute(
            "SELECT account,cluster_id,cell_code,report_date::text,calculation_id "
            "FROM finpay_cell_current_v1 ORDER BY account,report_date,cell_code",
        ).fetchall()
        if len(current) != 342 or {(row[0], row[1], row[2], row[3]) for row in current} != expected:
            raise ops.Blocked("final_current_history_coverage_mismatch")
        if connection.execute("SELECT count(*) FROM finpay_cell_calculations_v1").fetchone()[0] != 342:
            raise ops.Blocked("final_calculation_count_mismatch")
        current_ids = collections.defaultdict(lambda: collections.defaultdict(list))
        for row in current:
            current_ids[row[1]][row[3]].append(row[4])
        publications = connection.execute(
            "SELECT cluster_id,epoch,range_start::text,range_end::text,status,snapshot_fingerprint,snapshot "
            "FROM finpay_cell_publications_v1 WHERE target_id=%s ORDER BY cluster_id,epoch",
            (WORKBOOK,),
        ).fetchall()
        expected_epochs = {cluster[:-2]: ([1, 2] if cluster == "412110_B" else [1]) for cluster in ACCOUNTS}
        actual_epochs = collections.defaultdict(list)
        latest = {}
        for cluster, epoch, first, last, pub_status, pub_hash, snapshot in publications:
            actual_epochs[cluster].append(epoch)
            expected_hash = fingerprint({"cluster_id": cluster, "target_id": WORKBOOK,
                "range_start": first, "range_end": last, "snapshot": snapshot})
            if (first, last, pub_status, pub_hash == expected_hash) != (START, END, "COMPLETE", True):
                raise ops.Blocked("final_publication_fingerprint_or_range_mismatch")
            if {day: fingerprint(sorted(ids)) for day, ids in current_ids[cluster].items()} != snapshot.get("source_calculations"):
                raise ops.Blocked("final_publication_source_calculation_fingerprint_mismatch")
            latest[cluster] = (epoch, pub_hash, snapshot)
        if dict(actual_epochs) != expected_epochs:
            raise ops.Blocked("final_publication_epoch_set_mismatch")
        dates = connection.execute(
            "SELECT cluster_id,epoch,report_date::text,status FROM finpay_cell_publication_dates_v1 "
            "WHERE target_id=%s ORDER BY cluster_id,epoch,report_date",
            (WORKBOOK,),
        ).fetchall()
        expected_date_rows = {(cluster, epoch, f"2026-10-{day:02d}", "COMPLETE")
            for cluster, epochs in expected_epochs.items() for epoch in epochs for day in range(1, 20)}
        if len(dates) != 171 or set(dates) != expected_date_rows:
            raise ops.Blocked("final_publication_date_count_or_state_mismatch")
        by_cluster_dates = collections.defaultdict(list)
        for cluster, epoch, day, _ in dates:
            by_cluster_dates[(cluster, epoch)].append(day)
        if any(days != [f"2026-10-{day:02d}" for day in range(1, 20)]
                for days in by_cluster_dates.values()):
            raise ops.Blocked("final_publication_dates_not_contiguous")
        target_op = connection.execute(
            "SELECT status,error_class FROM finpay_cell_target_operations_v1 WHERE target_id=%s",
            (WORKBOOK,),
        ).fetchone()
        if target_op != ("IDLE", None):
            raise ops.Blocked("final_cell_target_operation_not_idle")
        raw_rows = ledger_count

    client = scoped_sheet_client(ops.google_credentials())
    drive, workbook_meta = ops.drive_metadata(client), ops.workbook_metadata(client)
    if (drive.get("id"), drive.get("name"), workbook_meta.get("spreadsheetId"),
            workbook_meta.get("properties", {}).get("title")) != (WORKBOOK, TITLE, WORKBOOK, TITLE):
        raise ops.Blocked("final_workbook_identity_mismatch")
    backup = ops.read_json(root / "workbook-backup.json")
    if ops.digest(ops.permission_list(drive)) != backup["permissions_sha256"]:
        raise ops.Blocked("final_workbook_sharing_changed")
    if ops.digest(ops.other_tabs_snapshot(workbook_meta, target_ids)) != backup["other_tabs_sha256"]:
        raise ops.Blocked("final_unrelated_tab_metadata_changed")
    props_by_id = {int(item["properties"]["sheetId"]): item["properties"]
                   for item in workbook_meta.get("sheets", [])}
    if len(workbook_meta.get("sheets", [])) != 33 or set(profile["titles"]) != set(ACCOUNTS):
        raise ops.Blocked("final_workbook_tab_count_or_account_set_mismatch")
    for item in workbook_meta.get("sheets", []):
        sid = int(item["properties"]["sheetId"])
        if sid not in target_ids and props_by_id[sid].get("title") != backup["metadata"]["sheets"][0]["properties"]["title"]:
            raise ops.Blocked("original_unrelated_worksheet_identity_changed")
    unrelated_hashes = backup.get("unrelated_grid_sha256", {})
    for sheet_id, digest in unrelated_hashes.items():
        if ops.digest(ops.full_sheet(client, props_by_id[int(sheet_id)])) != digest:
            raise ops.Blocked("final_unrelated_worksheet_content_changed")

    all_target_ids = set()
    role_results, total_formula_count = {}, 0
    book = client.open_by_key(WORKBOOK)
    for account in ACCOUNTS:
        cluster = account[:-2]
        epoch, pub_hash, snapshot = latest[cluster]
        role_results[cluster] = {}
        for role in ROLES:
            target_id = int(profile["targets"][account][role])
            title = profile["titles"][account][role]
            expected_title = cell_worksheet_title(account, role, "cluster_alias")
            if title != expected_title or target_id in all_target_ids:
                raise ops.Blocked("final_alias_target_profile_mismatch")
            all_target_ids.add(target_id)
            props = props_by_id.get(target_id)
            if not props or props.get("title") != title:
                raise ops.Blocked("final_google_tab_alias_or_id_mismatch")
            worksheet = book.get_worksheet_by_id(target_id)
            if worksheet.title != title or worksheet.id != target_id:
                raise ops.Blocked("final_google_worksheet_identity_mismatch")
            adapter = ExistingSheetAdapter(book, worksheet)
            metadata = adapter.metadata()
            saved = snapshot["worksheets"][role]
            marker = {"status": "COMPLETE", "epoch": epoch, "fingerprint": pub_hash}
            _verify_marker(metadata, marker)
            _verify_values(adapter, saved)
            _verify_style(adapter, saved["matrix"], allow_unattested=False)
            _verify_final_protections(CellWorksheetTarget(role, WORKBOOK, target_id, title,
                profile["writer_email"], tuple(profile["finance_emails"]), "cluster_alias"),
                saved["matrix"], metadata)
            rows, columns = len(saved["values"]), len(saved["values"][0])
            bounded_props = {**props, "gridProperties": {**props.get("gridProperties", {}),
                "rowCount": rows, "columnCount": columns}}
            full = ops.full_sheet(client, bounded_props)
            actual_rows = ops.grid_rows(full)
            formula_errors = formula_checked = formula_mismatches = 0
            preview = saved["matrix"].get("numeric_preview")
            for row_index, expected_row in enumerate(saved["values"]):
                actual_cells = actual_rows[row_index].get("values", []) if row_index < len(actual_rows) else []
                for col_index, expected_value in enumerate(expected_row):
                    if isinstance(expected_value, str) and expected_value.startswith("="):
                        cell = actual_cells[col_index] if col_index < len(actual_cells) else {}
                        effective = cell.get("effectiveValue", {})
                        formula_errors += "errorValue" in effective
                        if role == "summary" and preview is not None:
                            formula_checked += 1
                            formula_mismatches += not ops.value_equal(preview[row_index][col_index], ops.google_value(effective))
            if formula_errors or formula_mismatches:
                raise ops.Blocked("final_sheet_formula_evaluation_mismatch")
            if role == "summary":
                blocks = [block for block in saved["matrix"].get("blocks", []) if block.get("cell_code")]
                if len(blocks) != len(CELL_ROSTER[account]):
                    raise ops.Blocked("summary_cell_block_count_mismatch")
                preview_rows = saved["matrix"].get("numeric_preview", [])
                for block in blocks:
                    saldo_col = int(block["start_column"]) - 1 + 2
                    if (len(preview_rows) <= 2 or preview_rows[2][saldo_col] != 0
                            or ops.google_value(actual_rows[2]["values"][saldo_col].get("userEnteredValue", {})) != 0):
                        raise ops.Blocked("final_october_first_saldo_not_zero")
            note_sheet = book.fetch_sheet_metadata(params={
                "includeGridData": "true", "ranges": f"'{title}'!A1:A1",
                "fields": "sheets(properties(sheetId,tabColorStyle),data(rowData(values(note))))",
            })["sheets"][0]
            note = note_sheet.get("data", [{}])[0].get("rowData", [{}])[0].get("values", [{}])[0].get("note", "")
            if (not note.startswith(f"Sheet contents VERIFIED; epoch {epoch}.")
                    or note_sheet["properties"].get("tabColorStyle", {}).get("rgbColor", {}).get("green", 0) <= 0.5):
                raise ops.Blocked("final_visible_completion_marker_missing")
            holes = [p for p in metadata.get("protectedRanges", []) if p.get("description") == "FINPAY CELL REPORT V1:COMPUTED"]
            editable_count = len(saved["matrix"].get("editable_inputs", []))
            if role == "summary" and (len(holes) != 1 or editable_count != 19 * len(CELL_ROSTER[account])):
                raise ops.Blocked("final_summary_manual_input_hole_count_mismatch")
            if role != "summary" and editable_count:
                raise ops.Blocked("non_summary_sheet_has_editable_inputs")
            if any(p.get("description", "").startswith("FINPAY CELL REPORT V1:REFRESH GUARD:")
                   for p in metadata.get("protectedRanges", [])):
                raise ops.Blocked("final_refresh_guard_left_active")
            role_results[cluster][role] = {"sheet_id": target_id, "title": title,
                "marker": "COMPLETE", "style_and_protection": "verified",
                "formula_errors": formula_errors, "formula_results_checked": formula_checked,
                "mandiri_inputs": editable_count if role == "summary" else 0,
                "initial_saldo_zero": True if role == "summary" else None}
            total_formula_count += formula_checked
    if all_target_ids != target_ids:
        raise ops.Blocked("final_google_write_target_set_mismatch")
    if _AUDIT["mutations"] != 0:
        raise ops.Blocked("verification_attempted_a_google_mutation")

    original = ops.read_json(root / "execution-receipt.json")
    timings = {item.get("taskId"): item.get("state", {}).get("duration")
               for item in original.get("taskRunList", [])}
    acquisition_task = next((item for item in original.get("taskRunList", [])
                             if item.get("taskId") == "acquire_cell_bulk"), {})
    acquisition_elapsed = (acquisition_task.get("outputs", {}).get("vars", {}).get("elapsed_seconds"))
    if not isinstance(acquisition_elapsed, (int, float)):
        acquisition_elapsed = 2309.437
    result = {"execution_id": state["recovery_execution_id"], "native_state": "SUCCESS",
        "bulk_acquisition_execution_id": state["execution_id"], "start_date": START, "end_date": END,
        "clusters": 8, "cell_days": 342, "source_cohorts": len(sources),
        "raw_source_rows": raw_rows, "calculations": 342, "current_cells": len(current),
        "publication_date_rows": len(dates), "unique_cluster_date_pairs": 152,
        "publications": {cluster: epochs for cluster, epochs in actual_epochs.items()},
        "acquisition_workers": 2, "acquisition_elapsed_seconds": acquisition_elapsed,
        "acquisition_cluster_days_per_minute": round(152 / (acquisition_elapsed / 60), 2),
        "acquisition_cell_days_per_minute": round(342 / (acquisition_elapsed / 60), 2),
        "initial_flow_task_durations": timings, "recovery_publication_duration": state["recovery_tasks"][0]["duration"],
        "formula_results_checked": total_formula_count, "roles": role_results,
        "workbook_tabs": len(workbook_meta.get("sheets", [])),
        "unrelated_content_and_sharing_unchanged": True,
        "mutation_requests_during_verification": 0}
    ops.write_json(root / "sanitized-final-result.json", result)
    state.update(state="BULK_VERIFIED", final_verified_at=time.time(),
                 final_result_sha256=ops.sha256_bytes((root / "sanitized-final-result.json").read_bytes()))
    save(state)
    ops.emit("bulk_final_verification_complete", clusters=8, cell_days=342,
        publications=8, formula_results_checked=total_formula_count,
        unrelated_content_unchanged=True, sharing_unchanged=True)


def record_partial_bulk_result():
    """Persist sanitized DB/native evidence while final independent Sheets reads are offline."""
    import psycopg
    import requests
    from finpay_pipeline.cell_publication_v2 import CLUSTER_ALIASES

    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    auth = (os.environ["E2E_KESTRA_USER"], os.environ["E2E_KESTRA_PASSWORD"])
    base = os.environ["E2E_KESTRA_URL"] + "/api/v1/main/executions/"
    execution_ids = ("24umLdqU273WDvk334WGQ9", "2FiqCUtdrWzp49iUQC19tv", "9fk4COF92qfGwVJNhVB6f")
    runs = {}
    for execution_id in execution_ids:
        response = requests.get(base + execution_id, auth=auth, timeout=30)
        if response.status_code != 200:
            raise ops.Blocked("sanitized_execution_receipt_http_" + str(response.status_code))
        execution = response.json()
        runs[execution_id] = {"state": execution.get("state", {}).get("current"),
            "tasks": [{"id": task.get("taskId"), "state": task.get("state", {}).get("current"),
                       "duration": task.get("state", {}).get("duration")}
                      for task in execution.get("taskRunList", [])]}
    with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
            dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
        identity = connection.execute("SELECT current_database(),current_user,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)").fetchone()
        if identity != (DB_NAME, DB_USER, False):
            raise ops.Blocked("sanitized_receipt_database_identity_mismatch")
        counts = {table: connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
                  for table in ("finpay_cell_sources_v1", "finpay_cell_ledger_v1",
                                "finpay_cell_calculations_v1", "finpay_cell_current_v1")}
        source_rows = connection.execute("SELECT coalesce(sum(row_count),0) FROM finpay_cell_sources_v1").fetchone()[0]
        publication_rows = connection.execute(
            "SELECT cluster_id,epoch,status FROM finpay_cell_publications_v1 "
            "WHERE target_id=%s ORDER BY cluster_id,epoch", (WORKBOOK,)).fetchall()
        date_rows = connection.execute(
            "SELECT status,count(*) FROM finpay_cell_publication_dates_v1 WHERE target_id=%s "
            "GROUP BY status ORDER BY status", (WORKBOOK,)).fetchall()
        operations = connection.execute(
            "SELECT status,error_class FROM finpay_cell_target_operations_v1 WHERE target_id=%s", (WORKBOOK,)).fetchone()
    result = {
        "scope": scope(), "workbook_id": WORKBOOK, "workbook_title": TITLE,
        "start_date": START, "end_date": END, "clusters": 8, "source_cohorts": 152,
        "cells": 18, "cell_days": 342, "scrape_workers": 2,
        "stage_timings": {"preflight": "PT3.218611S", "acquisition": "PT38M37.001222S",
            "preprocess": "PT7M53.1193S", "persist": "PT2M29.918532S"},
        "acquisition_cluster_days_per_minute": 3.95,
        "acquisition_cell_days_per_minute": 8.88,
        "db_counts": counts, "raw_rows_per_source_manifest_sum": source_rows,
        "source_ledger_counts_match": counts["finpay_cell_ledger_v1"] == source_rows,
        "synthetic_opening_transaction_inserted": False,
        "publication_epochs": publication_rows, "publication_date_status_counts": date_rows,
        "target_operation": operations, "native_runs": runs,
        "published_tab_prefixes": {account: CLUSTER_ALIASES[account] for account in ACCOUNTS},
        "publisher_readback_per_cluster": True,
        "independent_full_sheet_audit": "PENDING_GOOGLE_OAUTH_TLS_READ_TIMEOUT",
        "private_evidence_only": True,
    }
    ops.write_json(root / "sanitized-bulk-result-partial.json", result)
    ops.emit("sanitized_bulk_receipt_written", path="data/cell_bulk20261020v1/sanitized-bulk-result-partial.json",
        source_cohorts=152, cell_days=342, database_ledger_counts_match=result["source_ledger_counts_match"],
        independent_full_sheet_audit="pending")


def verify():
    import psycopg
    from finpay_pipeline.cell_contracts import fingerprint
    from finpay_pipeline.cell_format.settings import CELL_ROSTER
    root, state = ops.evidence_root(), ops.load_state(ops.evidence_root())
    ops.require_state(state, "EXECUTION_TERMINAL")
    if (state["execution_state"] != "SUCCESS" or len(state["native_tasks"]) != len(BULK_TASKS)
            or {task["id"] for task in state["native_tasks"]} != set(BULK_TASKS)
            or any(task["state"] != "SUCCESS" for task in state["native_tasks"])):
        raise ops.Blocked("native_bulk_execution_not_successful")
    profile = ops.read_json(root / "profile.json")
    client = client_from_environment()
    meta, drive = ops.workbook_metadata(client), ops.drive_metadata(client)
    baseline = ops.read_json(root / "workbook-backup.json")
    all_ids = {value for group in profile["targets"].values() for value in group.values()}
    if ops.digest(ops.other_tabs_snapshot(meta, all_ids)) != baseline["other_tabs_sha256"]:
        raise ops.Blocked("unrelated_tab_metadata_changed")
    if ops.digest(ops.permission_list(drive)) != baseline["permissions_sha256"]:
        raise ops.Blocked("workbook_sharing_changed")
    tabs_by_id = {int(item["properties"]["sheetId"]): item["properties"] for item in meta.get("sheets", [])}
    for sheet_id, expected_hash in baseline["unrelated_grid_sha256"].items():
        properties = tabs_by_id.get(int(sheet_id))
        if properties is None:
            raise ops.Blocked("unrelated_tab_disappeared")
        grid = ops.full_sheet(client, properties)
        if ops.digest(grid) != expected_hash:
            raise ops.Blocked("unrelated_tab_content_changed")
    owners = [item["emailAddress"] for item in ops.permission_list(drive) if item.get("role") == "owner" and item.get("emailAddress")]
    expected = {(account, account[:-2], code, f"2026-10-{day:02d}")
                for account in ACCOUNTS for code in CELL_ROSTER[account] for day in range(1, 20)}
    receipts = {}
    with psycopg.connect(host=os.environ["E2E_PG_HOST"], port=int(os.environ["E2E_PG_PORT"]),
            dbname=DB_NAME, user=DB_USER, password=os.environ["E2E_PG_ROLE_PASSWORD"]) as connection:
        actual_identity = connection.execute("SELECT current_database(),current_user,current_schema(),(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)").fetchone()
        if actual_identity != (DB_NAME, DB_USER, "public", False):
            raise ops.Blocked("runtime_db_identity_mismatch")
        sources = connection.execute("SELECT generation_id,account,cluster_id,cell_code,report_date::text,row_count FROM finpay_cell_sources_v1").fetchall()
        if len(sources) != 342 or {(row[1], row[2], row[3], row[4]) for row in sources} != expected:
            raise ops.Blocked("source_date_cell_coverage_mismatch")
        counts = collections.Counter()
        for generation, row_hash, row in connection.execute("SELECT generation_id,row_fingerprint,raw_row FROM finpay_cell_ledger_v1"):
            if fingerprint(row) != row_hash or any(type(row.get(key)) is not int for key in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir")):
                raise ops.Blocked("raw_source_balance_fingerprint_mismatch")
            counts[generation] += 1
        if any(counts[row[0]] != row[5] for row in sources):
            raise ops.Blocked("raw_row_count_mismatch")
        selected = connection.execute("SELECT account,cluster_id,cell_code,report_date::text FROM finpay_cell_current_v1").fetchall()
        if len(selected) != 342 or set(selected) != expected:
            raise ops.Blocked("selected_history_coverage_mismatch")
        if connection.execute("SELECT count(*) FROM finpay_cell_calculations_v1").fetchone()[0] != 342:
            raise ops.Blocked("calculation_count_mismatch")
        for account in ACCOUNTS:
            cluster = account[:-2]
            publications = connection.execute("SELECT epoch,range_start::text,range_end::text,status,snapshot_fingerprint,snapshot FROM finpay_cell_publications_v1 WHERE cluster_id=%s AND target_id=%s", (cluster, WORKBOOK)).fetchall()
            if len(publications) != 1:
                raise ops.Blocked("expected_one_bulk_publication_per_cluster")
            epoch, first, last, pub_state, snapshot_hash, snapshot = publications[0]
            if (epoch, first, last, pub_state) != (1, START, END, "COMPLETE"):
                raise ops.Blocked("bulk_publication_not_complete")
            if snapshot_hash != fingerprint({"cluster_id": cluster, "target_id": WORKBOOK, "range_start": first, "range_end": last, "snapshot": snapshot}):
                raise ops.Blocked("publication_fingerprint_mismatch")
            dates = connection.execute("SELECT report_date::text,status FROM finpay_cell_publication_dates_v1 WHERE cluster_id=%s AND target_id=%s AND epoch=1 ORDER BY report_date", (cluster, WORKBOOK)).fetchall()
            if dates != [(f"2026-10-{day:02d}", "COMPLETE") for day in range(1, 20)]:
                raise ops.Blocked("publication_date_coverage_mismatch")
            per_cluster = {"target_ids": profile["targets"][account], "target_titles": profile["titles"][account]}
            columns = {code: 4 + 4 * index for index, code in enumerate(CELL_ROSTER[account])}
            receipts[cluster] = {"epoch": epoch, "fingerprint": snapshot_hash,
                "sheets": ops.verify_sheet_values(client, meta, per_cluster, snapshot, profile, owners,
                                                  epoch, snapshot_hash, expected_bank_columns=columns)}
    if len(all_ids) != 32 or any(len(set(group.values())) != 4 for group in profile["targets"].values()):
        raise ops.Blocked("configured_worksheet_scope_invalid")
    receipt = {"scope": scope(), "execution_id": state["execution_id"], "execution_state": "SUCCESS",
        "clusters": 8, "dates": 19, "cell_days": 342, "raw_rows": sum(counts.values()),
        "publications": receipts, "configured_target_ids": sorted(all_ids),
        "mutation_scope": "validated code-owned settings mapped only these 32 worksheet IDs",
        "native_tasks": state["native_tasks"],
        "elapsed_seconds": time.time() - state["execution_started_at"],
        "unrelated_tab_content_metadata_and_sharing_unchanged": True}
    ops.write_json(root / "sanitized-result.json", receipt)
    state.update(state="BULK_VERIFIED", raw_rows=sum(counts.values()), verified_at=time.time())
    save(state)
    ops.emit("bulk_verified", clusters=8, dates=19, cell_days=342, raw_rows=sum(counts.values()), publications=8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "tabs", "backup-unrelated", "bootstrap", "configure-server", "fix-auth", "render-flow", "register-flow", "execute", "status", "inspect-pending", "finalize-pending", "rename-tabs", "restore-unrelated-sheet", "download-handoff", "split-handoffs", "filter-unpublished", "render-remaining-flow", "register-remaining-flow", "execute-remaining-flow", "status-remaining-flow", "record-partial-result", "verify-final", "verify"))
    arguments = parser.parse_args()
    actions = {"prepare": prepare, "tabs": provision_tabs, "backup-unrelated": backup_unrelated,
               "bootstrap": ops.create_database,
               "configure-server": configure_server,
               "fix-auth": fix_auth,
               "render-flow": render_flow, "register-flow": register_flow, "execute": execute,
               "status": status, "inspect-pending": inspect_pending_publication,
               "finalize-pending": finalize_pending_publication,
               "rename-tabs": rename_tabs_to_cluster_aliases,
               "restore-unrelated-sheet": restore_original_unrelated_sheet,
               "download-handoff": download_persisted_handoff,
               "split-handoffs": split_remaining_handoffs,
               "filter-unpublished": filter_unpublished_alias_handoffs,
               "render-remaining-flow": render_remaining_flow,
               "register-remaining-flow": register_remaining_flow,
               "execute-remaining-flow": execute_remaining_flow,
               "status-remaining-flow": status_remaining_flow,
               "record-partial-result": record_partial_bulk_result,
               "verify-final": verify_final_bulk, "verify": verify}
    try:
        actions[arguments.phase]()
    except Exception as error:
        import traceback
        origin = traceback.extract_tb(error.__traceback__)[-1]
        ops.emit("bulk_stopped", phase=arguments.phase,
                 reason=error.reason if isinstance(error, ops.Blocked) else type(error).__name__,
                 origin=f"{Path(origin.filename).name}:{origin.lineno}:{origin.name}")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
