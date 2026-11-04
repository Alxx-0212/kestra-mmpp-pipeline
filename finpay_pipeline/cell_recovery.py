"""Single-scope, read-only-proof reconciliation for an ambiguous cell publication.

This module is intentionally separate from ``CellStore.acquire`` and
``CellStore.complete``. It never writes to Google Sheets and only transitions
the exact, approved UNKNOWN publication after its saved workbook state is
independently read back under the normal target and cluster advisory locks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .cell_contracts import OBSERVED_CELL_ROSTER, business_date, fingerprint
from .cell_database import CellDatabaseTarget
from .cell_publication import (
    GUARD_PREFIX,
    MANAGED_PREFIX,
    ExistingSheetAdapter,
    _verify_final_protections,
    _verify_marker,
)
from .cell_publication import CellWorksheetTarget
from .cell_publication_v2 import ROLES, _verify_style, _verify_values, cell_worksheet_title

_APPROVED_RANGE = ("2026-10-01", "2026-10-29")
_APPROVED_EPOCH = 4
_APPROVED_ERROR = "ReadTimeout"
_APPROVED_CLUSTER_ID = "422010"
_APPROVED_LOCAL_DATABASE_TARGET = CellDatabaseTarget(
    "postgres", 5432, "finpay", "public", "finpay",
)
_APPROVED_WORKBOOK_ID_SHA256 = "bf9335230a04911dbf749c9d0fc8af83fa5b00c2aa838f1ccf549a619642f962"
_APPROVED_OPERATION_PIN_SHA256 = "397c0b951543e39a95c0d1ee1c1b23a3fcbeff8bcd60a1f6d40fac4b7eadf591"
_APPROVED_SNAPSHOT_FINGERPRINT = "12169484bb133f8789680b41a93791348dafba096b9ac2a45e833b9cdb9825a3"
_APPROVED_OWNER_SET_SHA256 = "d2fd58394c2563fc98406d3b86bc78b35411eb6273ddd48e4b1844acec0bbfcf"
_APPROVED_FINANCE_SET_SHA256 = "145dc3ce54c60614f307ba83f93f1e7e995beadc338e57eb007914dd55c41542"
_APPROVED_WRITER_SET_SHA256 = "a184484faa26cfde4f710e1ef95f80088f3729f284031bbbb065d05d1d69aa44"
_APPROVED_READER_SET_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_APPROVED_FAILED_EXECUTION_ID_SHA256 = "3053ae00c85f6a76048042b29cf10674a67b4423a8046c47527aeafff0145175"
_APPROVED_OPERATOR_REFERENCE = "current-user-approved-guarded-oct29-recovery-20261030"
_APPROVED_FAILED_FLOW_REVISION = 6
_APPROVED_FINANCE_NAMESPACE = "finance.finpay"
_APPROVED_FINANCE_FLOW_ID = "finpay_cell_bulk_v1"
_APPROVED_WORKBOOK_SHEET_COUNT = 32
_APPROVED_AUDIT_RECEIPT_PATH = Path(
    "/tmp/kilo/finpay-oct29-recovery-20261102/reconciliation-audit.jsonl",
)
_APPROVED_DATABASE_TOC_COUNT = 50
_APPROVED_DATABASE_LISTED_ENTRY_COUNT = 45
_EPOCH5_RANGE = ("2026-10-01", "2026-11-01")
_EPOCH5_EPOCH = 5
_EPOCH5_OPERATION_PIN_SHA256 = "7e63ec7d76235175d5b4eb94c3c93945c6596624f9657cff768ac873fe56cd39"
_EPOCH5_SNAPSHOT_FINGERPRINT = "e4497cd8174132351c2610644ec25945e565be75cc60206a4ec766ff0e8df10b"
_EPOCH5_FAILED_EXECUTION_ID_SHA256 = "505f24f66396617414c5c0ce317eafc3873265863e48e92e585130cbeab30aff"
_EPOCH5_OPERATOR_REFERENCE = "epoch5-owner-authorized-20261102"
_EPOCH5_FAILED_FLOW_REVISION = 6
_EPOCH5_AUDIT_RECEIPT_PATH = Path(
    "/tmp/kilo/finpay-epoch5-recovery-20261102/reconciliation-audit.jsonl",
)
_MAX_GRID_ROWS = 100_000
_MAX_GRID_COLUMNS = 100
_MAX_WORKBOOK_CELLS = 10_000_000
_BACKUP_PURPOSE = "finpay-cell-unknown-recovery"
_WORKBOOK_ARCHIVE_PURPOSE = "finpay-cell-workbook-backup-v1"
_QUIESCENCE_PURPOSE = "finpay-cell-writer-quiescence"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_WORKBOOK_ARCHIVE_BYTES = 768 * 1024 * 1024
_MAX_WORKBOOK_ARCHIVE_CONTENT_BYTES = 1024 * 1024 * 1024
_MAX_WORKBOOK_ARCHIVE_ENTRIES = 4096
_MAX_WORKBOOK_GRID_PAGE_BYTES = 64 * 1024 * 1024
_MAX_QUIESCENCE_AGE = timedelta(minutes=15)
_MAX_DATABASE_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_BACKUP_RECEIPT_BYTES = 16 * 1024 * 1024
_MAX_RESTORE_LIST_BYTES = 8 * 1024 * 1024
_REQUIRED_CELL_BACKUP_TABLES = (
    "finpay_cell_configs_v1",
    "finpay_cell_sources_v1",
    "finpay_cell_ledger_v1",
    "finpay_cell_calculations_v1",
    "finpay_cell_current_v1",
    "finpay_cell_publications_v1",
    "finpay_cell_target_operations_v1",
    "finpay_cell_dirty_dates_v1",
    "finpay_cell_publication_dates_v1",
    "finpay_cell_manual_resets_v1",
)
_REQUIRED_CELL_BACKUP_VIEWS = ("finpay_cell_current_results_v1",)
_APPROVED_STOPPED_WRITERS = {"mmpp-finpay-replication-20261021-kestra-1"}
_APPROVED_RUNNING_LOCAL_CONTAINERS = {"mmpp-finpay-replication-20261021-postgres-1"}
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_OPERATION_ID = re.compile(r"[a-f0-9]{32}\Z")


class CellRecoveryProfile(Enum):
    """The only incident profiles accepted by the recovery API."""

    EPOCH4 = "epoch4"
    EPOCH5 = "epoch5"


@dataclass(frozen=True)
class _RecoveryPolicy:
    profile: CellRecoveryProfile
    report_range: tuple[str, str]
    epoch: int
    expected_error: str
    cluster_id: str
    database_target: CellDatabaseTarget
    workbook_id_sha256: str
    operation_pin_sha256: str
    snapshot_fingerprint: str
    owner_set_sha256: str
    finance_set_sha256: str
    writer_set_sha256: str
    reader_set_sha256: str
    failed_execution_id_sha256: str
    operator_reference: str
    failed_flow_revision: int
    finance_namespace: str
    finance_flow_id: str
    workbook_sheet_count: int
    audit_receipt_path: Path
    database_toc_count: int
    database_listed_entry_count: int
    scope_inputs: tuple[tuple[str, Any], ...]
    stopped_writers: frozenset[str]
    running_local_containers: frozenset[str]


_EPOCH4_POLICY = _RecoveryPolicy(
    profile=CellRecoveryProfile.EPOCH4,
    report_range=_APPROVED_RANGE,
    epoch=_APPROVED_EPOCH,
    expected_error=_APPROVED_ERROR,
    cluster_id=_APPROVED_CLUSTER_ID,
    database_target=_APPROVED_LOCAL_DATABASE_TARGET,
    workbook_id_sha256=_APPROVED_WORKBOOK_ID_SHA256,
    operation_pin_sha256=_APPROVED_OPERATION_PIN_SHA256,
    snapshot_fingerprint=_APPROVED_SNAPSHOT_FINGERPRINT,
    owner_set_sha256=_APPROVED_OWNER_SET_SHA256,
    finance_set_sha256=_APPROVED_FINANCE_SET_SHA256,
    writer_set_sha256=_APPROVED_WRITER_SET_SHA256,
    reader_set_sha256=_APPROVED_READER_SET_SHA256,
    failed_execution_id_sha256=_APPROVED_FAILED_EXECUTION_ID_SHA256,
    operator_reference=_APPROVED_OPERATOR_REFERENCE,
    failed_flow_revision=_APPROVED_FAILED_FLOW_REVISION,
    finance_namespace=_APPROVED_FINANCE_NAMESPACE,
    finance_flow_id=_APPROVED_FINANCE_FLOW_ID,
    workbook_sheet_count=_APPROVED_WORKBOOK_SHEET_COUNT,
    audit_receipt_path=_APPROVED_AUDIT_RECEIPT_PATH,
    database_toc_count=_APPROVED_DATABASE_TOC_COUNT,
    database_listed_entry_count=_APPROVED_DATABASE_LISTED_ENTRY_COUNT,
    scope_inputs=(
        ("start_date", "2026-10-29"),
        ("end_date", "2026-10-29"),
        ("cluster_id", "ALL"),
        ("scrape_workers", 2),
        ("dry_run", False),
        ("write_gsheet", True),
    ),
    stopped_writers=frozenset(_APPROVED_STOPPED_WRITERS),
    running_local_containers=frozenset(_APPROVED_RUNNING_LOCAL_CONTAINERS),
)
_EPOCH5_POLICY = _RecoveryPolicy(
    profile=CellRecoveryProfile.EPOCH5,
    report_range=_EPOCH5_RANGE,
    epoch=_EPOCH5_EPOCH,
    expected_error=_APPROVED_ERROR,
    cluster_id=_APPROVED_CLUSTER_ID,
    database_target=_APPROVED_LOCAL_DATABASE_TARGET,
    workbook_id_sha256=_APPROVED_WORKBOOK_ID_SHA256,
    operation_pin_sha256=_EPOCH5_OPERATION_PIN_SHA256,
    snapshot_fingerprint=_EPOCH5_SNAPSHOT_FINGERPRINT,
    owner_set_sha256=_APPROVED_OWNER_SET_SHA256,
    finance_set_sha256=_APPROVED_FINANCE_SET_SHA256,
    writer_set_sha256=_APPROVED_WRITER_SET_SHA256,
    reader_set_sha256=_APPROVED_READER_SET_SHA256,
    failed_execution_id_sha256=_EPOCH5_FAILED_EXECUTION_ID_SHA256,
    operator_reference=_EPOCH5_OPERATOR_REFERENCE,
    failed_flow_revision=_EPOCH5_FAILED_FLOW_REVISION,
    finance_namespace=_APPROVED_FINANCE_NAMESPACE,
    finance_flow_id=_APPROVED_FINANCE_FLOW_ID,
    workbook_sheet_count=_APPROVED_WORKBOOK_SHEET_COUNT,
    audit_receipt_path=_EPOCH5_AUDIT_RECEIPT_PATH,
    # Retain the existing archive pins; do not relax them without fresh evidence.
    database_toc_count=_APPROVED_DATABASE_TOC_COUNT,
    database_listed_entry_count=_APPROVED_DATABASE_LISTED_ENTRY_COUNT,
    scope_inputs=(
        ("start_date", "2026-10-30"),
        ("end_date", "2026-11-01"),
        ("cluster_id", "ALL"),
        ("scrape_workers", 2),
        ("dry_run", False),
        ("write_gsheet", True),
    ),
    stopped_writers=frozenset(_APPROVED_STOPPED_WRITERS),
    running_local_containers=frozenset(_APPROVED_RUNNING_LOCAL_CONTAINERS),
)
_RECOVERY_POLICIES = MappingProxyType({
    CellRecoveryProfile.EPOCH4: _EPOCH4_POLICY,
    CellRecoveryProfile.EPOCH5: _EPOCH5_POLICY,
})


def _policy_for_profile(profile: CellRecoveryProfile) -> _RecoveryPolicy:
    if type(profile) is not CellRecoveryProfile:
        raise ValueError("an explicit supported CellRecoveryProfile is required")
    return _RECOVERY_POLICIES[profile]


def _policy_for_approval(approval: CellRecoveryApproval) -> _RecoveryPolicy:
    return _policy_for_profile(approval.profile)


def _identity_set_sha256(values) -> str:
    normalized = sorted({value.casefold().strip() for value in values})
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _verify_grid_page_coverage(sheet: dict, title: str, rows: int, columns: int) -> None:
    rectangles = []
    for page in sheet["grid_pages"]:
        try:
            sheet_ref, cells = page["range"].rsplit("!", 1)
        except ValueError as exc:
            raise ValueError("workbook grid page must identify its exact worksheet") from exc
        if sheet_ref.startswith("'") and sheet_ref.endswith("'"):
            page_title = sheet_ref[1:-1].replace("''", "'")
        else:
            page_title = sheet_ref
        if page_title != title:
            raise ValueError("workbook grid page refers to another worksheet")
        match = re.fullmatch(r"\$?([A-Z]+)\$?([1-9][0-9]*):\$?([A-Z]+)\$?([1-9][0-9]*)", cells)
        if not match:
            raise ValueError("workbook grid page has an invalid A1 range")
        first_column, first_row, last_column, last_row = match.groups()
        bounds = (_column_number(first_column), int(first_row),
                  _column_number(last_column), int(last_row))
        if (bounds[0] != 1 or bounds[2] != columns or bounds[1] < 1
                or bounds[3] > rows or bounds[1] > bounds[3]):
            raise ValueError("workbook grid page does not match the pinned sheet extent")
        rectangles.append(bounds)
    cursor = 1
    for _, start_row, _, end_row in sorted(rectangles, key=lambda item: item[1]):
        if start_row != cursor:
            raise ValueError("workbook grid pages do not cover every row exactly once")
        cursor = end_row + 1
    if cursor != rows + 1:
        raise ValueError("workbook grid pages do not cover the complete saved sheet extent")


@dataclass(frozen=True)
class CellRecoveryApproval:
    """Immutable pins approved for one explicitly selected recovery profile."""

    profile: CellRecoveryProfile
    database_target: CellDatabaseTarget
    spreadsheet_id: str
    cluster_id: str
    epoch: int
    range_start: str
    range_end: str
    expected_error: str
    operation_id: str
    operation_started_at: datetime
    snapshot_fingerprint: str
    failed_execution_id: str
    operator_reference: str
    worksheet_targets: tuple[CellWorksheetTarget, ...]
    owner_emails: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_approval(self)


def _validate_approval(approval: CellRecoveryApproval) -> None:
    policy = _policy_for_profile(approval.profile)
    if approval.profile is not policy.profile:
        raise ValueError("recovery profile and immutable policy do not match")
    target = approval.database_target
    if not isinstance(target, CellDatabaseTarget):
        raise ValueError("an explicit FinPay database target is required")
    if (target.host, target.port) != (policy.database_target.host, policy.database_target.port):
        raise ValueError("recovery is restricted to the exact approved local PostgreSQL endpoint")
    if (target.database, target.schema, target.user) != (
        policy.database_target.database, policy.database_target.schema, policy.database_target.user,
    ):
        raise ValueError("recovery is restricted to the approved local FinPay database role/schema")
    target.validate((policy.database_target,))
    if (not isinstance(approval.spreadsheet_id, str) or not approval.spreadsheet_id.strip()
            or len(approval.spreadsheet_id) > 256):
        raise ValueError("the runtime spreadsheet ID must be explicit")
    if (not re.fullmatch(r"\d{6}", approval.cluster_id)
            or approval.cluster_id != policy.cluster_id):
        raise ValueError("recovery is restricted to the specifically approved cluster")
    if hashlib.sha256(approval.spreadsheet_id.encode()).hexdigest() != policy.workbook_id_sha256:
        raise ValueError("recovery is restricted to the specifically approved non-production workbook")
    if type(approval.epoch) is not int or approval.epoch != policy.epoch:
        raise ValueError("recovery is restricted to the approved publication epoch")
    if (approval.range_start, approval.range_end) != policy.report_range:
        raise ValueError("recovery is restricted to the approved report-date range")
    business_date(approval.range_start)
    business_date(approval.range_end)
    if approval.expected_error != policy.expected_error:
        raise ValueError("recovery is restricted to the approved ambiguous error class")
    if not isinstance(approval.operation_id, str) or not _OPERATION_ID.fullmatch(approval.operation_id):
        raise ValueError("the exact target-operation ID must be a lowercase UUID hex value")
    if (not isinstance(approval.operation_started_at, datetime)
            or approval.operation_started_at.tzinfo is None
            or approval.operation_started_at.utcoffset() is None):
        raise ValueError("the exact timezone-aware operation start time is required")
    operation_pin = approval.operation_id + "|" + _utc_timestamp(approval.operation_started_at)
    if hashlib.sha256(operation_pin.encode()).hexdigest() != policy.operation_pin_sha256:
        raise ValueError("recovery is restricted to the exact approved target-operation instance")
    if (not isinstance(approval.snapshot_fingerprint, str)
            or not _SHA256.fullmatch(approval.snapshot_fingerprint)):
        raise ValueError("the exact snapshot SHA-256 fingerprint is required")
    if approval.snapshot_fingerprint != policy.snapshot_fingerprint:
        raise ValueError("recovery is restricted to the approved persisted snapshot")
    if (not isinstance(approval.failed_execution_id, str) or not approval.failed_execution_id.strip()
            or len(approval.failed_execution_id) > 256):
        raise ValueError("the failed execution ID must be explicit")
    if (hashlib.sha256(approval.failed_execution_id.encode()).hexdigest()
            != policy.failed_execution_id_sha256):
        raise ValueError("recovery is restricted to the exact failed local Kestra execution")
    if approval.operator_reference != policy.operator_reference:
        raise ValueError("the exact user-approved recovery authorization reference is required")
    if type(approval.worksheet_targets) is not tuple or len(approval.worksheet_targets) != len(ROLES):
        raise ValueError("exactly four immutable worksheet targets are required")
    if type(approval.owner_emails) is not tuple or not approval.owner_emails:
        raise ValueError("approved workbook owner email pins are required")
    if any(not isinstance(email, str) or not email.strip() for email in approval.owner_emails):
        raise ValueError("workbook owner email pins must be nonempty strings")
    if len({email.casefold() for email in approval.owner_emails}) != len(approval.owner_emails):
        raise ValueError("duplicate workbook owner email pins are not allowed")

    account = approval.cluster_id + "_B"
    by_role = {}
    for worksheet in approval.worksheet_targets:
        if not isinstance(worksheet, CellWorksheetTarget):
            raise ValueError("worksheet pins must use CellWorksheetTarget")
        worksheet.sheet_target()
        if (not isinstance(worksheet.writer_email, str) or not worksheet.writer_email.strip()
                or type(worksheet.finance_emails) is not tuple or not worksheet.finance_emails
                or any(not isinstance(email, str) or not email.strip() for email in worksheet.finance_emails)):
            raise ValueError("worksheet writer and Finance editor pins must be immutable email identities")
        if len({email.casefold() for email in worksheet.finance_emails}) != len(worksheet.finance_emails):
            raise ValueError("duplicate worksheet Finance editor pins are not allowed")
        if worksheet.role in by_role:
            raise ValueError("duplicate worksheet role pin")
        if worksheet.spreadsheet_id != approval.spreadsheet_id:
            raise ValueError("worksheet pin refers to a different workbook")
        if worksheet.title != cell_worksheet_title(account, worksheet.role, worksheet.naming_scheme):
            raise ValueError("worksheet title does not match the pinned cluster role")
        by_role[worksheet.role] = worksheet
    if set(by_role) != set(ROLES):
        raise ValueError("worksheet pins must cover exactly the four publication roles")
    if len({item.worksheet_id for item in approval.worksheet_targets}) != len(ROLES):
        raise ValueError("worksheet IDs must be unique")
    if len({item.title for item in approval.worksheet_targets}) != len(ROLES):
        raise ValueError("worksheet titles must be unique")
    if _identity_set_sha256(item.writer_email for item in approval.worksheet_targets) != policy.writer_set_sha256:
        raise ValueError("worksheet writer identities differ from the exact approved local configuration")
    if _identity_set_sha256(
        [email for item in approval.worksheet_targets for email in item.finance_emails],
    ) != policy.finance_set_sha256:
        raise ValueError("Finance editor identities differ from the exact approved local configuration")
    if _identity_set_sha256(approval.owner_emails) != policy.owner_set_sha256:
        raise ValueError("workbook owners differ from the exact approved non-production target")


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def approval_payload(approval: CellRecoveryApproval) -> dict[str, Any]:
    """Return the canonical, credential-free payload to embed in evidence manifests."""
    if not isinstance(approval, CellRecoveryApproval):
        raise TypeError("CellRecoveryApproval is required")
    return {
        "database_target": {
            "host": approval.database_target.host,
            "port": approval.database_target.port,
            "database": approval.database_target.database,
            "schema": approval.database_target.schema,
            "user": approval.database_target.user,
        },
        "spreadsheet_id": approval.spreadsheet_id,
        "cluster_id": approval.cluster_id,
        "epoch": approval.epoch,
        "range_start": approval.range_start,
        "range_end": approval.range_end,
        "expected_error": approval.expected_error,
        "operation_id": approval.operation_id,
        "operation_started_at": _utc_timestamp(approval.operation_started_at),
        "snapshot_fingerprint": approval.snapshot_fingerprint,
        "failed_execution_id": approval.failed_execution_id,
        "operator_reference": approval.operator_reference,
        "worksheet_targets": [
            {
                "role": target.role,
                "spreadsheet_id": target.spreadsheet_id,
                "worksheet_id": target.worksheet_id,
                "title": target.title,
                "writer_email": target.writer_email,
                "finance_emails": sorted(target.finance_emails),
                "naming_scheme": target.naming_scheme,
            }
            for target in sorted(approval.worksheet_targets, key=lambda item: item.role)
        ],
        "owner_emails": sorted(approval.owner_emails, key=str.casefold),
    }


def approval_fingerprint(approval: CellRecoveryApproval) -> str:
    """SHA-256 fingerprint binding recovery evidence to every approval pin."""
    return fingerprint(approval_payload(approval))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in recovery evidence")
        result[key] = value
    return result


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _external_path(path, *, must_exist: bool) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("recovery evidence paths must be absolute")
    resolved = path.resolve(strict=must_exist)
    repository = Path(__file__).resolve().parents[1]
    if resolved == repository or repository in resolved.parents:
        raise ValueError("recovery evidence must remain outside the repository")
    return resolved


def _check_private_directory(path: Path) -> None:
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("recovery evidence parent must be an owner-only 0700 directory")


def _open_private_file(path: Path, flags: int):
    path = _external_path(path, must_exist=True)
    _check_private_directory(path.parent.resolve(strict=True))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | nofollow)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size <= 0):
        os.close(descriptor)
        raise ValueError("recovery evidence files must be owner-only 0600 single-link regular files")
    return descriptor


def _read_private_bytes(path, *, limit: int = _MAX_JSON_BYTES) -> tuple[bytes, str]:
    descriptor = _open_private_file(Path(path), os.O_RDONLY)
    digest, chunks, size = hashlib.sha256(), [], 0
    try:
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError("recovery JSON evidence exceeds its size bound")
            digest.update(chunk)
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks), digest.hexdigest()


def _read_private_json(path, *, limit: int = _MAX_JSON_BYTES) -> tuple[dict, str]:
    raw, digest = _read_private_bytes(path, limit=limit)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("non-finite JSON number in recovery evidence")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery evidence must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("recovery JSON evidence must be an object")
    return value, digest


def _checked_file(entry, label: str, *, parse_json: bool = False, max_bytes: int | None = None):
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise ValueError(f"{label} must pin exactly a path and SHA-256")
    path = _external_path(entry["path"], must_exist=True)
    expected = entry["sha256"]
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise ValueError(f"{label} SHA-256 must be lowercase hexadecimal")
    if parse_json:
        limit = _MAX_JSON_BYTES if max_bytes is None else max_bytes
        descriptor = _open_private_file(path, os.O_RDONLY)
        try:
            if os.fstat(descriptor).st_size > limit:
                raise ValueError(f"{label} exceeds its size bound")
        finally:
            os.close(descriptor)
        value, actual = _read_private_json(path, limit=limit)
    else:
        descriptor = _open_private_file(path, os.O_RDONLY)
        digest = hashlib.sha256()
        size = 0
        try:
            if max_bytes is not None and os.fstat(descriptor).st_size > max_bytes:
                raise ValueError(f"{label} exceeds its size bound")
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"{label} exceeds its size bound")
                digest.update(chunk)
        finally:
            os.close(descriptor)
        value, actual = None, digest.hexdigest()
    if actual != expected:
        raise ValueError(f"{label} file checksum mismatch")
    return path, actual, value


def _check_workbook_binding(
    binding: dict,
    approval: CellRecoveryApproval,
) -> None:
    policy = _policy_for_approval(approval)
    if (type(binding.get("schema_version")) is not int
            or binding.get("schema_version") != 1
            or binding.get("purpose") != "finpay-cell-workbook-binding"):
        raise ValueError("unsupported workbook backup binding receipt")
    if binding.get("spreadsheet_id") != approval.spreadsheet_id:
        raise ValueError("workbook backup binding spreadsheet ID differs from approval")
    owners = binding.get("owners")
    if (not isinstance(owners, list) or any(not isinstance(item, str) or not item.strip() for item in owners)
            or len({item.casefold() for item in owners}) != len(owners)
            or {item.casefold() for item in owners} != {item.casefold() for item in approval.owner_emails}):
        raise ValueError("workbook backup binding owners differ from approved owner pins")
    sheets = binding.get("sheets")
    if not isinstance(sheets, list) or len(sheets) != policy.workbook_sheet_count:
        raise ValueError("workbook backup binding must contain metadata for all approved tabs")
    identities = {}
    ids, titles = set(), set()
    target_ids = {target.worksheet_id for target in approval.worksheet_targets}
    workbook_cells = 0
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError("workbook backup contains malformed sheet metadata")
        properties = sheet.get("properties", sheet)
        if not isinstance(properties, dict):
            raise ValueError("workbook backup contains malformed sheet properties")
        sheet_id = properties.get("sheetId", properties.get("sheet_id"))
        title = properties.get("title")
        if type(sheet_id) is not int or not isinstance(title, str) or not title:
            sheet_id = sheet.get("worksheet_id", sheet_id)
            title = sheet.get("title", title)
        if type(sheet_id) is not int or not isinstance(title, str) or not title:
            raise ValueError("workbook binding sheet IDs and titles must be explicit")
        if sheet_id in ids or title in titles:
            raise ValueError("workbook backup has duplicate sheet IDs or titles")
        ids.add(sheet_id)
        titles.add(title)
        grid = sheet.get("gridProperties", properties.get("gridProperties"))
        if not isinstance(grid, dict):
            raise ValueError("workbook binding is missing pinned grid dimensions")
        rows, columns = grid.get("rowCount"), grid.get("columnCount")
        if (type(rows) is not int or type(columns) is not int or rows < 1 or columns < 1
                or rows * columns > _MAX_WORKBOOK_CELLS):
            raise ValueError("workbook binding grid extent exceeds the bounded read contract")
        if sheet_id in target_ids and (rows > _MAX_GRID_ROWS or columns > _MAX_GRID_COLUMNS):
            raise ValueError("approved worksheet extent exceeds the bounded read contract")
        workbook_cells += rows * columns
        if workbook_cells > _MAX_WORKBOOK_CELLS:
            raise ValueError("workbook binding allocation exceeds the 10,000,000-cell contract")
        grid_pages = sheet.get("grid_pages")
        if not isinstance(grid_pages, list) or not grid_pages:
            raise ValueError("workbook binding must include hashed grid pages for every tab")
        ranges = set()
        for page in grid_pages:
            if (not isinstance(page, dict) or set(page) != {"range", "sha256"}
                    or not isinstance(page["range"], str) or not page["range"].strip()
                    or not isinstance(page["sha256"], str) or not _SHA256.fullmatch(page["sha256"])):
                raise ValueError("workbook binding grid pages need explicit ranges and SHA-256 values")
            if page["range"] in ranges:
                raise ValueError("workbook binding repeats a grid page range")
            ranges.add(page["range"])
        _verify_grid_page_coverage(sheet, title, rows, columns)
        protections = sheet.get("protectedRanges")
        metadata = sheet.get("developerMetadata")
        if not isinstance(protections, list) or not isinstance(metadata, list):
            raise ValueError("workbook binding must include protection and marker metadata for each tab")
        identities[sheet_id] = (title, sheet, {"rowCount": rows, "columnCount": columns})

    target_extents = {}
    for target in approval.worksheet_targets:
        item = identities.get(target.worksheet_id)
        if item is None or item[0] != target.title:
            raise ValueError("workbook binding does not contain the exact approved worksheet pins")
        sheet = item[1]
        protections = sheet["protectedRanges"]
        if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
            raise ValueError("workbook binding records an unresolved worksheet guard")
        if any(item.get("description") not in {
            MANAGED_PREFIX + "COMPUTED", MANAGED_PREFIX + "BANK INPUT",
        } for item in protections):
            raise ValueError("workbook binding records unmanaged worksheet protections")
        _verify_marker({"developerMetadata": sheet["developerMetadata"]}, {
            "status": "COMPLETE", "epoch": approval.epoch, "fingerprint": approval.snapshot_fingerprint,
        })
        target_extents[target.worksheet_id] = item[2]
    return target_extents


def _archive_json(archive: zipfile.ZipFile, name: str, *, limit: int) -> tuple[Any, str]:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError("workbook backup archive is missing a required entry") from exc
    if info.file_size > limit:
        raise ValueError("workbook backup archive JSON entry exceeds its size bound")
    raw = archive.read(info)
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("non-finite number in workbook backup archive")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("workbook backup archive contains invalid JSON") from exc
    return value, hashlib.sha256(raw).hexdigest()


def _verify_workbook_archive(
    approval: CellRecoveryApproval,
    archive_path: Path,
    binding: dict,
) -> tuple[str, str, int]:
    policy = _policy_for_approval(approval)
    if archive_path.stat().st_size > _MAX_WORKBOOK_ARCHIVE_BYTES:
        raise ValueError("workbook backup archive exceeds its compressed size bound")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (not infos or len(infos) > _MAX_WORKBOOK_ARCHIVE_ENTRIES
                    or len(names) != len(set(names))
                    or any(item.is_dir() or item.flag_bits & 0x1 for item in infos)
                    or any(name.startswith("/") or "\\" in name or ".." in name.split("/") for name in names)):
                raise ValueError("workbook backup archive has unsafe or duplicate entries")
            if sum(item.file_size for item in infos) > _MAX_WORKBOOK_ARCHIVE_CONTENT_BYTES:
                raise ValueError("workbook backup archive exceeds its expanded size bound")

            archive_manifest, _ = _archive_json(archive, "manifest.json", limit=_MAX_JSON_BYTES)
            if (not isinstance(archive_manifest, dict)
                    or set(archive_manifest) != {
                    "schema_version", "purpose", "spreadsheet_id", "metadata_path",
                    "permissions_path", "sheets",
            } or type(archive_manifest["schema_version"]) is not int
                    or archive_manifest["schema_version"] != 1
                    or archive_manifest["purpose"] != _WORKBOOK_ARCHIVE_PURPOSE
                    or archive_manifest["spreadsheet_id"] != approval.spreadsheet_id
                    or archive_manifest["metadata_path"] != "metadata.json"
                    or archive_manifest["permissions_path"] != "permissions.json"):
                raise ValueError("workbook backup archive manifest differs from the approved format")

            metadata, metadata_sha = _archive_json(
                archive, archive_manifest["metadata_path"], limit=_MAX_JSON_BYTES,
            )
            permissions, permissions_sha = _archive_json(
                archive, archive_manifest["permissions_path"], limit=_MAX_JSON_BYTES,
            )
            if not isinstance(metadata, dict) or metadata.get("spreadsheetId") != approval.spreadsheet_id:
                raise ValueError("workbook backup archive metadata targets another spreadsheet")
            metadata_sheets = metadata.get("sheets")
            binding_sheets = binding.get("sheets")
            archive_sheets = archive_manifest.get("sheets")
            if (not isinstance(metadata_sheets, list)
                    or len(metadata_sheets) != policy.workbook_sheet_count
                    or not isinstance(binding_sheets, list)
                    or not isinstance(archive_sheets, list)
                    or len(archive_sheets) != policy.workbook_sheet_count):
                raise ValueError("workbook backup archive must contain metadata for all approved tabs")

            metadata_by_id = {}
            for item in metadata_sheets:
                properties = item.get("properties") if isinstance(item, dict) else None
                if not isinstance(properties, dict):
                    raise ValueError("workbook backup archive contains malformed sheet metadata")
                sheet_id, title = properties.get("sheetId"), properties.get("title")
                if type(sheet_id) is not int or not isinstance(title, str) or not title or sheet_id in metadata_by_id:
                    raise ValueError("workbook backup archive has duplicate or invalid sheet metadata")
                metadata_by_id[sheet_id] = item

            if not isinstance(permissions, list):
                raise ValueError("workbook backup archive permissions must be a list")
            expected_owners = {email.casefold() for email in approval.owner_emails}
            expected_finance = {
                email.casefold() for target in approval.worksheet_targets for email in target.finance_emails
            }
            expected_writers = {target.writer_email.casefold() for target in approval.worksheet_targets}
            actual_owners = set()
            actual_editors = set()
            actual_readers = set()
            for permission in permissions:
                if not isinstance(permission, dict):
                    raise ValueError("workbook backup archive contains malformed permission metadata")
                role, principal_type = permission.get("role"), permission.get("type")
                email = permission.get("emailAddress")
                if role not in {"owner", "writer", "reader", "commenter"}:
                    raise ValueError("workbook backup archive contains an unapproved permission role")
                if principal_type != "user" or not isinstance(email, str) or not email.strip():
                    raise ValueError("workbook backup archive contains an unpinned access principal")
                if role == "owner":
                    actual_owners.add(email.casefold())
                    actual_editors.add(email.casefold())
                elif role == "writer":
                    actual_editors.add(email.casefold())
                else:
                    actual_readers.add(email.casefold())
            if (actual_owners != expected_owners
                    or actual_editors != expected_owners | expected_finance | expected_writers):
                raise ValueError("workbook backup archive owners or editors differ from approved identities")
            if _identity_set_sha256(tuple(actual_readers)) != policy.reader_set_sha256:
                raise ValueError("workbook backup archive readers differ from approved read permissions")

            expected_entries = {"manifest.json", "metadata.json", "permissions.json"}
            page_count = 0
            archive_sheet_ids = set()
            binding_by_id = {}
            for sheet in binding_sheets:
                properties = sheet.get("properties", sheet)
                sheet_id = properties.get("sheetId", properties.get("sheet_id"))
                if type(sheet_id) is not int:
                    sheet_id = sheet.get("worksheet_id")
                binding_by_id[sheet_id] = sheet
            for archive_sheet in archive_sheets:
                if (not isinstance(archive_sheet, dict)
                        or set(archive_sheet) != {"worksheet_id", "title", "grid_pages"}):
                    raise ValueError("workbook backup archive has malformed sheet entries")
                sheet_id, title = archive_sheet["worksheet_id"], archive_sheet["title"]
                if (type(sheet_id) is not int or not isinstance(title, str)
                        or sheet_id in archive_sheet_ids or sheet_id not in binding_by_id):
                    raise ValueError("workbook backup archive sheet identity differs from the binding")
                archive_sheet_ids.add(sheet_id)
                bound_sheet = binding_by_id[sheet_id]
                bound_properties = bound_sheet.get("properties", bound_sheet)
                bound_title = bound_properties.get("title", bound_sheet.get("title"))
                if title != bound_title:
                    raise ValueError("workbook backup archive sheet title differs from the binding")
                source_sheet = metadata_by_id.get(sheet_id)
                source_properties = source_sheet.get("properties") if source_sheet else None
                if (not isinstance(source_properties, dict)
                        or source_properties.get("title") != title
                        or source_properties.get("gridProperties") != bound_sheet.get(
                            "gridProperties", bound_properties.get("gridProperties"),
                        )
                        or source_sheet.get("protectedRanges", []) != bound_sheet.get("protectedRanges")
                        or source_sheet.get("developerMetadata", []) != bound_sheet.get("developerMetadata")):
                    raise ValueError("workbook backup archive metadata differs from the full-grid binding")

                bound_pages = bound_sheet.get("grid_pages")
                archive_pages = archive_sheet["grid_pages"]
                if not isinstance(bound_pages, list) or not isinstance(archive_pages, list) or len(bound_pages) != len(archive_pages):
                    raise ValueError("workbook backup archive grid pages differ from the full-grid binding")
                for index, (bound_page, archive_page) in enumerate(zip(bound_pages, archive_pages, strict=True)):
                    entry_name = f"grid_pages/{sheet_id}/{index:04d}.json"
                    if (not isinstance(archive_page, dict)
                            or set(archive_page) != {"range", "path", "sha256"}
                            or archive_page["path"] != entry_name
                            or archive_page["range"] != bound_page.get("range")
                            or archive_page["sha256"] != bound_page.get("sha256")):
                        raise ValueError("workbook backup archive page identity differs from its binding")
                    try:
                        info = archive.getinfo(entry_name)
                    except KeyError as exc:
                        raise ValueError("workbook backup archive is missing a grid page") from exc
                    if info.file_size > _MAX_WORKBOOK_GRID_PAGE_BYTES:
                        raise ValueError("workbook backup archive grid page exceeds its size bound")
                    page_bytes = archive.read(info)
                    if hashlib.sha256(page_bytes).hexdigest() != archive_page["sha256"]:
                        raise ValueError("workbook backup archive grid-page checksum mismatch")
                    try:
                        page = json.loads(
                            page_bytes, object_pairs_hook=_unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                ValueError("non-finite number in workbook grid page")),
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError("workbook backup archive grid page is invalid JSON") from exc
                    response_sheets = page.get("response", {}).get("sheets") if isinstance(page, dict) else None
                    if (not isinstance(page, dict) or set(page) != {"range", "response"}
                            or page["range"] != archive_page["range"]
                            or not isinstance(page.get("response"), dict)
                            or page["response"].get("spreadsheetId") != approval.spreadsheet_id
                            or not isinstance(response_sheets, list) or len(response_sheets) != 1
                            or not isinstance(response_sheets[0], dict)
                            or response_sheets[0].get("properties", {}).get("sheetId") != sheet_id
                            or response_sheets[0].get("properties", {}).get("title") != title):
                        raise ValueError("workbook backup archive grid page content has the wrong identity")
                    match = re.fullmatch(
                        r"\$?([A-Z]+)\$?([1-9][0-9]*):\$?([A-Z]+)\$?([1-9][0-9]*)",
                        archive_page["range"].rsplit("!", 1)[1],
                    )
                    if match is None:
                        raise ValueError("workbook backup archive grid page has an invalid extent")
                    first_column, first_row, last_column, last_row = match.groups()
                    first_row, last_row = int(first_row), int(last_row)
                    expected_rows = last_row - first_row + 1
                    expected_columns = _column_number(last_column) - _column_number(first_column) + 1
                    grid_data = response_sheets[0].get("data")
                    if (not isinstance(grid_data, list) or len(grid_data) != 1
                            or not isinstance(grid_data[0], dict)
                            or grid_data[0].get("startRow", 0) != first_row - 1
                            or grid_data[0].get("startColumn", 0) != _column_number(first_column) - 1):
                        raise ValueError("workbook backup archive grid page omits its requested grid data")
                    row_data = grid_data[0].get("rowData")
                    if (row_data is not None
                            and (not isinstance(row_data, list) or len(row_data) > expected_rows)):
                        raise ValueError("workbook backup archive grid page exceeds requested rows")
                    if any(
                        not isinstance(row, dict)
                        or not isinstance(row.get("values", []), list)
                        or len(row.get("values", [])) > expected_columns
                        or any(not isinstance(cell, dict) for cell in row.get("values", []))
                        for row in (row_data or [])
                    ):
                        raise ValueError("workbook backup archive grid page has malformed cell data")
                    expected_entries.add(entry_name)
                    page_count += 1

            if archive_sheet_ids != set(binding_by_id) or set(names) != expected_entries:
                raise ValueError("workbook backup archive entries do not cover the exact approved workbook")
            return metadata_sha, permissions_sha, page_count
    except zipfile.BadZipFile as exc:
        raise ValueError("workbook backup must be a valid content-verified ZIP archive") from exc


def _verify_execution_scope(
    scope: dict,
    approval: CellRecoveryApproval,
) -> None:
    policy = _policy_for_approval(approval)
    variables = scope.get("variables") if isinstance(scope.get("variables"), dict) else {}
    inputs = scope.get("inputs") if isinstance(scope.get("inputs"), dict) else {}
    if (scope.get("local_mode") is not True
            or scope.get("namespace") != policy.finance_namespace
            or scope.get("flow_id") != policy.finance_flow_id
            or scope.get("flow_revision") != policy.failed_flow_revision
            or scope.get("execution_id") != approval.failed_execution_id
            or variables.get("cell_spreadsheet_id") != approval.spreadsheet_id
            or variables.get("cell_finance_emails") != sorted({
                email.casefold() for target in approval.worksheet_targets
                for email in target.finance_emails
            })
            or inputs != dict(policy.scope_inputs)):
        raise ValueError("failed-flow receipt differs from the approved local execution scope")


def _verify_backup_manifest(
    approval: CellRecoveryApproval,
    path,
) -> tuple[str, str, dict[int, dict[str, int]]]:
    policy = _policy_for_approval(approval)
    manifest_path = _external_path(path, must_exist=True)
    manifest, manifest_sha = _read_private_json(manifest_path, limit=8 * 1024 * 1024)
    if (type(manifest.get("schema_version")) is not int
            or (manifest.get("schema_version"), manifest.get("purpose")) != (1, _BACKUP_PURPOSE)):
        raise ValueError("unsupported external recovery backup manifest")
    expected_keys = {
        "schema_version", "purpose", "approval", "database_dump", "workbook_backup",
        "workbook_binding", "audit_receipts",
    }
    if set(manifest) != expected_keys or manifest["approval"] != approval_payload(approval):
        raise ValueError("backup manifest scope differs from the exact recovery approval")
    binding_path, binding_sha, binding = _checked_file(
        manifest["workbook_binding"], "workbook binding", parse_json=True,
    )
    grid_extents = _check_workbook_binding(binding, approval)
    db_path, db_sha, _ = _checked_file(
        manifest["database_dump"], "database dump", max_bytes=_MAX_DATABASE_ARCHIVE_BYTES,
    )
    descriptor = _open_private_file(db_path, os.O_RDONLY)
    try:
        database_archive_magic = os.read(descriptor, 5)
    finally:
        os.close(descriptor)
    if database_archive_magic != b"PGDMP":
        raise ValueError("database dump is not a PostgreSQL custom-format archive")
    workbook_path, workbook_sha, _ = _checked_file(
        manifest["workbook_backup"], "workbook backup", max_bytes=_MAX_WORKBOOK_ARCHIVE_BYTES,
    )
    workbook_metadata_sha, workbook_permissions_sha, workbook_page_count = _verify_workbook_archive(
        approval, workbook_path, binding,
    )
    evidence_paths = {manifest_path, db_path, workbook_path, binding_path}
    if len(evidence_paths) != 4:
        raise ValueError("backup manifest and backup artifacts must use exclusive file paths")
    receipts = manifest["audit_receipts"]
    if not isinstance(receipts, list) or not receipts:
        raise ValueError("backup manifest must include hashed audit receipts")
    receipt_paths = set()
    receipt_values = {}
    receipt_files = {}
    for item in receipts:
        parse_json = isinstance(item, dict) and str(item.get("path", "")).endswith(".json")
        receipt_name = Path(str(item.get("path", ""))).name if isinstance(item, dict) else ""
        receipt_limit = (
            _MAX_RESTORE_LIST_BYTES if receipt_name == "database-restore-list.txt"
            else _MAX_BACKUP_RECEIPT_BYTES
        )
        receipt_path, _, receipt = _checked_file(
            item, "backup audit receipt", parse_json=parse_json, max_bytes=receipt_limit,
        )
        if receipt_path in evidence_paths or receipt_path in receipt_paths:
            raise ValueError("backup manifest repeats an audit receipt")
        receipt_paths.add(receipt_path)
        receipt_files[receipt_path.name] = receipt_path
        if parse_json:
            receipt_values[receipt_path.name] = receipt

    required_receipts = {
        "database-backup-receipt.json", "workbook-binding-refresh.json",
        "recovery-pins.json", "scope.json",
    }
    if (not required_receipts <= set(receipt_values)
            or "database-restore-list.txt" not in receipt_files):
        raise ValueError("backup evidence is missing validated database/workbook/scope receipts")
    database_receipt = receipt_values["database-backup-receipt.json"]
    listing_path = receipt_files["database-restore-list.txt"]
    listing_bytes, listing_sha = _read_private_bytes(listing_path, limit=_MAX_RESTORE_LIST_BYTES)
    try:
        listing_text = listing_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("pg_restore listing receipt must be UTF-8 text") from exc
    non_comment_lines = [line for line in listing_text.splitlines() if line and not line.startswith(";")]
    toc_record = re.compile(r"^\s*[0-9]+;\s+[0-9]+\s+[0-9]+\s+\S+")
    toc_entries = [line for line in non_comment_lines if toc_record.match(line)]
    toc_header = re.search(r"(?m)^;\s*TOC Entries:\s*([0-9]+)\s*$", listing_text)
    toc_count = int(toc_header.group(1)) if toc_header else None
    listed_tables = {
        match.group(1)
        for line in toc_entries
        if (match := re.search(r"\bTABLE\s+public\s+([a-z0-9_]+)\b", line))
    }
    listed_table_data = {
        match.group(1)
        for line in toc_entries
        if (match := re.search(r"\bTABLE DATA\s+public\s+([a-z0-9_]+)\b", line))
    }
    listed_views = {
        match.group(1)
        for line in toc_entries
        if (match := re.search(r"\bVIEW\s+public\s+([a-z0-9_]+)\b", line))
    }
    if (database_receipt.get("archive_verified") is not True
            or database_receipt.get("sha256") != db_sha
            or database_receipt.get("bytes") != db_path.stat().st_size
            or type(database_receipt.get("archive_entries")) is not int
            or database_receipt["archive_entries"] != policy.database_toc_count
            or type(database_receipt.get("listed_entries")) is not int
            or database_receipt["listed_entries"] != policy.database_listed_entry_count
            or database_receipt.get("verification_tool") != "pg_restore --list"
            or type(database_receipt.get("verification_exit_code")) is not int
            or database_receipt["verification_exit_code"] != 0
            or database_receipt.get("archive_listing_sha256") != listing_sha
            or not re.search(r"(?m)^;\s*dbname:\s*finpay\s*$", listing_text)
            or toc_count != database_receipt["archive_entries"]
            or len(toc_entries) != database_receipt["listed_entries"]
            or len(non_comment_lines) != len(toc_entries)
            or not set(_REQUIRED_CELL_BACKUP_TABLES) <= listed_tables
            or not set(_REQUIRED_CELL_BACKUP_TABLES) <= listed_table_data
            or not set(_REQUIRED_CELL_BACKUP_VIEWS) <= listed_views
            or database_receipt.get("database_target") != approval_payload(approval)["database_target"]):
        raise ValueError("database dump lacks a complete matching pg_restore archive listing")
    workbook_receipt = receipt_values["workbook-binding-refresh.json"]
    if (workbook_receipt.get("source_workbook_backup_sha256") != workbook_sha
            or workbook_receipt.get("binding_tabs") != policy.workbook_sheet_count
            or workbook_receipt.get("grid_property_count") != policy.workbook_sheet_count
            or workbook_receipt.get("binding_sha256") != binding_sha
            or workbook_receipt.get("metadata_sha256") != workbook_metadata_sha
            or workbook_receipt.get("permissions_sha256") != workbook_permissions_sha
            or workbook_receipt.get("grid_page_count") != workbook_page_count):
        raise ValueError("workbook backup lacks a matching full-grid binding receipt")
    pins = receipt_values["recovery-pins.json"]
    if pins.get("approval") != approval_payload(approval):
        raise ValueError("recovery-pin receipt differs from the exact approved incident")
    _verify_execution_scope(receipt_values["scope.json"], approval)
    return manifest_sha, fingerprint({
        "database_dump": db_sha,
        "workbook_backup": workbook_sha,
        "workbook_binding": binding_sha,
    }), grid_extents


def _parse_evidence_time(value, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp must be an explicit ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _verify_quiescence(
    approval: CellRecoveryApproval,
    path,
) -> str:
    policy = _policy_for_approval(approval)
    manifest_path = _external_path(path, must_exist=True)
    manifest, manifest_sha = _read_private_json(manifest_path, limit=8 * 1024 * 1024)
    required = {
        "schema_version", "purpose", "approval_sha256", "observed_at",
        "stopped_writers", "no_active_job", "running_containers",
    }
    if (set(manifest) != required or type(manifest["schema_version"]) is not int
            or (manifest["schema_version"], manifest["purpose"]) != (1, _QUIESCENCE_PURPOSE)):
        raise ValueError("unsupported writer-quiescence evidence manifest")
    if manifest["approval_sha256"] != approval_fingerprint(approval):
        raise ValueError("quiescence evidence is not bound to the exact recovery approval")
    now = _now_utc()
    manifest_time = _parse_evidence_time(manifest["observed_at"], "quiescence manifest")
    if manifest_time > now + timedelta(seconds=30) or now - manifest_time > _MAX_QUIESCENCE_AGE:
        raise ValueError("quiescence evidence is stale or future-dated")
    running = manifest["running_containers"]
    if (not isinstance(running, list) or len(running) != len(set(running))
            or set(running) != policy.running_local_containers):
        raise ValueError("the local container inventory is not quiescent")
    writers = manifest["stopped_writers"]
    if (not isinstance(writers, list)
            or {item.get("name") for item in writers if isinstance(item, dict)} != policy.stopped_writers
            or len(writers) != len(policy.stopped_writers)):
        raise ValueError("the approved local writer set must be explicitly stopped")
    names = set()
    proof_hashes = []
    for item in writers:
        if not isinstance(item, dict) or set(item) != {"name", "receipt"}:
            raise ValueError("each stopped writer needs a named evidence receipt")
        name = item["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("stopped-writer names must be unique and explicit")
        names.add(name)
        _, digest, receipt = _checked_file(item["receipt"], "stopped-writer receipt", parse_json=True)
        if (type(receipt.get("schema_version")) is not int
                or (receipt.get("schema_version"), receipt.get("kind"), receipt.get("writer"))
                != (1, "writer-stop", name)):
            raise ValueError("stopped-writer receipt identity mismatch")
        if not isinstance(receipt.get("container_id"), str) or not receipt["container_id"].strip():
            raise ValueError("writer-stop receipt must identify the stopped container")
        observed = _parse_evidence_time(receipt.get("observed_at"), "stopped-writer")
        if observed > manifest_time or now - observed > _MAX_QUIESCENCE_AGE:
            raise ValueError("writer-stop evidence is stale or out of order")
        if receipt.get("state_after") not in {"exited", "stopped", "not_found"}:
            raise ValueError("writer-stop receipt does not prove the writer stopped")
        if receipt.get("state_before") not in {"running", "exited", "stopped", "not_found"}:
            raise ValueError("writer-stop receipt has an invalid before state")
        proof_hashes.append(digest)

    no_active = manifest["no_active_job"]
    if not isinstance(no_active, dict) or set(no_active) != {"receipt"}:
        raise ValueError("explicit no-active-job receipt is required")
    _, digest, receipt = _checked_file(no_active["receipt"], "no-active-job receipt", parse_json=True)
    if (type(receipt.get("schema_version")) is not int
            or (receipt.get("schema_version"), receipt.get("kind")) != (1, "kestra-active-executions")):
        raise ValueError("active-job receipt does not identify a Kestra execution list")
    observed = _parse_evidence_time(receipt.get("observed_at"), "active-job list")
    if observed > manifest_time or now - observed > _MAX_QUIESCENCE_AGE:
        raise ValueError("active-job evidence is stale or out of order")
    active = receipt.get("active_executions")
    if not isinstance(active, list) or active:
        raise ValueError("active-job receipt must prove that no execution was active")
    if (receipt.get("kestra_container_state") != "exited"
            or receipt.get("running_containers") != sorted(policy.running_local_containers)):
        raise ValueError("Kestra stopped-state and local container inventory do not match the approved quiescence")
    proof_hashes.append(digest)
    return fingerprint({"manifest_sha256": manifest_sha, "receipt_sha256": sorted(proof_hashes)})


def _database_identity(connection, approval: CellRecoveryApproval) -> None:
    connection.execute(f'SET search_path TO "{approval.database_target.schema}"')
    row = connection.execute("SELECT current_database(), current_user, current_schema()").fetchone()
    expected = (approval.database_target.database, approval.database_target.user, approval.database_target.schema)
    if tuple(row or ()) != expected:
        raise ValueError("connected database/user/schema differs from the approved target")
    info = getattr(connection, "info", None)
    if info is not None:
        if getattr(info, "dbname", approval.database_target.database) != approval.database_target.database:
            raise ValueError("connected database identity differs from the approved target")
        if getattr(info, "user", approval.database_target.user) != approval.database_target.user:
            raise ValueError("connected database role differs from the approved target")
        host = getattr(info, "host", None)
        if host and host != approval.database_target.host:
            raise ValueError("connected database host differs from the approved target")
        port = getattr(info, "port", None)
        if port is not None and str(port) != str(approval.database_target.port):
            raise ValueError("connected database port differs from the approved target")


def _table(approval: CellRecoveryApproval, name: str) -> str:
    # The schema is identifier-validated by CellDatabaseTarget and restricted to public.
    return f'"{approval.database_target.schema}"."{name}"'


def _check_database_state(connection, approval: CellRecoveryApproval):
    target_id, cluster_id, epoch = approval.spreadsheet_id, approval.cluster_id, approval.epoch
    publication_table = _table(approval, "finpay_cell_publications_v1")
    operation_table = _table(approval, "finpay_cell_target_operations_v1")

    operations = connection.execute(
        f"SELECT target_id,cluster_id,operation_id,status,error_class,started_at FROM {operation_table} "
        "WHERE (target_id=%s OR cluster_id=%s) AND status <> 'IDLE' FOR UPDATE",
        (target_id, cluster_id),
    ).fetchall()
    if len(operations) != 1:
        raise RuntimeError("recovery requires exactly one unresolved target operation")
    operation = operations[0]
    if (operation[0], operation[1], operation[2], operation[3], operation[4], operation[5]) != (
        target_id, cluster_id, approval.operation_id, "UNKNOWN", approval.expected_error,
        approval.operation_started_at,
    ):
        raise ValueError("target operation row differs from exact approved pins")

    unresolved = connection.execute(
        f"SELECT cluster_id,target_id,epoch,status FROM {publication_table} "
        "WHERE (target_id=%s OR cluster_id=%s) AND status NOT IN ('COMPLETE','SUPERSEDED','DB_ONLY') FOR UPDATE",
        (target_id, cluster_id),
    ).fetchall()
    if len(unresolved) != 1 or tuple(unresolved[0]) != (cluster_id, target_id, epoch, "UNKNOWN"):
        raise RuntimeError("another non-idle, IN_FLIGHT, or UNKNOWN publication blocks recovery")

    rows = connection.execute(
        f"SELECT epoch,range_start,range_end,snapshot_fingerprint,snapshot,status,confirmed_chunks,"
        f"error_class,created_at,confirmed_at FROM {publication_table} "
        "WHERE cluster_id=%s AND target_id=%s ORDER BY epoch DESC FOR UPDATE",
        (cluster_id, target_id),
    ).fetchall()
    if not rows or rows[0][0] != epoch:
        raise ValueError("approved UNKNOWN publication is not the latest target history")
    row = rows[0]
    start, end = row[1].isoformat(), row[2].isoformat()
    if (start, end, row[3], row[5], row[7]) != (
        approval.range_start, approval.range_end, approval.snapshot_fingerprint, "UNKNOWN", approval.expected_error,
    ):
        raise ValueError("publication row differs from exact approved pins")
    if not isinstance(row[4], dict) or not isinstance(row[6], list):
        raise ValueError("publication snapshot/history fields are malformed")
    if any(not isinstance(item, str) for item in row[6]):
        raise ValueError("publication chunk history is malformed")
    actual_fingerprint = fingerprint({
        "cluster_id": cluster_id,
        "target_id": target_id,
        "range_start": start,
        "range_end": end,
        "snapshot": row[4],
    })
    if actual_fingerprint != approval.snapshot_fingerprint:
        raise ValueError("saved publication snapshot fingerprint mismatch")
    return row


def _dates(start: str, end: str) -> list[date]:
    first, last = business_date(start), business_date(end)
    return [date.fromordinal(value) for value in range(first.toordinal(), last.toordinal() + 1)]


def _verify_snapshot_and_sheets(approval: CellRecoveryApproval, publication_row, adapters,
                               backup_grid_extents: dict[int, dict[str, int]]) -> dict[str, Any]:
    snapshot = publication_row[4]
    if (snapshot.get("schema_version"), snapshot.get("spreadsheet_id")) != (2, approval.spreadsheet_id):
        raise ValueError("saved snapshot schema/workbook differs from approval")
    worksheets = snapshot.get("worksheets")
    if not isinstance(worksheets, dict) or set(worksheets) != set(ROLES):
        raise ValueError("saved snapshot must contain exactly the four publication roles")
    targets = {target.role: target for target in approval.worksheet_targets}
    marker = {"status": "COMPLETE", "epoch": approval.epoch, "fingerprint": approval.snapshot_fingerprint}
    for role in ROLES:
        saved, target, adapter = worksheets[role], targets[role], adapters[role]
        if not isinstance(saved, dict) or not isinstance(saved.get("matrix"), dict):
            raise ValueError("saved worksheet matrix is missing")
        if saved["matrix"].get("style_contract", {}).get("role") != role:
            raise ValueError("saved worksheet style contract does not match its role")
        if saved["matrix"].get("values") != saved.get("values"):
            raise ValueError("saved worksheet values differ from its style matrix")
        if (saved.get("worksheet_id"), saved.get("title")) != (target.worksheet_id, target.title):
            raise ValueError("saved worksheet ID/title differs from approval")
        if (adapter.spreadsheet_id, adapter.worksheet_id) != (approval.spreadsheet_id, target.worksheet_id):
            raise ValueError("opened read-only worksheet differs from approved IDs")
        metadata = adapter.metadata()
        properties = metadata.get("properties", {})
        if (properties.get("sheetId"), properties.get("title")) != (target.worksheet_id, target.title):
            raise ValueError("live worksheet metadata differs from approved ID/title")
        pinned_grid = backup_grid_extents.get(target.worksheet_id)
        live_grid = properties.get("gridProperties")
        if not isinstance(pinned_grid, dict) or not isinstance(live_grid, dict):
            raise ValueError("live worksheet grid extent is missing from backup/live metadata")
        live_rows, live_columns = live_grid.get("rowCount"), live_grid.get("columnCount")
        pinned_dimensions = (pinned_grid["rowCount"], pinned_grid["columnCount"])
        if (type(live_rows) is not int or type(live_columns) is not int
                or (live_rows, live_columns) != pinned_dimensions):
            raise ValueError("live worksheet grid extent differs from the pinned backup")
        rows, columns = pinned_grid["rowCount"], pinned_grid["columnCount"]
        values = saved.get("values")
        if (not isinstance(values, list) or not values or not isinstance(values[0], list) or not values[0]
                or rows < len(values) or columns < len(values[0])
                or any(not isinstance(row, list) or len(row) != len(values[0]) for row in values)):
            raise ValueError("pinned worksheet grid cannot contain the complete committed rectangle")
        # The helpers use this prior shape only to extend their read window.
        previous_extent = {"values": [("",) * columns] * rows}
        owners = metadata.get("implicitOwnerEmails", [])
        if {str(item).casefold() for item in owners} != {item.casefold() for item in approval.owner_emails}:
            raise ValueError("live workbook owners differ from approved owner pins")
        protections = metadata.get("protectedRanges", [])
        if any(item.get("description", "").startswith(GUARD_PREFIX) for item in protections):
            raise RuntimeError("temporary worksheet guard remains; refusing reconciliation")
        expected_protections = 1 + len(saved["matrix"].get("editable_inputs", []))
        expected_descriptions = {MANAGED_PREFIX + "COMPUTED", MANAGED_PREFIX + "BANK INPUT"}
        if (len(protections) != expected_protections
                or any(item.get("description") not in expected_descriptions for item in protections)):
            raise ValueError("worksheet protections contain missing, extra, or unmanaged entries")
        _verify_values(adapter, saved, previous_extent)
        _verify_style(adapter, saved["matrix"], previous_extent)
        _verify_marker(metadata, marker)
        _verify_final_protections(target.sheet_target(), saved["matrix"], metadata)
    return snapshot


def _verify_source_coverage(connection, approval: CellRecoveryApproval, snapshot) -> dict[str, str]:
    account = approval.cluster_id + "_B"
    cells = OBSERVED_CELL_ROSTER.get(account)
    if not cells:
        raise ValueError("no approved source roster exists for the pinned cluster")
    table = _table(approval, "finpay_cell_current_v1")
    rows = connection.execute(
        f"SELECT account,cell_code,report_date,calculation_id FROM {table} "
        "WHERE cluster_id=%s AND report_date BETWEEN %s AND %s ORDER BY report_date,cell_code",
        (approval.cluster_id, business_date(approval.range_start), business_date(approval.range_end)),
    ).fetchall()
    expected_days = [item.isoformat() for item in _dates(approval.range_start, approval.range_end)]
    selected: dict[str, dict[str, str]] = {day: {} for day in expected_days}
    for account_value, cell_code, report_date, calculation_id in rows:
        day = report_date.isoformat()
        if account_value != account or day not in selected or cell_code not in cells:
            raise ValueError("current source rows contain an unexpected account/date/cell")
        if cell_code in selected[day] or not isinstance(calculation_id, str) or not calculation_id:
            raise ValueError("current source rows contain duplicate or missing calculations")
        selected[day][cell_code] = calculation_id
    if any(set(selected[day]) != set(cells) for day in expected_days):
        raise ValueError("current source roster coverage is incomplete through the approved end date")
    calculated = {day: fingerprint(sorted(selected[day].values())) for day in expected_days}
    pinned = snapshot.get("source_calculations")
    if not isinstance(pinned, dict) or set(pinned) != set(expected_days) or pinned != calculated:
        raise ValueError("current calculation fingerprints differ from the saved snapshot")
    return calculated


def _new_receipt(path) -> int:
    path = _external_path(path, must_exist=False)
    _check_private_directory(path.parent.resolve(strict=True))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("audit receipt must be a private exclusive file")
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _append_receipt(descriptor: int, event: dict[str, Any]) -> None:
    data = _canonical_json(event) + b"\n"
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written == 0:
            raise OSError("audit receipt write made no progress")
        offset += written
    os.fsync(descriptor)


def _release_locks(connection, target_id: str, cluster_id: str, *, cluster_locked: bool, target_locked: bool) -> None:
    if getattr(connection, "closed", False):
        return
    try:
        if cluster_locked:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-cluster-v1", cluster_id),
            )
    finally:
        if target_locked and not getattr(connection, "closed", False):
            connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-publisher-v1", target_id),
            )


def reconcile_unknown_publication(
    *,
    approval: CellRecoveryApproval,
    connection,
    adapters: dict[str, ExistingSheetAdapter],
    backup_manifest_path,
    quiescence_manifest_path,
    audit_receipt_path,
) -> dict[str, Any]:
    """Read back and atomically confirm one approved UNKNOWN publication.

    All external evidence and Sheet reads are validated before any database
    mutation. The caller owns the connection and actual read-only Google client;
    this function never invokes an adapter's write-capable methods. Any pin,
    evidence, lock, readback, or compare-and-swap mismatch aborts without a
    durable database change. The audit receipt path is fixed for this incident,
    so changing a caller-supplied path cannot bypass the one-shot barrier. The
    selected policy is resolved only from the fixed profile map.
    """
    if not isinstance(approval, CellRecoveryApproval):
        raise TypeError("CellRecoveryApproval is required")
    policy = _policy_for_profile(approval.profile)
    _validate_approval(approval)
    if set(adapters) != set(ROLES) or any(type(item) is not ExistingSheetAdapter for item in adapters.values()):
        raise ValueError("exactly four ExistingSheetAdapter read-only worksheet adapters are required")
    if getattr(connection, "autocommit", None) is not True:
        raise ValueError("recovery requires an autocommit psycopg connection for session advisory locks")
    manifest_sha, backup_sha, backup_grid_extents = _verify_backup_manifest(
        approval, backup_manifest_path,
    )
    quiescence_sha = _verify_quiescence(approval, quiescence_manifest_path)
    receipt_path = _external_path(audit_receipt_path, must_exist=False)
    approved_receipt_path = _external_path(policy.audit_receipt_path, must_exist=False)
    if receipt_path != approved_receipt_path:
        raise ValueError("audit receipt path is fixed for the exact approved recovery incident")
    _check_private_directory(receipt_path.parent.resolve(strict=True))
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError("audit receipt path must be new and exclusive")

    proof_sha = fingerprint({
        "approval_sha256": approval_fingerprint(approval),
        "backup_manifest_sha256": manifest_sha,
        "backup_files_sha256": backup_sha,
        "quiescence_sha256": quiescence_sha,
        "receipt_path": str(receipt_path),
    })
    target_locked = cluster_locked = False
    receipt_fd = None
    cas_committed = False
    cas_ready_to_commit = False
    try:
        _database_identity(connection, approval)
        locked = connection.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            ("finpay-cell-publisher-v1", approval.spreadsheet_id),
        ).fetchone()
        if not locked or locked[0] is not True:
            raise RuntimeError("cell publication target is already locked")
        target_locked = True
        locked = connection.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            ("finpay-cell-cluster-v1", approval.cluster_id),
        ).fetchone()
        if not locked or locked[0] is not True:
            raise RuntimeError("cell publication cluster is already locked")
        cluster_locked = True

        with connection.transaction():
            publication_row = _check_database_state(connection, approval)
            if f"operator-recovery:{proof_sha}" in publication_row[6]:
                raise RuntimeError("this exact recovery proof is already recorded; refusing a duplicate")
            snapshot = _verify_snapshot_and_sheets(
                approval, publication_row, adapters, backup_grid_extents,
            )
            calculations = _verify_source_coverage(connection, approval, snapshot)
            day_values = _dates(approval.range_start, approval.range_end)
            expected_dates = [item.isoformat() for item in day_values]
            dates_table = _table(approval, "finpay_cell_publication_dates_v1")
            publication_dates = connection.execute(
                f"SELECT report_date,status FROM {dates_table} WHERE cluster_id=%s AND target_id=%s AND epoch=%s "
                "ORDER BY report_date FOR UPDATE",
                (approval.cluster_id, approval.spreadsheet_id, approval.epoch),
            ).fetchall()
            if [(day.isoformat(), status) for day, status in publication_dates] != [
                (day, "PENDING") for day in expected_dates
            ]:
                raise ValueError("publication-date history is not the exact pending approved range")

            receipt_fd = _new_receipt(receipt_path)
            _append_receipt(receipt_fd, {
                "event": "intent",
                "approval_sha256": approval_fingerprint(approval),
                "audit_proof_sha256": proof_sha,
                "manifest_sha256": manifest_sha,
                "backup_files_sha256": backup_sha,
                "quiescence_sha256": quiescence_sha,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            })

            publication_table = _table(approval, "finpay_cell_publications_v1")
            updated = connection.execute(
                f"UPDATE {publication_table} SET status='COMPLETE',confirmed_chunks=confirmed_chunks || %s::jsonb,"
                "confirmed_at=CURRENT_TIMESTAMP WHERE cluster_id=%s AND target_id=%s AND epoch=%s "
                "AND status='UNKNOWN' AND range_start=%s AND range_end=%s AND snapshot_fingerprint=%s "
                "AND error_class=%s AND snapshot=%s::jsonb AND confirmed_chunks=%s::jsonb "
                "AND created_at=%s AND confirmed_at IS NOT DISTINCT FROM %s RETURNING epoch",
                (json.dumps([f"operator-recovery:{proof_sha}"]), approval.cluster_id, approval.spreadsheet_id,
                 approval.epoch, business_date(approval.range_start), business_date(approval.range_end),
                 approval.snapshot_fingerprint, approval.expected_error, json.dumps(publication_row[4], sort_keys=True),
                 json.dumps(publication_row[6]), publication_row[8], publication_row[9]),
            ).fetchone()
            if not updated or updated[0] != approval.epoch:
                raise RuntimeError("publication compare-and-swap did not update exactly the approved row")

            date_rows = connection.execute(
                f"UPDATE {dates_table} SET status='COMPLETE' WHERE cluster_id=%s AND target_id=%s AND epoch=%s "
                "AND report_date BETWEEN %s AND %s AND status='PENDING' RETURNING report_date",
                (approval.cluster_id, approval.spreadsheet_id, approval.epoch,
                 business_date(approval.range_start), business_date(approval.range_end)),
            ).fetchall()
            if sorted(day.isoformat() for (day,) in date_rows) != expected_dates:
                raise RuntimeError("publication-date compare-and-swap count differs from the exact approved range")

            operation_table = _table(approval, "finpay_cell_target_operations_v1")
            operation = connection.execute(
                f"UPDATE {operation_table} SET status='IDLE' WHERE target_id=%s AND cluster_id=%s "
                "AND operation_id=%s AND status='UNKNOWN' AND error_class=%s AND started_at=%s "
                "RETURNING operation_id",
                (approval.spreadsheet_id, approval.cluster_id, approval.operation_id, approval.expected_error,
                 approval.operation_started_at),
            ).fetchone()
            if not operation or operation[0] != approval.operation_id:
                raise RuntimeError("target-operation compare-and-swap did not update the exact approved row")

            dirty_table = _table(approval, "finpay_cell_dirty_dates_v1")
            for report_date, digest in calculations.items():
                connection.execute(
                    f"DELETE FROM {dirty_table} WHERE cluster_id=%s AND report_date=%s "
                    "AND calculation_fingerprint=%s",
                    (approval.cluster_id, business_date(report_date), digest),
                )
            cas_ready_to_commit = True
        cas_committed = True
        try:
            _append_receipt(receipt_fd, {
                "event": "completed",
                "audit_proof_sha256": proof_sha,
                "epoch": approval.epoch,
                "dates_completed": len(_dates(approval.range_start, approval.range_end)),
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            })
            _, receipt_sha = _read_private_bytes(receipt_path, limit=8 * 1024 * 1024)
        except Exception as exc:
            raise RuntimeError(
                "database reconciliation committed but completed receipt hashing/fsync failed; do not rerun"
            ) from exc
        return {
            "status": "COMPLETE",
            "epoch": approval.epoch,
            "dates_completed": len(_dates(approval.range_start, approval.range_end)),
            "audit_proof_sha256": proof_sha,
            "audit_receipt_sha256": receipt_sha,
        }
    except Exception:
        if receipt_fd is not None and not cas_committed:
            try:
                _append_receipt(receipt_fd, {
                    "event": "commit_unknown" if cas_ready_to_commit else "rolled_back",
                    "audit_proof_sha256": proof_sha,
                    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                })
            except OSError:
                pass
        raise
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        _release_locks(
            connection,
            approval.spreadsheet_id,
            approval.cluster_id,
            cluster_locked=cluster_locked,
            target_locked=target_locked,
        )
