"""Opt-in guarded native E2E for one real FinPay cell day."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DATE = "2026-10-01"
CLUSTER = "412110"
ACCOUNT = "412110_B"
CELL_CODES = ("412111", "412112")
WORKBOOK_ID = "1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg"
WORKBOOK_TITLE = "Salinan dari Monitoring Finpay & LinkAja"
LEGACY_WORKBOOK_ID = "1Y_ZtlE-ugE5dFschxUl4OHPHihMgbluTg3g2mid-0FY"
RUN_SUFFIX = "20261020"
EVIDENCE_DIRNAME = f"cell_single_day_e2e_{RUN_SUFFIX}"
FLOW_ID = f"finpay_cell_single_day_e2e_{RUN_SUFFIX}"
NAMESPACE = f"finance.finpay.e2e_{RUN_SUFFIX}"
FLOW_IMAGE = "finpay-cell-reporting:e2e-20261019-refine"
SCRAPER_IMAGE = "finpay-digipos-cell-scraper:e2e-20261019-refine"
POSTGRES_IMAGE = "postgres:18.4"
KESTRA_IMAGE = "kestra/kestra:v1.3.26"
RUN_NAME = f"finpay-cell-single-day-e2e-{RUN_SUFFIX}"
NETWORK = RUN_NAME
PG_CONTAINER = RUN_NAME + "-pg"
KESTRA_CONTAINER = RUN_NAME + "-kestra"
PG_ALIAS = "finpay-cell-single-day-postgres"
DB_NAME = f"finpay_cell_single_day_e2e_{RUN_SUFFIX}_412110"
DB_USER = "finpay_cell_e2e"
DB_MIGRATOR = "finpay_cell_e2e_migrator"
FLOW_TASKS = (
    "validate_cell_sheet_targets",
    "acquire_cell_cluster_day",
    "preprocess_cell_cluster_day",
    "persist_cell_sources_and_calculations",
    "publish_cell_four_tabs",
)
REQUIRED_ENV_KEYS = ("SECRET_DIGIPOS_PASSWORD", "SECRET_GCP_SA_KEY")
SAFE_DATABASE_DENYLIST = {"finpay", "finpay_txn", "linkaja", "kestra"}
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_SHEET_CELLS = 5_000_000


class Blocked(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def evidence_root() -> Path:
    value = os.environ.get("E2E_ROOT")
    if not value:
        raise Blocked("evidence_root_not_configured")
    root = Path(value)
    if root.is_symlink():
        raise Blocked("evidence_root_symlink")
    return root


def secure_file(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def write_json(path: Path, value, *, exclusive: bool = False) -> None:
    secure_file(path, (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8"), exclusive=exclusive)


def read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise Blocked("required_private_evidence_missing")
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(root: Path) -> dict:
    return read_json(root / "state.json")


def save_state(root: Path, state: dict) -> None:
    temporary = root / ".state.pending"
    write_json(temporary, state)
    os.replace(temporary, root / "state.json")
    os.chmod(root / "state.json", 0o600)


def require_state(state: dict, *allowed: str) -> None:
    if state.get("state") not in allowed:
        raise Blocked("phase_state_not_allowed")


def env_file_values(path: Path, wanted: set[str]) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise Blocked("source_env_file_unavailable")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise Blocked("source_env_file_permissions_too_open")
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if not separator or key not in wanted:
                continue
            if key in values:
                raise Blocked("duplicate_required_source_secret")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value:
                raise Blocked("required_source_secret_empty")
            values[key] = value
    if set(values) != wanted:
        raise Blocked("required_source_secret_missing")
    return values


def decode_source_secrets(values: dict[str, str]) -> tuple[bytes, bytes]:
    def decode(value: str) -> bytes:
        try:
            return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        except Exception:
            return value.encode("utf-8")

    password = decode(values["SECRET_DIGIPOS_PASSWORD"])
    key_bytes = decode(values["SECRET_GCP_SA_KEY"])
    if not password:
        raise Blocked("digipos_secret_invalid")
    try:
        info = json.loads(key_bytes.decode("utf-8"))
    except Exception:
        try:
            info = json.loads(values["SECRET_GCP_SA_KEY"])
            key_bytes = values["SECRET_GCP_SA_KEY"].encode("utf-8")
        except Exception:
            raise Blocked("google_secret_invalid") from None
    if not isinstance(info, dict) or not all(info.get(k) for k in ("client_email", "private_key", "token_uri")):
        raise Blocked("google_secret_invalid")
    return password, json.dumps(info, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def prepare() -> None:
    root = evidence_root()
    host_worktree = Path(os.environ.get("E2E_HOST_WORKTREE", ""))
    expected_root = host_worktree / "finpay_pipeline" / "data" / EVIDENCE_DIRNAME
    if not host_worktree.is_absolute() or root != expected_root:
        raise Blocked("versioned_evidence_path_mismatch")
    if root.exists():
        raise Blocked("evidence_directory_already_exists")
    if not root.parent.is_dir() or root.parent.is_symlink():
        raise Blocked("evidence_parent_invalid")
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    taskworkdir = root / "taskworkdir"
    if not taskworkdir.is_absolute():
        raise Blocked("host_taskworkdir_not_absolute")
    taskworkdir.mkdir(mode=0o700)
    (taskworkdir / "tmp").mkdir(mode=0o700)
    source_path = Path(os.environ.get("E2E_SOURCE_ENV", ""))
    source_values = env_file_values(source_path, set(REQUIRED_ENV_KEYS))
    digipos_password, google_json = decode_source_secrets(source_values)
    try:
        google_info = json.loads(google_json)
    except Exception:
        raise Blocked("google_secret_invalid") from None
    if not google_info.get("client_email"):
        raise Blocked("google_secret_invalid")
    del source_values

    pg_admin_password = secrets.token_urlsafe(32)
    app_password = secrets.token_urlsafe(32)
    kestra_password = secrets.token_urlsafe(32)
    pg_port, kestra_port = pick_port(), pick_port()
    while kestra_port == pg_port:
        kestra_port = pick_port()
    secret_entries = {
        "SECRET_DIGIPOS_PASSWORD": base64.b64encode(digipos_password).decode("ascii"),
        "SECRET_GCP_SA_KEY": base64.b64encode(google_json).decode("ascii"),
        "SECRET_FINPAY_DB_HOST": b64(PG_ALIAS),
        "SECRET_FINPAY_DB_PORT": b64("5432"),
        "SECRET_FINPAY_DB_NAME": b64(DB_NAME),
        "SECRET_FINPAY_DB_USER": b64(DB_USER),
        "SECRET_FINPAY_DB_PASSWORD": b64(app_password),
        "KESTRA_BASIC_AUTH_USERNAME": "finpay-e2e",
        "KESTRA_BASIC_AUTH_PASSWORD": kestra_password,
    }
    controller_entries = {
        "E2E_GCP_SA_KEY_B64": base64.b64encode(google_json).decode("ascii"),
        "E2E_PG_ADMIN_USER": "postgres",
        "E2E_PG_ADMIN_PASSWORD": pg_admin_password,
        "E2E_PG_ROLE_USER": DB_USER,
        "E2E_PG_ROLE_PASSWORD": app_password,
        "E2E_PG_DATABASE": DB_NAME,
        "E2E_PG_HOST": "127.0.0.1",
        "E2E_PG_PORT": str(pg_port),
        "E2E_KESTRA_URL": f"http://127.0.0.1:{kestra_port}",
        "E2E_KESTRA_USER": "finpay-e2e",
        "E2E_KESTRA_PASSWORD": kestra_password,
        "E2E_NETWORK": NETWORK,
        "E2E_PG_CLUSTER_ID": "",
    }
    postgres_entries = {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": pg_admin_password,
        "POSTGRES_DB": "postgres",
    }
    for name, entries in (("kestra.env", secret_entries), ("controller.env", controller_entries),
                          ("postgres.env", postgres_entries)):
        secure_file(root / name, ("\n".join(f"{key}={value}" for key, value in entries.items()) + "\n").encode("utf-8"), exclusive=True)
    state = {
        "state": "PREPARED",
        "scope": {"date": DATE, "cluster": CLUSTER, "account": ACCOUNT,
                  "cell_codes": list(CELL_CODES), "spreadsheet_id": WORKBOOK_ID,
                  "workbook_title": WORKBOOK_TITLE, "database": DB_NAME, "schema": "public"},
        "flow": {"namespace": NAMESPACE, "id": FLOW_ID, "reporting_image": FLOW_IMAGE,
                 "scraper_image": SCRAPER_IMAGE},
        "resources": {"network": NETWORK, "postgres_container": PG_CONTAINER,
                      "kestra_container": KESTRA_CONTAINER, "postgres_host_port": pg_port,
                      "kestra_host_port": kestra_port, "postgres_image": POSTGRES_IMAGE,
                      "kestra_image": KESTRA_IMAGE,
                      "taskworkdir": str(taskworkdir)},
        "counts": {},
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(root / "state.json", state, exclusive=True)
    os.chmod(root / "state.json", 0o600)
    emit("prepared", evidence_root=str(root), postgres_port=pg_port, kestra_port=kestra_port,
         network=NETWORK, db_name=DB_NAME, workbook_id=WORKBOOK_ID)


def source_hash(root: Path, variant: str) -> tuple[str, int]:
    patterns = ["*.py", "cell_format/**/*.py", "migrations/**/*", "queries/**/*"]
    if variant == "cell-reporting":
        patterns.append("legacy/**/*")
    source_files = {path.relative_to(root).as_posix(): path for pattern in patterns for path in root.glob(pattern)
                    if path.is_file() and "__pycache__" not in path.parts}
    return digest({name: sha256_bytes(path.read_bytes()) for name, path in sorted(source_files.items())}), len(source_files)


def source_check(variant: str) -> None:
    checkout = Path(os.environ.get("E2E_SOURCE_ROOT", "/checkout")) / "finpay_pipeline"
    runtime = Path(os.environ.get("E2E_RUNTIME_ROOT", "/app/finpay_pipeline"))
    if not checkout.is_dir() or not runtime.is_dir():
        raise Blocked("image_source_directory_missing")
    checkout_hash, checkout_count = source_hash(checkout, variant)
    runtime_hash, runtime_count = source_hash(runtime, variant)
    if not checkout_count or (checkout_hash, checkout_count) != (runtime_hash, runtime_count):
        raise Blocked("packaged_runtime_source_mismatch")
    emit("source_hash_verified", variant=variant, sha256=runtime_hash, files=runtime_count)


def json_request(session, method: str, url: str, *, params=None, json_body=None, data=None, auth=None):
    response = session.request(method, url, params=params, json=json_body, data=data, auth=auth,
                               timeout=(15, 90))
    if not response.ok:
        raise Blocked(f"http_{method.lower()}_{response.status_code}")
    if not response.content:
        return {}
    return response.json()


def google_credentials():
    from finpay_pipeline import decode_cell_google_credentials

    value = os.environ.get("E2E_GCP_SA_KEY_B64", "")
    if not value:
        raise Blocked("google_controller_credential_missing")
    return decode_cell_google_credentials(value)


def sheet_client():
    from finpay_pipeline.cell_format.pipeline import make_cell_sheet_client

    return make_cell_sheet_client(google_credentials())


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"


def drive_metadata(client) -> dict:
    url = f"https://www.googleapis.com/drive/v3/files/{WORKBOOK_ID}"
    response = client.http_client.request("get", url, params={
        "supportsAllDrives": "true",
        "fields": "id,name,mimeType,permissions(id,type,role,emailAddress,domain,deleted,allowFileDiscovery),capabilities(canEdit)",
    })
    if not response.ok:
        raise Blocked(f"drive_metadata_http_{response.status_code}")
    return response.json()


WORKBOOK_FIELDS = (
    "spreadsheetId,properties(title,locale,timeZone,autoRecalc),namedRanges,developerMetadata,"
    "sheets(properties(sheetId,title,index,sheetType,gridProperties,hidden,tabColor,tabColorStyle,rightToLeft),"
    "merges,protectedRanges,developerMetadata,basicFilter,bandedRanges,conditionalFormats,charts,slicers,filterViews)"
)
GRID_FIELDS = (
    "spreadsheetId,properties(title),sheets(properties(sheetId,title,index,gridProperties),merges,protectedRanges,"
    "developerMetadata,basicFilter,bandedRanges,conditionalFormats,charts,slicers,filterViews,"
    "data(startRow,startColumn,rowMetadata,columnMetadata,rowData(values(userEnteredValue,effectiveValue,"
    "formattedValue,userEnteredFormat,note,dataValidation,textFormatRuns))))"
)


def workbook_metadata(client) -> dict:
    response = client.http_client.request("get", sheet_url(WORKBOOK_ID), params={"fields": WORKBOOK_FIELDS})
    if not response.ok:
        raise Blocked(f"sheets_metadata_http_{response.status_code}")
    return response.json()


def column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def full_sheet(client, properties: dict) -> dict:
    grid = properties.get("gridProperties", {})
    rows, columns = int(grid.get("rowCount", 0)), int(grid.get("columnCount", 0))
    if rows < 1 or columns < 1 or rows * columns > MAX_SHEET_CELLS:
        raise Blocked("approved_tab_dimensions_out_of_bounds")
    title = properties["title"].replace("'", "''")
    response = client.http_client.request("get", sheet_url(WORKBOOK_ID), params={
        "includeGridData": "true",
        "ranges": f"'{title}'!A1:{column_name(columns)}{rows}",
        "fields": GRID_FIELDS,
    })
    if not response.ok:
        raise Blocked(f"sheet_grid_http_{response.status_code}")
    data = response.json()
    sheets = data.get("sheets", [])
    if len(sheets) != 1 or sheets[0].get("properties", {}).get("sheetId") != properties.get("sheetId"):
        raise Blocked("sheet_grid_identity_mismatch")
    return sheets[0]


def sheet_meta_digest(meta: dict) -> str:
    return digest(meta)


def other_tabs_snapshot(metadata: dict, target_ids: set[int]) -> dict:
    props = {key: value for key, value in metadata.get("properties", {}).items()}
    tabs = [sheet for sheet in metadata.get("sheets", [])
            if sheet.get("properties", {}).get("sheetId") not in target_ids]
    return {"spreadsheet_properties": props, "named_ranges": metadata.get("namedRanges", []),
            "developer_metadata": metadata.get("developerMetadata", []),
            "tabs": sorted(tabs, key=lambda item: item.get("properties", {}).get("sheetId", -1))}


def permission_list(metadata: dict) -> list[dict]:
    return sorted(metadata.get("permissions", []), key=lambda item: str(item.get("id", "")))


def all_cluster_protection_audit(metadata: dict) -> dict:
    from finpay_pipeline.cell_format.settings import CELL_ROSTER, cell_worksheet_title
    from finpay_pipeline.cell_publication_v2 import CLUSTER_ALIASES, ROLES, MANAGED_PREFIX, GUARD_PREFIX

    by_title: dict[str, list[dict]] = {}
    for sheet in metadata.get("sheets", []):
        by_title.setdefault(sheet.get("properties", {}).get("title", ""), []).append(sheet)
    report = []
    for account in sorted(CELL_ROSTER):
        cluster = account[:-2]
        for role in ROLES:
            titles = [("code", cell_worksheet_title(account, role))]
            if account in CLUSTER_ALIASES:
                titles.append(("cluster_alias", cell_worksheet_title(account, role, "cluster_alias")))
            for scheme, title in dict.fromkeys(titles):
                matches = by_title.get(title, [])
                if len(matches) > 1:
                    raise Blocked("duplicate_cluster_role_tab_title")
                if not matches:
                    continue
                sheet = matches[0]
                protections = sheet.get("protectedRanges", [])
                managed = [item for item in protections if item.get("description", "").startswith(MANAGED_PREFIX)]
                computed = [item for item in managed if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
                inputs = [item for item in managed if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
                holes = sum(len(item.get("unprotectedRanges", [])) for item in protections)
                editors = sorted({email for item in protections for email in item.get("editors", {}).get("users", [])})
                report.append({
                    "cluster": cluster, "role": role, "naming_scheme": scheme, "title": title,
                    "present": True, "sheet_id": sheet["properties"]["sheetId"],
                    "protection_count": len(protections), "managed_computed_count": len(computed),
                    "managed_bank_input_count": len(inputs), "unprotected_range_count": holes,
                    "refresh_guard_count": sum(item.get("description", "").startswith(GUARD_PREFIX)
                                                for item in protections),
                    "editor_count": len(editors), "editor_set_sha256": digest(editors),
                    "protection_sha256": digest(protections),
                })
    return {"summary_tabs_present": sum(item["role"] == "summary" and item["present"] for item in report),
            "clusters_in_roster": len(CELL_ROSTER), "tabs": report}


def target_reset_preconditions(tabs: dict[str, dict]) -> dict:
    from finpay_pipeline.cell_publication import GUARD_PREFIX, _marker_entries

    marker_statuses, refresh_guard_counts, note_state_codes, issues = {}, {}, {}, []
    for role, sheet in tabs.items():
        try:
            markers = _marker_entries(sheet)
        except ValueError:
            markers = []
            issues.append(f"{role}:duplicate_marker")
        if not markers:
            marker_statuses[role] = "ABSENT"
        else:
            try:
                marker = json.loads(markers[0].get("metadataValue", "null"))
                status = marker.get("status") if isinstance(marker, dict) else None
            except (TypeError, json.JSONDecodeError):
                status = None
            marker_statuses[role] = status if isinstance(status, str) else "INVALID"
            if status != "COMPLETE":
                issues.append(f"{role}:unresolved_marker")
        guards = [item for item in sheet.get("protectedRanges", [])
                  if item.get("description", "").startswith(GUARD_PREFIX)]
        refresh_guard_counts[role] = len(guards)
        if guards:
            issues.append(f"{role}:refresh_guard")
        rows = grid_rows(sheet)
        first_cell = (rows[0].get("values", [{}])[0] if rows and rows[0].get("values") else {})
        note = first_cell.get("note", "")
        if "INCOMPLETE" in note or "SYNTHETIC DESIGN PREVIEW" in note or any(
                token in note for token in ("PENDING", "IN_FLIGHT", "UNKNOWN")):
            note_state_codes[role] = "UNRESOLVED"
            issues.append(f"{role}:unresolved_status_note")
        elif note.startswith("Sheet contents VERIFIED; epoch "):
            note_state_codes[role] = "COMPLETE"
        elif note:
            note_state_codes[role] = "OTHER_NOTE"
        else:
            note_state_codes[role] = "ABSENT"
    return {"safe_to_reset": not issues, "marker_statuses": marker_statuses,
            "refresh_guard_counts": refresh_guard_counts, "note_state_codes": note_state_codes,
            "issues": issues}


def inspect_and_backup() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "PREPARED")
    client = sheet_client()
    drive = drive_metadata(client)
    if (drive.get("id"), drive.get("name"), drive.get("mimeType"),
            drive.get("capabilities", {}).get("canEdit")) != (
            WORKBOOK_ID, WORKBOOK_TITLE, "application/vnd.google-apps.spreadsheet", True):
        state["state"] = "BLOCKED_TARGET_METADATA_MISMATCH"
        state["observed_workbook_id"] = drive.get("id")
        state["observed_workbook_title"] = drive.get("name")
        state["observed_can_edit"] = drive.get("capabilities", {}).get("canEdit")
        save_state(root, state)
        emit("workbook_metadata_mismatch", approved_id=WORKBOOK_ID, approved_title=WORKBOOK_TITLE,
             actual_id=drive.get("id"), actual_title=drive.get("name"),
             is_google_sheet=drive.get("mimeType") == "application/vnd.google-apps.spreadsheet",
             can_edit=drive.get("capabilities", {}).get("canEdit"))
        raise Blocked("approved_workbook_identity_mismatch")
    permissions = permission_list(drive)
    from finpay_pipeline.cell_format.settings import cell_worksheet_title
    from finpay_pipeline.cell_publication_v2 import ROLES

    metadata = workbook_metadata(client)
    if metadata.get("spreadsheetId") != WORKBOOK_ID or metadata.get("properties", {}).get("title") != WORKBOOK_TITLE:
        state["state"] = "BLOCKED_TARGET_METADATA_MISMATCH"
        state["observed_workbook_id"] = metadata.get("spreadsheetId")
        state["observed_workbook_title"] = metadata.get("properties", {}).get("title")
        state["observed_can_edit"] = drive.get("capabilities", {}).get("canEdit")
        save_state(root, state)
        raise Blocked("approved_workbook_metadata_mismatch")
    credential_email = google_credentials().get("client_email", "").casefold()
    direct = [item for item in permissions if item.get("type") == "user" and not item.get("deleted")
              and item.get("role") in ("owner", "writer") and item.get("emailAddress")]
    writer_matches = [item for item in direct if item["emailAddress"].casefold() == credential_email]
    if not writer_matches:
        raise Blocked("authenticated_writer_not_direct_owner_or_writer")
    finance_emails = sorted({item["emailAddress"] for item in direct
                             if item["emailAddress"].casefold() != credential_email})
    if not finance_emails:
        raise Blocked("no_existing_finance_editor_principal")
    expected_titles = {role: cell_worksheet_title(ACCOUNT, role) for role in ROLES}
    if set(expected_titles) != {"summary", "invoice", "unusual", "reversal"}:
        raise Blocked("cell_role_contract_changed")
    by_title: dict[str, list[dict]] = {}
    for sheet in metadata.get("sheets", []):
        by_title.setdefault(sheet.get("properties", {}).get("title", ""), []).append(sheet)
    selected = {}
    for role, title in expected_titles.items():
        matches = by_title.get(title, [])
        if len(matches) != 1:
            candidates = [{"title": item.get("properties", {}).get("title"),
                           "sheet_id": item.get("properties", {}).get("sheetId")}
                          for item in metadata.get("sheets", [])
                          if any(token in item.get("properties", {}).get("title", "").casefold()
                                 for token in ("pky", CLUSTER, "summary", "invoice", "unusual", "reversal"))]
            state["state"] = "BLOCKED_TARGET_TABS_UNRESOLVED"
            state["target_resolution"] = {"expected_titles": expected_titles, "missing_role": role,
                                          "exact_match_count": len(matches),
                                          "workbook_sheet_count": len(metadata.get("sheets", [])),
                                          "role_like_candidates": candidates}
            save_state(root, state)
            emit("target_tab_title_resolution_failed", expected_titles=expected_titles,
                 workbook_sheet_count=len(metadata.get("sheets", [])), missing_role=role,
                 exact_match_count=len(matches), role_like_candidates=candidates)
            raise Blocked("approved_target_tab_missing_or_ambiguous")
        selected[role] = matches[0]
    target_ids = {int(item["properties"]["sheetId"]) for item in selected.values()}
    if len(target_ids) != 4:
        raise Blocked("approved_target_tab_ids_not_unique")
    full_tabs = {role: full_sheet(client, item["properties"]) for role, item in selected.items()}
    protected_audit = all_cluster_protection_audit(metadata)
    reset_audit = target_reset_preconditions(full_tabs)
    baseline = {
        "workbook": {"id": WORKBOOK_ID, "title": WORKBOOK_TITLE,
                     "spreadsheet_metadata": metadata, "drive_metadata": drive,
                     "permissions": permissions},
        "target_tabs": {role: full_tabs[role] for role in sorted(full_tabs)},
        "target_titles": expected_titles,
        "target_ids": {role: int(selected[role]["properties"]["sheetId"]) for role in selected},
        "other_tabs_sha256": digest(other_tabs_snapshot(metadata, target_ids)),
        "permissions_sha256": digest(permissions),
        "all_cluster_protection_audit": protected_audit,
        "writer_email": credential_email,
        "finance_emails": finance_emails,
    }
    backup_bytes = (json.dumps(baseline, sort_keys=True, separators=(",", ":"),
                               allow_nan=False) + "\n").encode("utf-8")
    if len(backup_bytes) > MAX_BACKUP_BYTES:
        raise Blocked("private_backup_exceeds_size_limit")
    backup_path = root / "workbook-backup.json"
    secure_file(backup_path, backup_bytes, exclusive=True)
    backup_hash = sha256_bytes(backup_bytes)
    secure_file(root / "workbook-backup.sha256", (backup_hash + "\n").encode("ascii"), exclusive=True)
    if sha256_bytes(backup_path.read_bytes()) != (root / "workbook-backup.sha256").read_text().strip():
        raise Blocked("private_backup_checksum_verification_failed")

    profile = {
        "writer_email": credential_email,
        "finance_emails": finance_emails,
        "target_worksheet_ids": {ACCOUNT: baseline["target_ids"]},
    }
    write_json(root / "profile.json", profile)
    state.update({
        "state": "BACKUP_VERIFIED" if reset_audit["safe_to_reset"] else "BLOCKED_UNRESOLVED_TARGET_PUBLICATION_STATE",
        "target_titles": expected_titles,
        "target_ids": baseline["target_ids"],
        "permissions_sha256": baseline["permissions_sha256"],
        "other_tabs_sha256": baseline["other_tabs_sha256"],
        "backup_sha256": backup_hash,
        "profile_sha256": digest({"writer": credential_email, "finance": finance_emails,
                                   "target_ids": baseline["target_ids"]}),
        "finance_editor_count": len(finance_emails),
        "nonowner_finance_count": sum(item.get("role") != "owner" for item in direct
                                       if item.get("emailAddress") in finance_emails),
        "cluster_protection_audit_before": protected_audit,
        "reset_preconditions": reset_audit,
    })
    save_state(root, state)
    if not reset_audit["safe_to_reset"]:
        emit("target_tabs_have_unresolved_publication_state", target_ids=baseline["target_ids"],
             marker_statuses=reset_audit["marker_statuses"], refresh_guard_counts=reset_audit["refresh_guard_counts"],
             note_state_codes=reset_audit["note_state_codes"], backup_sha256=backup_hash)
        raise Blocked("target_publication_state_requires_reconciliation")
    emit("backup_verified", workbook_id=WORKBOOK_ID, workbook_title=WORKBOOK_TITLE,
         target_ids=baseline["target_ids"], backup_bytes=len(backup_bytes),
         backup_sha256=backup_hash, permissions_sha256=baseline["permissions_sha256"],
         finance_editor_count=len(finance_emails),
         nonowner_finance_count=state["nonowner_finance_count"],
         cluster_summary_tabs_present=protected_audit["summary_tabs_present"],
         protection_audit_sha256=digest(protected_audit),
         reset_preconditions=reset_audit)


def add_profile_prefix(flow: str, profile: dict) -> str:
    roles = profile["target_worksheet_ids"][ACCOUNT]
    if set(roles) != {"summary", "invoice", "unusual", "reversal"} or len(set(roles.values())) != 4:
        raise Blocked("private_target_profile_invalid")
    assignment = "\n".join((
        "       from finpay_pipeline.cell_format import settings as _e2e_settings",
        f"       _e2e_settings.CELL_FORMAT_SPREADSHEET_ID = {json.dumps(WORKBOOK_ID)}",
        f"       _e2e_settings.LEGACY_SPREADSHEET_ID = {json.dumps(LEGACY_WORKBOOK_ID)}",
        f"       _e2e_settings.CELL_FORMAT_WRITER_EMAIL = {json.dumps(profile['writer_email'])}",
        f"       _e2e_settings.CELL_FORMAT_FINANCE_EMAILS = {json.dumps(profile['finance_emails'])}",
        f"       _e2e_settings.CELL_FORMAT_WORKSHEET_IDS = {json.dumps({ACCOUNT: roles}, sort_keys=True)}",
    ))
    substitutions = (
        ("id: finpay_cell_day_v1\n", f"id: {FLOW_ID}\n"),
        ("namespace: finance.finpay\n", f"namespace: {NAMESPACE}\n"),
        ("containerImage: finpay-cell-reporting:2.0", f"containerImage: {FLOW_IMAGE}"),
        ("containerImage: finpay-digipos-cell-scraper:2.0", f"containerImage: {SCRAPER_IMAGE}"),
        ("script: |\n       import os\n       from finpay_pipeline.cell_format import validate_sheet_range",
         "script: |\n" + assignment + "\n       import os\n       from finpay_pipeline.cell_format import validate_sheet_range"),
        ("script: |\n       import os\n       from kestra import Kestra\n       from finpay_pipeline import decode_cell_google_credentials",
         "script: |\n" + assignment + "\n       import os\n       from kestra import Kestra\n       from finpay_pipeline import decode_cell_google_credentials"),
    )
    for old, new in substitutions:
        if flow.count(old) != 1:
            raise Blocked("source_flow_contract_changed")
        flow = flow.replace(old, new, 1)
    if flow.count(f"containerImage: {FLOW_IMAGE}") != 4 or flow.count(f"containerImage: {SCRAPER_IMAGE}") != 1:
        raise Blocked("isolated_flow_image_profile_invalid")
    if "triggers:" in flow or "namespace: finance.finpay\n" in flow or "finpay-cell-reporting:2.0" in flow:
        raise Blocked("isolated_flow_contains_unapproved_runtime_settings")
    return flow


def register_flow() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "DATABASE_READY")
    source = Path(os.environ["E2E_SOURCE_ROOT"]) / "finpay_pipeline/workflows/finpay_cell_day_v1.yml"
    if source.is_symlink() or not source.is_file():
        raise Blocked("native_day_flow_source_missing")
    baseline = read_json(root / "workbook-backup.json")
    profile = read_json(root / "profile.json")
    if digest({"writer": profile["writer_email"], "finance": profile["finance_emails"],
               "target_ids": profile["target_worksheet_ids"][ACCOUNT]}) != state.get("profile_sha256"):
        raise Blocked("private_target_profile_changed")
    flow = add_profile_prefix(source.read_text(encoding="utf-8"), profile)
    flow_bytes = flow.encode("utf-8")
    flow_path = root / "isolated-flow.yml"
    secure_file(flow_path, flow_bytes, exclusive=True)
    state["flow_sha256"] = sha256_bytes(flow_bytes)
    state["state"] = "FLOW_READY"
    save_state(root, state)
    url = os.environ.get("E2E_KESTRA_URL", "").rstrip("/")
    auth = (os.environ.get("E2E_KESTRA_USER", ""), os.environ.get("E2E_KESTRA_PASSWORD", ""))
    if not url or not all(auth):
        raise Blocked("kestra_controller_auth_missing")
    import requests

    endpoint = f"{url}/api/v1/main/flows/{NAMESPACE}/{FLOW_ID}"
    try:
        response = requests.put(endpoint, data=flow_bytes, headers={"Content-Type": "application/x-yaml"},
                                auth=auth, timeout=(15, 90))
    except Exception as exc:
        state["state"] = "FLOW_REGISTER_UNKNOWN"
        state["flow_register_error_type"] = type(exc).__name__
        save_state(root, state)
        raise Blocked("isolated_flow_registration_unknown") from None
    if not response.ok:
        state["state"] = "FLOW_REGISTER_FAILED"
        state["flow_register_http_status"] = response.status_code
        save_state(root, state)
        raise Blocked(f"isolated_flow_registration_http_{response.status_code}")
    get_response = requests.get(endpoint, auth=auth, timeout=(15, 90))
    if not get_response.ok:
        raise Blocked(f"isolated_flow_readback_http_{get_response.status_code}")
    payload = get_response.json()
    if (payload.get("id"), payload.get("namespace")) != (FLOW_ID, NAMESPACE):
        raise Blocked("isolated_flow_identity_readback_mismatch")
    state["state"] = "FLOW_REGISTERED"
    save_state(root, state)
    emit("isolated_flow_registered", namespace=NAMESPACE, flow_id=FLOW_ID,
         flow_sha256=state["flow_sha256"], tasks=list(FLOW_TASKS), images={"reporting": FLOW_IMAGE,
                                                                           "scraper": SCRAPER_IMAGE})


def create_database() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "BACKUP_VERIFIED")
    if DB_NAME in SAFE_DATABASE_DENYLIST or not re.fullmatch(r"[a-z][a-z0-9_]{8,62}", DB_NAME):
        raise Blocked("test_database_name_guard_failed")
    if state["scope"].get("database") != DB_NAME or state["scope"].get("schema") != "public":
        raise Blocked("test_database_scope_changed")
    if Path(os.environ.get("E2E_RUNTIME_ROOT", "/app/finpay_pipeline")).is_dir() is False:
        raise Blocked("runtime_package_missing")
    import psycopg
    from psycopg import sql

    host, port = os.environ["E2E_PG_HOST"], int(os.environ["E2E_PG_PORT"])
    admin_user, admin_password = os.environ["E2E_PG_ADMIN_USER"], os.environ["E2E_PG_ADMIN_PASSWORD"]
    if admin_user != "postgres" or os.environ["E2E_PG_DATABASE"] != DB_NAME or os.environ["E2E_PG_ROLE_USER"] != DB_USER:
        raise Blocked("postgres_test_identity_mismatch")
    with psycopg.connect(host=host, port=port, dbname="postgres", user=admin_user,
                         password=admin_password, connect_timeout=10, autocommit=True) as admin:
        identity = admin.execute("SELECT current_database(), current_user, (SELECT rolsuper FROM pg_roles WHERE rolname=current_user), (SELECT system_identifier FROM pg_control_system())").fetchone()
        if identity is None or identity[:3] != ("postgres", "postgres", True):
            raise Blocked("postgres_admin_identity_mismatch")
        existing_db = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,)).fetchone()
        existing_roles = admin.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                                       ([DB_USER, DB_MIGRATOR],)).fetchall()
        if existing_db or existing_roles:
            raise Blocked("test_database_or_role_already_exists")
        state["state"] = "DB_CREATE_IN_FLIGHT"
        save_state(root, state)
        try:
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(DB_MIGRATOR)))
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(DB_USER), sql.Literal(os.environ["E2E_PG_ROLE_PASSWORD"])))
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(DB_NAME), sql.Identifier(DB_MIGRATOR)))
        except Exception as exc:
            state["state"] = "DB_CREATE_UNKNOWN"
            state["db_error_type"] = type(exc).__name__
            save_state(root, state)
            raise Blocked("test_database_creation_uncertain") from None
        state["postgres_system_identifier"] = str(identity[3])

    from finpay_pipeline.cell_database import CELL_MIGRATION

    migration = CELL_MIGRATION.read_text(encoding="utf-8")
    table_names = sorted(set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)",
                                       migration, re.IGNORECASE)))
    if not table_names or any(not name.startswith("finpay_cell_") for name in table_names):
        raise Blocked("migration_003_scope_guard_failed")
    state["state"] = "MIGRATION_003_IN_FLIGHT"
    state["migration_sha256"] = sha256_bytes(migration.encode("utf-8"))
    state["migration_tables"] = table_names
    save_state(root, state)
    try:
        with psycopg.connect(host=host, port=port, dbname=DB_NAME, user=admin_user,
                             password=admin_password, connect_timeout=10) as migrator:
            with migrator.transaction():
                migrator.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(DB_MIGRATOR)))
                actual = migrator.execute("SELECT current_database(), current_user, current_schema(), (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)").fetchone()
                if actual != (DB_NAME, DB_MIGRATOR, "public", False):
                    raise Blocked("postgres_migration_role_identity_mismatch")
                migrator.execute("SET LOCAL search_path TO public")
                migrator.execute(migration, prepare=False)
        with psycopg.connect(host=host, port=port, dbname=DB_NAME, user=admin_user,
                             password=admin_password, connect_timeout=10, autocommit=True) as admin_db:
            admin_db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            admin_db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(DB_USER)))
            admin_db.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
                sql.Identifier(DB_USER)))
        with psycopg.connect(host=host, port=port, dbname=DB_NAME, user=DB_USER,
                             password=os.environ["E2E_PG_ROLE_PASSWORD"], connect_timeout=10) as app:
            app_identity = app.execute("SELECT current_database(), current_user, current_schema(), "
                "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user), "
                "has_database_privilege(current_user,current_database(),'CREATE'), "
                "has_schema_privilege(current_user,'public','CREATE')").fetchone()
            if app_identity != (DB_NAME, DB_USER, "public", False, False, False):
                raise Blocked("postgres_app_identity_or_ddl_privilege_mismatch")
            actual_tables = sorted(row[0] for row in app.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall())
            if actual_tables != table_names:
                raise Blocked("migration_003_table_readback_mismatch")
    except Exception as exc:
        state["state"] = "MIGRATION_003_UNKNOWN"
        state["migration_error_type"] = type(exc).__name__
        save_state(root, state)
        if isinstance(exc, Blocked):
            raise
        raise Blocked("migration_003_application_uncertain") from None
    state["state"] = "DATABASE_READY"
    state["database_role_superuser"] = False
    state["database_owner_role"] = DB_MIGRATOR
    state["database_migrator_role_superuser"] = False
    state["application_role_can_create_database"] = False
    state["application_role_can_create_schema"] = False
    state["migration_applied"] = "003_finpay_cell_reporting_v1.sql"
    state["counts"]["tables_after_migration"] = len(table_names)
    save_state(root, state)
    emit("isolated_database_ready", database=DB_NAME, schema="public", app_user=DB_USER,
         app_role_superuser=False, app_role_can_create_schema=False,
         migration_owner=DB_MIGRATOR, postgres_system_identifier=state["postgres_system_identifier"],
         migration="003_finpay_cell_reporting_v1.sql", migration_sha256=state["migration_sha256"],
         table_count=len(table_names))


def verify_backup_and_current(client, baseline: dict, state: dict) -> tuple[dict, dict, dict]:
    backup_path = evidence_root() / "workbook-backup.json"
    expected_hash = (evidence_root() / "workbook-backup.sha256").read_text().strip()
    if sha256_bytes(backup_path.read_bytes()) != expected_hash or expected_hash != state.get("backup_sha256"):
        raise Blocked("private_backup_checksum_changed")
    drive = drive_metadata(client)
    metadata = workbook_metadata(client)
    if (drive.get("id"), drive.get("name"), metadata.get("spreadsheetId"),
            metadata.get("properties", {}).get("title")) != (
            WORKBOOK_ID, WORKBOOK_TITLE, WORKBOOK_ID, WORKBOOK_TITLE):
        raise Blocked("workbook_identity_changed_after_backup")
    permissions = permission_list(drive)
    if digest(permissions) != baseline.get("permissions_sha256"):
        raise Blocked("workbook_sharing_changed_after_backup")
    target_ids = {int(value) for value in baseline["target_ids"].values()}
    if digest(other_tabs_snapshot(metadata, target_ids)) != baseline.get("other_tabs_sha256"):
        raise Blocked("non_target_workbook_metadata_changed_after_backup")
    by_id = {int(item["properties"]["sheetId"]): item for item in metadata.get("sheets", [])}
    current = {}
    for role, sheet_id in baseline["target_ids"].items():
        sheet_meta = by_id.get(int(sheet_id))
        if not sheet_meta or sheet_meta.get("properties", {}).get("title") != baseline["target_titles"][role]:
            raise Blocked("approved_target_tab_identity_changed")
        current[role] = full_sheet(client, sheet_meta["properties"])
    return drive, metadata, current


def verify_reset_request_scope(requests: list[dict], sheets: dict[str, dict], target_ids: set[int]) -> dict:
    allowed_resources = {
        "protectedRangeId": {int(item["protectedRangeId"]) for sheet in sheets.values()
                              for item in sheet.get("protectedRanges", [])},
        "metadataId": {int(item["metadataId"]) for sheet in sheets.values()
                       for item in sheet.get("developerMetadata", [])},
        "bandedRangeId": {int(item["bandedRangeId"]) for sheet in sheets.values()
                           for item in sheet.get("bandedRanges", [])},
        "objectId": {int(item["chartId"]) for sheet in sheets.values() for item in sheet.get("charts", [])},
        "slicerId": {int(item["slicerId"]) for sheet in sheets.values() for item in sheet.get("slicers", [])},
    }
    seen_sheet_ids, resource_count = set(), 0

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "sheetId":
                    seen_sheet_ids.add(int(item))
                elif key in allowed_resources and isinstance(item, int):
                    if item not in allowed_resources[key]:
                        raise Blocked("reset_resource_id_outside_approved_tabs")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for request in requests:
        walk(request)
    if not seen_sheet_ids or seen_sheet_ids - target_ids:
        raise Blocked("reset_request_outside_approved_sheet_ids")
    return {"sheet_ids": sorted(seen_sheet_ids), "request_count": len(requests),
            "target_only": True, "other_sheet_grid_values_read": False}


def reset_approved_tabs() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "FLOW_REGISTERED")
    baseline = read_json(root / "workbook-backup.json")
    client = sheet_client()
    _drive, metadata, current = verify_backup_and_current(client, baseline, state)
    if digest(current) != digest(baseline["target_tabs"]):
        raise Blocked("approved_tabs_changed_since_backup")
    reset_audit = target_reset_preconditions(current)
    if not reset_audit["safe_to_reset"]:
        state["state"] = "BLOCKED_UNRESOLVED_TARGET_PUBLICATION_STATE"
        state["reset_preconditions"] = reset_audit
        save_state(root, state)
        raise Blocked("target_publication_state_requires_reconciliation")
    if digest(reset_audit) != digest(state.get("reset_preconditions")):
        raise Blocked("target_reset_preconditions_changed_after_backup")
    from finpay_pipeline.cell_publication_v2 import ROLES

    target_ids = set(int(value) for value in baseline["target_ids"].values())
    if len(target_ids) != 4 or target_ids != {int(item["properties"]["sheetId"])
                                               for item in baseline["target_tabs"].values()}:
        raise Blocked("reset_target_id_allowlist_mismatch")
    requests = []
    for role in ROLES:
        sheet = baseline["target_tabs"][role]
        sheet_id = int(baseline["target_ids"][role])
        if sheet["properties"]["sheetId"] != sheet_id:
            raise Blocked("reset_target_tab_identity_mismatch")
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {},
                         "fields": "userEnteredValue,userEnteredFormat,note,dataValidation,textFormatRuns"}})
        if sheet.get("merges"):
            requests.append({"unmergeCells": {"range": {"sheetId": sheet_id}}})
        if sheet.get("basicFilter"):
            requests.append({"clearBasicFilter": {"sheetId": sheet_id}})
        for item in sheet.get("protectedRanges", []):
            requests.append({"deleteProtectedRange": {"protectedRangeId": int(item["protectedRangeId"])}})
        for item in sheet.get("developerMetadata", []):
            requests.append({"deleteDeveloperMetadata": {"dataFilter": {
                "developerMetadataLookup": {"metadataId": int(item["metadataId"])}}}})
        for item in sheet.get("bandedRanges", []):
            requests.append({"deleteBanding": {"bandedRangeId": int(item["bandedRangeId"])}})
        for index in reversed(range(len(sheet.get("conditionalFormats", [])))):
            requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}})
        for item in sheet.get("charts", []):
            requests.append({"deleteEmbeddedObject": {"objectId": int(item["chartId"])}})
        for item in sheet.get("slicers", []):
            requests.append({"deleteSlicer": {"slicerId": int(item["slicerId"])}})
    if not requests or len(requests) > 900:
        raise Blocked("approved_tab_reset_request_count_out_of_bounds")
    reset_scope = verify_reset_request_scope(requests, baseline["target_tabs"], target_ids)
    # Every request is limited to the four exact sheet IDs resolved and backed up above.
    state["state"] = "RESET_IN_FLIGHT"
    state["reset_request_count"] = len(requests)
    state["reset_scope"] = reset_scope
    save_state(root, state)
    try:
        workbook = client.open_by_key(WORKBOOK_ID)
        workbook.batch_update({"requests": requests})
    except Exception as exc:
        state["state"] = "RESET_UNKNOWN"
        state["reset_error_type"] = type(exc).__name__
        save_state(root, state)
        raise Blocked("approved_tab_reset_outcome_unknown_no_retry") from None
    state["state"] = "RESET_ACKNOWLEDGED"
    save_state(root, state)
    readback_meta = workbook_metadata(client)
    readback_by_id = {int(item["properties"]["sheetId"]): item for item in readback_meta.get("sheets", [])}
    if digest(other_tabs_snapshot(readback_meta, target_ids)) != baseline["other_tabs_sha256"]:
        raise Blocked("non_target_workbook_metadata_changed_during_reset")
    drive_after = drive_metadata(client)
    if digest(permission_list(drive_after)) != baseline["permissions_sha256"]:
        raise Blocked("workbook_sharing_changed_during_reset")
    after = {}
    for role, sheet_id in baseline["target_ids"].items():
        meta = readback_by_id[int(sheet_id)]
        tab = full_sheet(client, meta["properties"])
        props = tab.get("properties", {})
        if props.get("title") != baseline["target_titles"][role]:
            raise Blocked("reset_tab_title_readback_mismatch")
        old_grid = baseline["target_tabs"][role]["properties"].get("gridProperties", {})
        new_grid = props.get("gridProperties", {})
        if (old_grid.get("rowCount"), old_grid.get("columnCount")) != (
                new_grid.get("rowCount"), new_grid.get("columnCount")):
            raise Blocked("reset_tab_dimensions_changed")
        if (tab.get("protectedRanges") or tab.get("developerMetadata") or tab.get("merges")
                or tab.get("basicFilter") or tab.get("bandedRanges") or tab.get("conditionalFormats")
                or tab.get("charts") or tab.get("slicers")):
            raise Blocked("reset_tab_metadata_not_empty")
        for grid_data in tab.get("data", []):
            for row in grid_data.get("rowData", []):
                for cell in row.get("values", []):
                    if any(cell.get(key) not in (None, {}, [], "") for key in (
                            "userEnteredValue", "userEnteredFormat", "note", "dataValidation", "textFormatRuns")):
                        raise Blocked("reset_tab_cell_readback_not_empty")
        after[role] = {"sheet_id": int(sheet_id), "grid_rows": int(new_grid.get("rowCount", 0)),
                       "grid_columns": int(new_grid.get("columnCount", 0)), "empty": True}
    state["state"] = "RESET_COMPLETE"
    state["reset_tabs"] = after
    state["reset_scope"] = reset_scope
    state["reset_other_tabs_sha256"] = digest(other_tabs_snapshot(readback_meta, target_ids))
    state["reset_permissions_sha256"] = digest(permission_list(drive_after))
    save_state(root, state)
    emit("approved_tabs_reset_and_verified", tab_ids=baseline["target_ids"], request_count=len(requests),
         other_tabs_sha256=state["reset_other_tabs_sha256"],
         permissions_sha256=state["reset_permissions_sha256"])


def api_auth() -> tuple[str, str]:
    auth = (os.environ.get("E2E_KESTRA_USER", ""), os.environ.get("E2E_KESTRA_PASSWORD", ""))
    if not all(auth):
        raise Blocked("kestra_controller_auth_missing")
    return auth


def kestra_url() -> str:
    value = os.environ.get("E2E_KESTRA_URL", "").rstrip("/")
    if not value.startswith("http://127.0.0.1:"):
        raise Blocked("kestra_endpoint_not_loopback")
    return value


def trigger_and_wait() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "RESET_COMPLETE")
    import requests

    url = kestra_url()
    auth = api_auth()
    endpoint = f"{url}/api/v1/main/executions/{NAMESPACE}/{FLOW_ID}"
    state["state"] = "EXECUTION_TRIGGER_IN_FLIGHT"
    state["execution_trigger_count"] = 1
    save_state(root, state)
    try:
        response = requests.post(endpoint, data={"report_date": DATE, "cluster_id": CLUSTER,
                                                  "dry_run": "false", "write_gsheet": "true"},
                                 auth=auth, timeout=(15, 90))
    except Exception as exc:
        state["state"] = "EXECUTION_TRIGGER_UNKNOWN"
        state["trigger_error_type"] = type(exc).__name__
        save_state(root, state)
        raise Blocked("execution_trigger_outcome_unknown_no_retry") from None
    if not response.ok:
        state["state"] = "EXECUTION_TRIGGER_FAILED"
        state["trigger_http_status"] = response.status_code
        save_state(root, state)
        raise Blocked(f"execution_trigger_http_{response.status_code}")
    payload = response.json()
    execution_id = payload.get("id")
    if not isinstance(execution_id, str) or not execution_id:
        state["state"] = "EXECUTION_TRIGGER_UNKNOWN"
        save_state(root, state)
        raise Blocked("execution_trigger_missing_id_no_retry")
    state["execution_id"] = execution_id
    state["state"] = "EXECUTION_RUNNING"
    save_state(root, state)
    emit("native_execution_started", execution_id=execution_id, namespace=NAMESPACE, flow_id=FLOW_ID,
         report_date=DATE, cluster=CLUSTER)
    deadline = time.monotonic() + 6 * 60 * 60
    detail_url = f"{url}/api/v1/main/executions/{execution_id}"
    while time.monotonic() < deadline:
        try:
            detail_response = requests.get(detail_url, auth=auth, timeout=(15, 60))
            if detail_response.ok:
                detail = detail_response.json()
                current = detail.get("state", {}).get("current")
                if current in ("SUCCESS", "WARNING", "FAILED", "KILLED", "CANCELLED", "PAUSED", "RESTARTED"):
                    state["execution_state"] = current
                    state["state"] = "EXECUTION_TERMINAL"
                    state["execution_detail_keys"] = sorted(detail.keys())
                    save_state(root, state)
                    if current != "SUCCESS":
                        raise Blocked("native_execution_not_success")
                    emit("native_execution_terminal", execution_id=execution_id, state=current)
                    return
        except Blocked:
            raise
        except Exception:
            pass
        time.sleep(5)
    state["state"] = "EXECUTION_WAIT_TIMEOUT"
    save_state(root, state)
    raise Blocked("native_execution_wait_timeout_no_retrigger")


def execution_task_runs(session, state: dict) -> list[dict]:
    import requests

    url, auth = kestra_url(), api_auth()
    execution_id = state.get("execution_id")
    response = requests.get(f"{url}/api/v1/main/executions/{execution_id}/taskruns",
                            params={"size": 1000}, auth=auth, timeout=(15, 90))
    if response.ok:
        payload = response.json()
        runs = payload.get("results", payload.get("taskRuns", payload if isinstance(payload, list) else []))
        if isinstance(runs, list):
            return runs
    detail = json_request(session, "get", f"{url}/api/v1/main/executions/{execution_id}", auth=auth)
    runs = detail.get("taskRunList", [])
    if not isinstance(runs, list) or not runs:
        raise Blocked("native_task_run_list_unavailable")
    return runs


def task_vars(run: dict) -> dict:
    outputs = run.get("outputs") or {}
    if isinstance(outputs, dict):
        vars_value = outputs.get("vars")
        if isinstance(vars_value, dict):
            return vars_value
    vars_value = run.get("vars")
    return vars_value if isinstance(vars_value, dict) else {}


def verify_manifest_vars(runs: list[dict]) -> dict:
    by_task: dict[str, list[dict]] = {}
    for run in runs:
        by_task.setdefault(run.get("taskId", ""), []).append(run)
    if set(by_task) != set(FLOW_TASKS) or any(len(by_task[name]) != 1 for name in FLOW_TASKS):
        raise Blocked("native_execution_task_coverage_mismatch")
    task_states = {}
    for task_id in FLOW_TASKS:
        run = by_task[task_id][0]
        current = run.get("state", {}).get("current")
        if current != "SUCCESS":
            raise Blocked("native_task_not_success")
        task_states[task_id] = current
    acquired = task_vars(by_task["acquire_cell_cluster_day"][0])
    prepared = task_vars(by_task["preprocess_cell_cluster_day"][0])
    published = task_vars(by_task["publish_cell_four_tabs"][0])
    if (acquired.get("report_date"), acquired.get("cluster_id"), acquired.get("account"),
            acquired.get("cells")) != (DATE, CLUSTER, ACCOUNT, 2):
        raise Blocked("acquisition_manifest_scope_mismatch")
    source_hash = acquired.get("source_bundle_sha256", "")
    prepared_hash = prepared.get("prepared_sha256", "")
    published_hash = published.get("fingerprint", "")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source_hash)) or not re.fullmatch(r"[0-9a-f]{64}", str(prepared_hash)):
        raise Blocked("native_artifact_checksum_missing")
    if not re.fullmatch(r"[0-9a-f]{64}", str(published_hash)):
        raise Blocked("native_publication_fingerprint_missing")
    if (prepared.get("report_date"), prepared.get("cluster_id")) != (DATE, CLUSTER):
        raise Blocked("preprocessed_artifact_scope_mismatch")
    if published.get("status") != "COMPLETE" or published.get("epoch") != 1:
        raise Blocked("native_publication_receipt_incomplete")
    return {"task_states": task_states, "account": ACCOUNT, "cells": 2,
            "source_bundle_sha256": source_hash, "prepared_sha256": prepared_hash,
            "source_artifact_readback": "PREPROCESS_SUCCESS_WITH_SOURCE_SHA256_CHECK",
            "publication_fingerprint": published_hash,
            "publication_status": published.get("status"), "publication_epoch": published.get("epoch")}


def google_value(value: dict):
    if not value:
        return ""
    for key in ("formulaValue", "stringValue", "numberValue", "boolValue", "errorValue"):
        if key in value:
            return value[key]
    return ""


def value_equal(expected, actual) -> bool:
    if expected in (None, "") and actual in (None, ""):
        return True
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)
    return expected == actual


def grid_rows(sheet: dict) -> list[list[dict]]:
    data = sheet.get("data", [])
    return data[0].get("rowData", []) if data else []


def verify_protections(sheet: dict, role: str, matrix: dict, sheet_id: int,
                       writer_email: str, finance_emails: list[str], owners: list[str]) -> dict:
    from finpay_pipeline.cell_publication_v2 import MANAGED_PREFIX, GUARD_PREFIX

    protections = sheet.get("protectedRanges", [])
    computed = [item for item in protections if item.get("description") == MANAGED_PREFIX + "COMPUTED"]
    inputs = [item for item in protections if item.get("description") == MANAGED_PREFIX + "BANK INPUT"]
    if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
        raise Blocked("published_refresh_guard_remains")
    if len(computed) != 1 or any(not item.get("description", "").startswith(MANAGED_PREFIX) for item in protections):
        raise Blocked("published_protection_set_unexpected")
    principal = computed[0]
    expected_holes = []
    expected_inputs = []
    for entry in matrix.get("editable_inputs", []):
        expected_inputs.append({"sheetId": sheet_id, "startRowIndex": entry["row"] - 1,
                                "endRowIndex": entry["row"], "startColumnIndex": entry["column"] - 1,
                                "endColumnIndex": entry["column"]})
    expected_inputs.sort(key=lambda item: (item["startRowIndex"], item["startColumnIndex"]))
    if role == "summary":
        expected_holes = expected_inputs
        if len(inputs) != len(expected_inputs):
            raise Blocked("summary_bank_input_protection_count_mismatch")
    elif inputs:
        raise Blocked("non_summary_tab_has_bank_input_protection")
    actual_holes = sorted(principal.get("unprotectedRanges", []),
                          key=lambda item: (item.get("startRowIndex", -1), item.get("startColumnIndex", -1)))
    if (principal.get("range") != {"sheetId": sheet_id} or principal.get("warningOnly", False)
            or actual_holes != expected_holes):
        raise Blocked("computed_protection_range_mismatch")

    def editors_match(actual: dict, allowed: list[str]) -> bool:
        users = set(actual.get("users", []))
        return (users <= set(allowed) | set(owners) and set(allowed) - set(owners) <= users
                and not actual.get("groups") and not actual.get("domainUsersCanEdit", False))

    if not editors_match(principal.get("editors", {}), [writer_email]):
        raise Blocked("computed_editor_policy_mismatch")
    for item in inputs:
        if item.get("unprotectedRanges") or item.get("warningOnly", False):
            raise Blocked("bank_input_protection_not_strict")
        if not editors_match(item.get("editors", {}), [writer_email, *finance_emails]):
            raise Blocked("bank_input_editor_policy_mismatch")
    return {"protection_count": len(protections), "computed_count": len(computed),
            "bank_input_count": len(inputs), "unprotected_range_count": len(actual_holes),
            "protection_sha256": digest(protections),
            "bank_editor_count": len({email for item in inputs for email in item.get("editors", {}).get("users", [])}),
            "writer_is_service_account": True}


def verify_sheet_values(client, metadata: dict, state: dict, database_snapshot: dict,
                         profile: dict, owners: list[str], publication_epoch: int,
                         publication_fingerprint: str, expected_bank_columns: dict | None = None) -> dict:
    from finpay_pipeline.cell_publication import _verify_marker

    target_ids = {int(value) for value in state["target_ids"].values()}
    by_id = {int(item["properties"]["sheetId"]): item for item in metadata.get("sheets", [])}
    receipts = {}
    summary_matrix = database_snapshot["worksheets"]["summary"]["matrix"]
    expected_bank = {(item["cell_code"], item["column"]): item for item in summary_matrix["editable_inputs"]}
    expected_columns = {"412111": 4, "412112": 8} if expected_bank_columns is None else expected_bank_columns
    if {code: column for code, column in ((key[0], key[1]) for key in expected_bank)} != expected_columns:
        raise Blocked("summary_mandiri_columns_not_d_h")
    for role, target_id in state["target_ids"].items():
        target_id = int(target_id)
        meta = by_id.get(target_id)
        if not meta or meta.get("properties", {}).get("title") != state["target_titles"][role]:
            raise Blocked("published_sheet_identity_mismatch")
        expected_marker = {"status": "COMPLETE", "epoch": publication_epoch,
                           "fingerprint": publication_fingerprint}
        try:
            _verify_marker(meta, expected_marker)
        except Exception:
            raise Blocked("published_sheet_marker_mismatch") from None
        expected = database_snapshot["worksheets"][role]["values"]
        matrix = database_snapshot["worksheets"][role]["matrix"]
        numeric_preview = matrix.get("numeric_preview")
        rows, columns = len(expected), len(expected[0])
        props = {**meta["properties"], "gridProperties": {
            **meta["properties"].get("gridProperties", {}),
            "rowCount": max(rows, int(meta["properties"].get("gridProperties", {}).get("rowCount", rows))),
            "columnCount": max(columns, int(meta["properties"].get("gridProperties", {}).get("columnCount", columns))),
        }}
        actual_sheet = full_sheet(client, {**meta, "properties": props})
        actual_rows = grid_rows(actual_sheet)
        formulas = formula_errors = formula_results_verified = formats = populated = 0
        actual_matrix = []
        for row_index in range(rows):
            row = []
            source_row = actual_rows[row_index] if row_index < len(actual_rows) else {}
            cells = source_row.get("values", [])
            for column_index in range(columns):
                cell = cells[column_index] if column_index < len(cells) else {}
                user_value = cell.get("userEnteredValue", {})
                effective_value = cell.get("effectiveValue", {})
                actual = google_value(user_value)
                if not value_equal(expected[row_index][column_index], actual):
                    raise Blocked("published_value_or_formula_readback_mismatch")
                if "formulaValue" in user_value:
                    formulas += 1
                    formula_errors += "errorValue" in effective_value
                    if "errorValue" in effective_value:
                        raise Blocked("published_formula_evaluation_error")
                    if role == "summary":
                        evaluated = google_value(effective_value)
                        if not value_equal(numeric_preview[row_index][column_index], evaluated):
                            raise Blocked("published_formula_result_differs_from_numeric_preview")
                        formula_results_verified += 1
                formats += bool(cell.get("userEnteredFormat"))
                populated += bool(user_value)
                row.append({key: cell[key] for key in
                            ("userEnteredValue", "effectiveValue", "userEnteredFormat", "note") if key in cell})
            actual_matrix.append(row)
        if not formats:
            raise Blocked("published_style_readback_empty")
        protection = verify_protections(actual_sheet, role, matrix, target_id,
                                        profile["writer_email"], profile["finance_emails"], owners)
        try:
            _verify_marker(actual_sheet, expected_marker)
        except Exception:
            raise Blocked("published_sheet_marker_readback_mismatch") from None
        grid = actual_sheet.get("properties", {}).get("gridProperties", {})
        receipts[role] = {
            "sheet_id": target_id, "rows": rows, "columns": columns,
            "cell_value_formula_sha256": digest(actual_matrix), "formula_count": formulas,
            "formula_errors": formula_errors, "formula_results_verified": formula_results_verified,
            "populated_cell_count": populated, "styled_cell_count": formats,
            "grid_frozen_rows": grid.get("frozenRowCount"),
            "grid_frozen_columns": grid.get("frozenColumnCount"),
            "grid_hide_gridlines": grid.get("hideGridlines"),
            **protection,
        }
    opening = summary_matrix["numeric_preview"][2]
    for block in summary_matrix["blocks"]:
        if opening[block["end_column"] - 1] != 0:
            raise Blocked("initial_summary_saldo_not_zero")
    return receipts


def verify_database() -> tuple[dict, dict]:
    import psycopg
    from finpay_pipeline.cell_contracts import fingerprint

    host, port = os.environ["E2E_PG_HOST"], int(os.environ["E2E_PG_PORT"])
    admin_user, admin_password = os.environ["E2E_PG_ADMIN_USER"], os.environ["E2E_PG_ADMIN_PASSWORD"]
    role_user, role_password, db_name = os.environ["E2E_PG_ROLE_USER"], os.environ["E2E_PG_ROLE_PASSWORD"], os.environ["E2E_PG_DATABASE"]
    if (db_name, role_user) != (DB_NAME, DB_USER) or db_name in SAFE_DATABASE_DENYLIST:
        raise Blocked("database_verification_target_guard_failed")
    with psycopg.connect(host=host, port=port, dbname="postgres", user=admin_user,
                         password=admin_password, connect_timeout=10) as admin:
        server_id = str(admin.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])
    state = load_state(evidence_root())
    if server_id != state.get("postgres_system_identifier"):
        raise Blocked("postgres_cluster_identity_changed")
    from finpay_pipeline.cell_database import CELL_MIGRATION
    table_names = sorted(set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)",
                                       CELL_MIGRATION.read_text(encoding="utf-8"), re.IGNORECASE)))
    with psycopg.connect(host=host, port=port, dbname=db_name, user=role_user,
                         password=role_password, connect_timeout=10) as connection:
        identity = connection.execute("SELECT current_database(), current_user, current_schema(), "
            "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user), "
            "has_database_privilege(current_user,current_database(),'CREATE'), "
            "has_schema_privilege(current_user,'public','CREATE')").fetchone()
        if identity != (DB_NAME, DB_USER, "public", False, False, False):
            raise Blocked("postgres_app_runtime_identity_or_privilege_mismatch")
        owner = connection.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()").fetchone()[0]
        if owner != DB_MIGRATOR:
            raise Blocked("postgres_migration_owner_mismatch")
        tables = sorted(row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall())
        if tables != table_names:
            raise Blocked("postgres_migration_table_set_mismatch")
        sources = connection.execute(
            "SELECT generation_id,account,cluster_id,cell_code,report_date::text,source_sha256,row_count,manifest "
            "FROM public.finpay_cell_sources_v1 ORDER BY cell_code").fetchall()
        if len(sources) != 2 or {(r[1], r[2], r[3], r[4]) for r in sources} != {
                (ACCOUNT, CLUSTER, CELL_CODES[0], DATE), (ACCOUNT, CLUSTER, CELL_CODES[1], DATE)}:
            raise Blocked("postgres_source_generation_coverage_mismatch")
        if any(not re.fullmatch(r"[0-9a-f]{64}", row[5]) or row[6] < 0 for row in sources):
            raise Blocked("postgres_source_generation_receipt_invalid")
        generations = [row[0] for row in sources]
        raw_rows = connection.execute(
            "SELECT generation_id,source_position,row_fingerprint,raw_row FROM public.finpay_cell_ledger_v1 "
            "WHERE generation_id = ANY(%s) ORDER BY generation_id,source_position", (generations,)).fetchall()
        row_counts: dict[str, int] = {generation: 0 for generation in generations}
        raw_fingerprints = []
        for generation, _position, row_hash, row in raw_rows:
            if (not isinstance(row, dict) or not all(type(row.get(key)) is int for key in
                    ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir")) or fingerprint(row) != row_hash):
                raise Blocked("postgres_raw_balance_or_row_fingerprint_mismatch")
            row_counts[generation] += 1
            raw_fingerprints.append(row_hash)
        if any(row_counts[row[0]] != row[6] for row in sources):
            raise Blocked("postgres_raw_ledger_count_mismatch")
        calculations = connection.execute(
            "SELECT count(*) FROM public.finpay_cell_calculations_v1 WHERE generation_id = ANY(%s)",
            (generations,)).fetchone()[0]
        selected = connection.execute(
            "SELECT account,cluster_id,cell_code,report_date::text,generation_id,calculation_id "
            "FROM public.finpay_cell_current_v1 ORDER BY cell_code").fetchall()
        if calculations != 2 or len(selected) != 2 or {row[2] for row in selected} != set(CELL_CODES):
            raise Blocked("postgres_calculation_or_current_selection_count_mismatch")
        if any((row[0], row[1], row[3]) != (ACCOUNT, CLUSTER, DATE) for row in selected):
            raise Blocked("postgres_current_selection_scope_mismatch")
        publications = connection.execute(
            "SELECT epoch,range_start::text,range_end::text,status,snapshot_fingerprint,snapshot "
            "FROM public.finpay_cell_publications_v1 WHERE cluster_id=%s AND target_id=%s ORDER BY epoch",
            (CLUSTER, WORKBOOK_ID)).fetchall()
        if len(publications) != 1:
            raise Blocked("postgres_publication_epoch_count_mismatch")
        epoch, start, end, status, pub_hash, snapshot = publications[0]
        if (epoch, start, end, status) != (1, DATE, DATE, "COMPLETE"):
            raise Blocked("postgres_publication_epoch_not_complete")
        expected_pub_hash = fingerprint({"cluster_id": CLUSTER, "target_id": WORKBOOK_ID,
                                         "range_start": start, "range_end": end, "snapshot": snapshot})
        if pub_hash != expected_pub_hash:
            raise Blocked("postgres_publication_snapshot_fingerprint_mismatch")
        if (snapshot.get("schema_version"), snapshot.get("spreadsheet_id")) != (2, WORKBOOK_ID):
            raise Blocked("postgres_publication_snapshot_identity_mismatch")
        worksheets = snapshot.get("worksheets", {})
        if set(worksheets) != {"summary", "invoice", "unusual", "reversal"}:
            raise Blocked("postgres_publication_tab_coverage_mismatch")
        state_rows = connection.execute(
            "SELECT report_date::text,status FROM public.finpay_cell_publication_dates_v1 "
            "WHERE cluster_id=%s AND target_id=%s AND epoch=%s ORDER BY report_date",
            (CLUSTER, WORKBOOK_ID, epoch)).fetchall()
        if state_rows != [(DATE, "COMPLETE")]:
            raise Blocked("postgres_publication_date_status_mismatch")
        dirty = connection.execute("SELECT count(*) FROM public.finpay_cell_dirty_dates_v1 WHERE cluster_id=%s", (CLUSTER,)).fetchone()[0]
        manual_resets = connection.execute("SELECT count(*) FROM public.finpay_cell_manual_resets_v1 WHERE cluster_id=%s AND target_id=%s", (CLUSTER, WORKBOOK_ID)).fetchone()[0]
        operations = connection.execute("SELECT status FROM public.finpay_cell_target_operations_v1 WHERE cluster_id=%s AND target_id=%s", (CLUSTER, WORKBOOK_ID)).fetchall()
        config_count = connection.execute("SELECT count(*) FROM public.finpay_cell_configs_v1").fetchone()[0]
        if dirty != 0 or manual_resets != 0 or operations != [("IDLE",)] or config_count != 1:
            raise Blocked("postgres_final_operation_state_mismatch")
    by_cell = {row[3]: row for row in sources}
    counts = {code: {"sources": 1, "raw_rows": row_counts[by_cell[code][0]], "calculations": 1,
                     "current_selection": 1, "raw_balance_rows_preserved": row_counts[by_cell[code][0]]}
              for code in CELL_CODES}
    summary = {"database": DB_NAME, "schema": "public", "app_user": DB_USER,
               "app_role_superuser": False, "app_role_can_create_database": False,
               "app_role_can_create_schema": False, "database_owner_role": DB_MIGRATOR,
               "postgres_system_identifier": server_id,
               "source_generations": len(sources), "ledger_rows": len(raw_rows),
               "raw_balance_fields_verified": len(raw_rows), "calculations": calculations,
               "current_selections": len(selected), "publication_epoch": epoch,
               "publication_status": status, "publication_dates_complete": len(state_rows),
               "snapshot_fingerprint": pub_hash, "snapshot_fingerprint_verified": True,
               "manual_reset_rows": manual_resets, "dirty_dates": dirty,
               "target_operation_status": operations[0][0], "configuration_rows": config_count,
               "raw_ledger_fingerprint_sha256": digest(raw_fingerprints),
               "publication_snapshot_sha256": pub_hash, "counts_by_cell": counts}
    return summary, snapshot


def verify_all() -> None:
    root = evidence_root()
    state = load_state(root)
    require_state(state, "EXECUTION_TERMINAL")
    if state.get("execution_state") != "SUCCESS" or state.get("execution_trigger_count") != 1:
        raise Blocked("execution_success_or_single_trigger_not_confirmed")
    import requests

    url, auth = kestra_url(), api_auth()
    runs = execution_task_runs(requests, state)
    execution_receipt = verify_manifest_vars(runs)
    baseline = read_json(root / "workbook-backup.json")
    profile = read_json(root / "profile.json")
    client = sheet_client()
    drive = drive_metadata(client)
    if digest(permission_list(drive)) != baseline["permissions_sha256"]:
        raise Blocked("workbook_sharing_changed_during_publication")
    metadata = workbook_metadata(client)
    target_ids = {int(value) for value in state["target_ids"].values()}
    if target_ids != {int(value) for value in baseline["target_ids"].values()}:
        raise Blocked("configured_targets_differ_from_backed_up_targets")
    if digest(other_tabs_snapshot(metadata, target_ids)) != baseline["other_tabs_sha256"]:
        raise Blocked("non_target_workbook_metadata_changed_during_publication")
    protection_audit = all_cluster_protection_audit(metadata)
    database_receipt, snapshot = verify_database()
    if execution_receipt["publication_fingerprint"] != database_receipt["snapshot_fingerprint"]:
        raise Blocked("native_and_database_publication_fingerprints_differ")
    if snapshot.get("source_calculations", {}).keys() != {DATE}:
        raise Blocked("publication_snapshot_contains_other_dates")
    if (snapshot["worksheets"]["summary"]["matrix"].get("row_keys", [""])[0:1] == []):
        raise Blocked("summary_matrix_invalid")
    owners = sorted({item["emailAddress"] for item in permission_list(drive)
                     if item.get("type") == "user" and item.get("role") == "owner" and item.get("emailAddress")})
    sheets = verify_sheet_values(client, metadata, state, snapshot, profile, owners,
                                 database_receipt["publication_epoch"],
                                 database_receipt["snapshot_fingerprint"])
    if digest(other_tabs_snapshot(workbook_metadata(client), target_ids)) != baseline["other_tabs_sha256"]:
        raise Blocked("non_target_workbook_metadata_changed_after_readback")
    state["state"] = "E2E_VERIFIED"
    state["execution_receipt"] = execution_receipt
    state["database_receipt"] = database_receipt
    state["sheet_receipt"] = sheets
    state["cluster_protection_audit_after"] = protection_audit
    state["verification"] = {
        "workbook_permissions_unchanged": True,
        "non_target_tab_metadata_and_protections_unchanged": True,
        "non_target_tab_grid_values_read": False,
        "configured_target_ids": sorted(target_ids),
        "configured_target_ids_match_backup": True,
        "publication_mutation_request_audit": "NOT_CAPTURED",
        "other_cluster_metadata_read_only": True,
        "native_execution_success": True,
        "four_governed_tabs_readback_complete": True,
        "initial_summary_saldo_zero": True,
        "summary_mandiri_inputs_d_h": True,
    }
    save_state(root, state)
    report = {
        "state": state["state"], "execution_id": state["execution_id"],
        "execution_state": state["execution_state"], "flow": state["flow"],
        "scope": state["scope"], "resources": state["resources"],
        "backup_sha256": state["backup_sha256"],
        "permissions_sha256": state["permissions_sha256"],
        "other_tabs_sha256": state["other_tabs_sha256"],
        "execution_receipt": execution_receipt,
        "database_receipt": database_receipt,
        "sheet_receipt": sheets,
        "finance_editor_count": state["finance_editor_count"],
        "nonowner_finance_count": state["nonowner_finance_count"],
        "cluster_protection_audit_before": state["cluster_protection_audit_before"],
        "cluster_protection_audit_after": protection_audit,
        "verification": state["verification"],
    }
    write_json(root / "sanitized-result.json", report)
    emit("e2e_verified", execution_id=state["execution_id"], execution_state="SUCCESS",
         database=DB_NAME, publication_epoch=database_receipt["publication_epoch"],
         source_generations=database_receipt["source_generations"],
         ledger_rows=database_receipt["ledger_rows"], calculations=database_receipt["calculations"],
         current_selections=database_receipt["current_selections"],
         workbook_permissions_unchanged=True,
         non_target_tab_metadata_and_protections_unchanged=True,
         non_target_tab_grid_values_read=False,
         backup_sha256=state["backup_sha256"], result_path=str(root / "sanitized-result.json"),
         result_sha256=sha256_bytes((root / "sanitized-result.json").read_bytes()))


def purge_secrets() -> None:
    root = evidence_root()
    state = load_state(root)
    if state.get("state") not in ("PREPARED", "BLOCKED_TARGET_METADATA_MISMATCH", "BLOCKED_TARGET_TABS_UNRESOLVED",
                                  "BLOCKED_UNRESOLVED_TARGET_PUBLICATION_STATE", "E2E_VERIFIED",
                                  "EXECUTION_TERMINAL", "RESET_UNKNOWN",
                                  "EXECUTION_TRIGGER_UNKNOWN", "DB_CREATE_UNKNOWN", "MIGRATION_003_UNKNOWN"):
        raise Blocked("secret_cleanup_not_at_safe_phase")
    removed = []
    for name in ("postgres.env", "kestra.env", "controller.env"):
        path = root / name
        if path.exists():
            path.unlink()
            removed.append(name)
    if not (root / "workbook-backup.json").exists():
        profile = root / "profile.json"
        if profile.exists():
            profile.unlink()
            removed.append("profile.json")
    state["secret_files_removed"] = sorted(set(state.get("secret_files_removed", [])) | set(removed))
    save_state(root, state)
    emit("private_credentials_removed", files=removed)


def self_check() -> None:
    assert DATE == "2026-10-01" and CLUSTER == "412110" and ACCOUNT == CLUSTER + "_B"
    assert CELL_CODES == ("412111", "412112")
    assert WORKBOOK_ID != LEGACY_WORKBOOK_ID and DB_NAME not in SAFE_DATABASE_DENYLIST
    assert column_name(1) == "A" and column_name(26) == "Z" and column_name(27) == "AA"
    assert b64("finpay") == "Zmlu cGF5".replace(" ", "")
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert value_equal("=SUM(A1:A2)", "=SUM(A1:A2)")
    assert value_equal(0, 0.0) and value_equal("", "")
    emit("self_check", result="PASS", contract="one_cluster_one_day_four_tabs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("self-check", "prepare", "source-check", "inspect-backup",
                                           "bootstrap-db", "register-flow", "reset-tabs", "execute",
                                           "verify", "purge-secrets"))
    parser.add_argument("--variant", choices=("cell-reporting", "cell-scraper"))
    args = parser.parse_args()
    try:
        if args.phase == "self-check":
            self_check()
        elif args.phase == "prepare":
            prepare()
        elif args.phase == "source-check":
            if not args.variant:
                raise Blocked("image_variant_required")
            source_check(args.variant)
        elif args.phase == "inspect-backup":
            inspect_and_backup()
        elif args.phase == "bootstrap-db":
            create_database()
        elif args.phase == "register-flow":
            register_flow()
        elif args.phase == "reset-tabs":
            reset_approved_tabs()
        elif args.phase == "execute":
            trigger_and_wait()
        elif args.phase == "verify":
            verify_all()
        elif args.phase == "purge-secrets":
            purge_secrets()
    except Blocked as exc:
        emit("blocked", reason=exc.reason)
        raise SystemExit(2) from None
    except Exception as exc:
        details = {"error_type": type(exc).__name__}
        if isinstance(exc, ModuleNotFoundError):
            details["module"] = exc.name
        emit("failed", **details)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
