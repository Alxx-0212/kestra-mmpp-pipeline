"""Offline fail-closed tests for the one approved cell-publication recovery path."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from finpay_pipeline.cell_contracts import OBSERVED_CELL_ROSTER, fingerprint
from finpay_pipeline.cell_database import CellDatabaseTarget
from finpay_pipeline.cell_publication import (
    MANAGED_PREFIX,
    PUBLICATION_METADATA_KEY,
    CellWorksheetTarget,
    ExistingSheetAdapter,
)
from finpay_pipeline.cell_publication_v2 import ROLES, cell_worksheet_title
from finpay_pipeline.cell_recovery import (
    CellRecoveryApproval,
    CellRecoveryProfile,
    _check_workbook_binding,
    _identity_set_sha256,
    _utc_timestamp,
    approval_fingerprint,
    approval_payload,
    reconcile_unknown_publication,
)
import finpay_pipeline.cell_recovery as recovery_module


class Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    """Small SQL protocol fake with transaction rollback and session locks."""

    def __init__(self, state):
        self.state = state
        self.autocommit = True
        self.closed = False
        self.database_identity = ("finpay", "finpay", "public")
        self.lock_results = {
            "finpay-cell-publisher-v1": True,
            "finpay-cell-cluster-v1": True,
        }
        self.locks = set()
        self.fail_at = None
        self.fail_commit = False
        self.fail_commit_rolls_back = False
        self.commits = 0
        self.rollbacks = 0
        self.calls = []

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.state)
        try:
            yield self
        except Exception:
            self.state.clear()
            self.state.update(before)
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
            if self.fail_commit:
                if self.fail_commit_rolls_back:
                    self.state.clear()
                    self.state.update(before)
                    self.rollbacks += 1
                raise RuntimeError("synthetic commit acknowledgement lost")

    def execute(self, query, params=()):
        text = str(query)
        self.calls.append((text, params))
        if text.startswith('SET search_path TO "'):
            return Result()
        if "current_database()" in text:
            return Result(self.database_identity)
        if "pg_try_advisory_lock" in text:
            namespace = params[0]
            allowed = self.lock_results[namespace]
            if allowed:
                self.locks.add((namespace, params[1]))
            return Result((allowed,))
        if "pg_advisory_unlock" in text:
            self.locks.discard((params[0], params[1]))
            return Result((True,))
        if text.startswith("SELECT target_id,cluster_id,operation_id,status,error_class,started_at"):
            rows = [
                (item["target_id"], item["cluster_id"], item["operation_id"], item["status"],
                 item["error_class"], item["started_at"])
                for item in self.state["operations"]
                if (item["target_id"] == params[0] or item["cluster_id"] == params[1])
                and item["status"] != "IDLE"
            ]
            return Result(rows=rows)
        if text.startswith("SELECT cluster_id,target_id,epoch,status"):
            rows = [
                (item["cluster_id"], item["target_id"], item["epoch"], item["status"])
                for item in self.state["publications"]
                if (item["target_id"] == params[0] or item["cluster_id"] == params[1])
                and item["status"] not in {"COMPLETE", "SUPERSEDED", "DB_ONLY"}
            ]
            return Result(rows=rows)
        if text.startswith("SELECT epoch,range_start,range_end,snapshot_fingerprint"):
            rows = [
                (item["epoch"], item["range_start"], item["range_end"], item["snapshot_fingerprint"],
                 copy.deepcopy(item["snapshot"]), item["status"], copy.deepcopy(item["confirmed_chunks"]),
                 item["error_class"], item["created_at"], item["confirmed_at"])
                for item in sorted(
                    self.state["publications"], key=lambda item: item["epoch"], reverse=True,
                )
                if item["cluster_id"] == params[0] and item["target_id"] == params[1]
            ]
            return Result(rows=rows)
        if text.startswith("SELECT account,cell_code,report_date,calculation_id"):
            return Result(rows=self.state["current"])
        if text.startswith("SELECT report_date,status FROM"):
            dates = [
                (item["report_date"], item["status"])
                for item in self.state["dates"]
                if (item["cluster_id"], item["target_id"], item["epoch"])
                == (params[0], params[1], params[2])
            ]
            return Result(rows=sorted(dates))
        if text.startswith("UPDATE") and "finpay_cell_publications_v1" in text:
            if self.fail_at == "publication":
                return Result()
            item = self.state["publications"][0]
            if item["status"] != "UNKNOWN" or item["epoch"] != params[3]:
                return Result()
            item["status"] = "COMPLETE"
            item["confirmed_chunks"].extend(json.loads(params[0]))
            item["confirmed_at"] = datetime.now(timezone.utc)
            return Result((item["epoch"],))
        if text.startswith("UPDATE") and "finpay_cell_publication_dates_v1" in text:
            changed = []
            for item in self.state["dates"]:
                if ((item["cluster_id"], item["target_id"], item["epoch"])
                        == (params[0], params[1], params[2])
                        and params[3] <= item["report_date"] <= params[4]
                        and item["status"] == "PENDING"):
                    item["status"] = "COMPLETE"
                    changed.append((item["report_date"],))
            if self.fail_at == "dates":
                return Result(rows=changed[:-1])
            return Result(rows=changed)
        if text.startswith("UPDATE") and "finpay_cell_target_operations_v1" in text:
            if self.fail_at == "operation":
                return Result()
            item = next(
                (item for item in self.state["operations"] if item["target_id"] == params[0]), None,
            )
            if (not item or item["status"] != "UNKNOWN" or item["operation_id"] != params[2]
                    or item["started_at"] != params[4]):
                return Result()
            item["status"] = "IDLE"
            return Result((item["operation_id"],))
        if text.startswith("DELETE") and "finpay_cell_dirty_dates_v1" in text:
            key = (params[0], params[1])
            if self.state["dirty"].get(key) == params[2]:
                del self.state["dirty"][key]
            return Result()
        raise AssertionError(f"unexpected synthetic SQL: {text}")


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeGoogleClient:
    def __init__(self, spreadsheet_id, owners):
        self.spreadsheet_id = spreadsheet_id
        self.owners = owners
        self.methods = []

    def request(self, method, url, *, params):
        self.methods.append(method)
        if method != "get":
            raise AssertionError("recovery must use read-only Google requests")
        return FakeResponse({"id": self.spreadsheet_id, "owners": [{"emailAddress": item} for item in self.owners]})


class FakeWorksheet:
    def __init__(self, worksheet_id, title, values):
        self.id = worksheet_id
        self.title = title
        self.values = copy.deepcopy(values)
        self.get_calls = 0

    def get(self, _range, *, value_render_option):
        self.get_calls += 1
        if value_render_option != "FORMULA":
            raise AssertionError("value read must preserve formulas")
        return copy.deepcopy(self.values)


class FakeSpreadsheet:
    def __init__(self, spreadsheet_id, owners, sheets):
        self.id = spreadsheet_id
        self.client = FakeGoogleClient(spreadsheet_id, owners)
        self.sheets = sheets
        self.write_calls = 0

    def fetch_sheet_metadata(self):
        return {"sheets": copy.deepcopy(self.sheets)}

    def batch_update(self, _request):
        self.write_calls += 1
        raise AssertionError("recovery must never write to Google Sheets")


def _raw_approval(values):
    # Synthetic test pins exercise validation without mutating production profiles.
    approval = object.__new__(CellRecoveryApproval)
    for name, value in values.items():
        object.__setattr__(approval, name, value)
    return approval


@contextmanager
def _selected_test_policy(policy):
    policies = dict(recovery_module._RECOVERY_POLICIES)
    policies[policy.profile] = policy
    with patch.object(recovery_module, "_RECOVERY_POLICIES", MappingProxyType(policies)):
        yield


def _synthetic_approval(policy, values):
    approval = _raw_approval(values)
    with _selected_test_policy(policy):
        recovery_module._validate_approval(approval)
    return approval


def _approval_values(approval):
    return {item.name: getattr(approval, item.name) for item in fields(approval)}


class RecoveryFixture:
    def __init__(self, temp_dir: Path, profile=CellRecoveryProfile.EPOCH4):
        self.directory = temp_dir
        self.audit_receipt_path = self.directory / "recovery-receipt.jsonl"
        base_policy = recovery_module._policy_for_profile(profile)
        self.profile = profile
        self.start = date.fromisoformat(base_policy.report_range[0])
        self.end = date.fromisoformat(base_policy.report_range[1])
        self.epoch = base_policy.epoch
        self.quiescence_time = datetime.now(timezone.utc)
        self.cluster_id = base_policy.cluster_id
        self.account = self.cluster_id + "_B"
        self.spreadsheet_id = "runtime-workbook-id"
        self.started_at = datetime.combine(self.end, datetime.min.time(), timezone.utc)
        self.operation_id = "a" * 32
        self.owner_emails = ("owner@example.test",)
        self.targets = tuple(
            CellWorksheetTarget(
                role=role,
                spreadsheet_id=self.spreadsheet_id,
                worksheet_id=700 + index,
                title=cell_worksheet_title(self.account, role, "cluster_alias"),
                writer_email="writer@example.test",
                finance_emails=("finance@example.test",),
                naming_scheme="cluster_alias",
            )
            for index, role in enumerate(ROLES)
        )
        self.current = []
        calculations = {}
        for offset in range((self.end - self.start).days + 1):
            day = self.start + timedelta(days=offset)
            calculation_ids = []
            for cell_code in OBSERVED_CELL_ROSTER[self.account]:
                calculation_id = f"calc-{day.isoformat()}-{cell_code}"
                calculation_ids.append(calculation_id)
                self.current.append((self.account, cell_code, day, calculation_id))
            calculations[day.isoformat()] = fingerprint(sorted(calculation_ids))

        worksheets = {}
        metadata_sheets = []
        adapters = {}
        for target in self.targets:
            values = [["header", target.role]]
            matrix = {"style_contract": {"role": target.role}, "values": values, "editable_inputs": []}
            if target.role == "summary":
                matrix.update({
                    "date_ranges": [{"start_row": 2, "end_row": 2, "report_date": "2026-10-01"}],
                    "row_keys": ["HEADER", "CASH:cash:MANDIRI"],
                    "blocks": [{"cell_code": None, "start_column": 2}],
                })
            worksheets[target.role] = {
                "worksheet_id": target.worksheet_id,
                "title": target.title,
                "values": values,
                "matrix": matrix,
            }
            marker = {"status": "COMPLETE", "epoch": self.epoch, "fingerprint": "0" * 64}
            metadata_sheets.append({
                "properties": {
                    "sheetId": target.worksheet_id,
                    "title": target.title,
                    "gridProperties": {"rowCount": 3, "columnCount": 2},
                },
                "protectedRanges": [{
                    "protectedRangeId": f"protection-{target.role}",
                    "range": {"sheetId": target.worksheet_id},
                    "description": MANAGED_PREFIX + "COMPUTED",
                    "warningOnly": False,
                    "editors": {"users": [target.writer_email]},
                    "unprotectedRanges": [],
                }],
                "developerMetadata": [{
                    "metadataKey": PUBLICATION_METADATA_KEY,
                    "metadataValue": json.dumps(marker, sort_keys=True),
                }],
            })
            spreadsheet = FakeSpreadsheet(self.spreadsheet_id, self.owner_emails, metadata_sheets)
            worksheet = FakeWorksheet(target.worksheet_id, target.title, values)
            adapters[target.role] = ExistingSheetAdapter(spreadsheet, worksheet)
        self.adapters = adapters

        snapshot = {
            "schema_version": 2,
            "spreadsheet_id": self.spreadsheet_id,
            "worksheets": worksheets,
            "source_calculations": calculations,
        }
        self.snapshot_fingerprint = fingerprint({
            "cluster_id": self.cluster_id,
            "target_id": self.spreadsheet_id,
            "range_start": self.start.isoformat(),
            "range_end": self.end.isoformat(),
            "snapshot": snapshot,
        })
        operation_pin = self.operation_id + "|" + _utc_timestamp(self.started_at)
        self.policy = replace(
            base_policy,
            workbook_id_sha256=hashlib.sha256(self.spreadsheet_id.encode()).hexdigest(),
            operation_pin_sha256=hashlib.sha256(operation_pin.encode()).hexdigest(),
            snapshot_fingerprint=self.snapshot_fingerprint,
            owner_set_sha256=_identity_set_sha256(self.owner_emails),
            finance_set_sha256=_identity_set_sha256(
                email for target in self.targets for email in target.finance_emails
            ),
            writer_set_sha256=_identity_set_sha256(target.writer_email for target in self.targets),
            failed_execution_id_sha256=hashlib.sha256(b"synthetic-failed-execution").hexdigest(),
            operator_reference="synthetic-test-approval",
            audit_receipt_path=self.audit_receipt_path,
            stopped_writers=frozenset({"kestra"}),
            running_local_containers=frozenset({"postgres"}),
        )
        self.approval = _synthetic_approval(self.policy, {
            "profile": self.profile,
            "database_target": base_policy.database_target,
            "spreadsheet_id": self.spreadsheet_id,
            "cluster_id": self.cluster_id,
            "epoch": self.epoch,
            "range_start": self.start.isoformat(),
            "range_end": self.end.isoformat(),
            "expected_error": base_policy.expected_error,
            "operation_id": self.operation_id,
            "operation_started_at": self.started_at,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "failed_execution_id": "synthetic-failed-execution",
            "operator_reference": self.policy.operator_reference,
            "worksheet_targets": self.targets,
            "owner_emails": self.owner_emails,
        })
        for sheet in metadata_sheets:
            sheet["developerMetadata"][0]["metadataValue"] = json.dumps({
                "status": "COMPLETE", "epoch": self.epoch,
                "fingerprint": self.approval.snapshot_fingerprint,
            }, sort_keys=True)
        now = self.quiescence_time
        self.state = {
            "publications": [{
                "cluster_id": self.cluster_id,
                "target_id": self.spreadsheet_id,
                "epoch": self.epoch,
                "range_start": self.start,
                "range_end": self.end,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "snapshot": snapshot,
                "status": "UNKNOWN",
                "confirmed_chunks": ["chunk-0"],
                "error_class": "ReadTimeout",
                "created_at": now,
                "confirmed_at": None,
            }],
            "operations": [{
                "target_id": self.spreadsheet_id,
                "cluster_id": self.cluster_id,
                "operation_id": self.operation_id,
                "status": "UNKNOWN",
                "error_class": "ReadTimeout",
                "started_at": self.started_at,
            }],
            "dates": [
                {"cluster_id": self.cluster_id, "target_id": self.spreadsheet_id,
                 "epoch": self.epoch, "report_date": self.start + timedelta(days=offset), "status": "PENDING"}
                for offset in range((self.end - self.start).days + 1)
            ],
            "current": self.current,
            "dirty": {
                (self.cluster_id, date.fromisoformat(day)):
                    ("f" * 64 if day == "2026-10-15" else digest)
                for day, digest in calculations.items()
            },
        }
        self.connection = FakeConnection(self.state)
        self._write_evidence()

    def _write(self, name, payload: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    def _json(self, name, value) -> Path:
        return self._write(name, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _entry(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def _write_evidence(self):
        dump = self._write("database.dump", b"PGDMP synthetic custom-format archive")
        audit = self._write("backup.audit", b"x")
        archive_pages = []

        def make_sheet(sheet_id, title, rows, columns, protections, metadata):
            end_column = "ET" if columns == 150 else "B" if columns == 2 else "A"
            page_range = f"'{title}'!A1:{end_column}{rows}"
            page_path = f"grid_pages/{sheet_id}/0000.json"
            page = {
                "range": page_range,
                "response": {
                    "spreadsheetId": self.spreadsheet_id,
                    "sheets": [{
                        "properties": {"sheetId": sheet_id, "title": title},
                        "data": [{
                            "startRow": 0,
                            "startColumn": 0,
                            "rowData": [{"values": [{} for _ in range(columns)]} for _ in range(rows)],
                        }],
                    }],
                },
            }
            page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode("utf-8")
            page_sha = hashlib.sha256(page_bytes).hexdigest()
            archive_pages.append((page_path, page_bytes))
            return {
                "worksheet_id": sheet_id,
                "title": title,
                "gridProperties": {"rowCount": rows, "columnCount": columns},
                "grid_pages": [{"range": page_range, "sha256": page_sha}],
                "protectedRanges": protections,
                "developerMetadata": metadata,
            }, {
                "worksheet_id": sheet_id,
                "title": title,
                "grid_pages": [{"range": page_range, "path": page_path, "sha256": page_sha}],
            }

        binding_sheets, archive_sheets = [], []
        for target in self.targets:
            binding_sheet, archive_sheet = make_sheet(
                target.worksheet_id, target.title, 3, 2,
                [{"description": MANAGED_PREFIX + "COMPUTED"}],
                [{
                    "metadataKey": PUBLICATION_METADATA_KEY,
                    "metadataValue": json.dumps({
                        "status": "COMPLETE", "epoch": self.epoch,
                        "fingerprint": self.approval.snapshot_fingerprint,
                    }),
                }],
            )
            binding_sheets.append(binding_sheet)
            archive_sheets.append(archive_sheet)
        for index in range(28):
            binding_sheet, archive_sheet = make_sheet(
                800 + index, f"Other {index}", 1, 150 if index == 0 else 1, [], [],
            )
            binding_sheets.append(binding_sheet)
            archive_sheets.append(archive_sheet)
        binding = self._json("workbook-binding.json", {
            "schema_version": 1,
            "purpose": "finpay-cell-workbook-binding",
            "spreadsheet_id": self.spreadsheet_id,
            "owners": list(self.owner_emails),
            "sheets": binding_sheets,
        })
        self.workbook_binding_path = binding
        metadata = {
            "spreadsheetId": self.spreadsheet_id,
            "sheets": [{
                "properties": {
                    "sheetId": sheet["worksheet_id"],
                    "title": sheet["title"],
                    "gridProperties": sheet["gridProperties"],
                },
                "protectedRanges": sheet["protectedRanges"],
                "developerMetadata": sheet["developerMetadata"],
            } for sheet in binding_sheets],
        }
        permissions = [
            {"type": "user", "role": "owner", "emailAddress": email}
            for email in self.owner_emails
        ] + [
            {"type": "user", "role": "writer", "emailAddress": email}
            for email in sorted({
                email for target in self.targets
                for email in (target.writer_email, *target.finance_emails)
                if email.casefold() not in {owner.casefold() for owner in self.owner_emails}
            })
        ]
        workbook_manifest = {
            "schema_version": 1,
            "purpose": "finpay-cell-workbook-backup-v1",
            "spreadsheet_id": self.spreadsheet_id,
            "metadata_path": "metadata.json",
            "permissions_path": "permissions.json",
            "sheets": archive_sheets,
        }
        workbook_path = self.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(
                workbook_manifest, sort_keys=True, separators=(",", ":"),
            ))
            archive.writestr("metadata.json", json.dumps(metadata, sort_keys=True, separators=(",", ":")))
            archive.writestr("permissions.json", json.dumps(permissions, sort_keys=True, separators=(",", ":")))
            for page_path, page_bytes in archive_pages:
                archive.writestr(page_path, page_bytes)
        workbook_path.chmod(0o600)
        metadata_sha = hashlib.sha256(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()
        permissions_sha = hashlib.sha256(
            json.dumps(permissions, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()
        toc_entries = []
        for index, table in enumerate(recovery_module._REQUIRED_CELL_BACKUP_TABLES):
            toc_entries.append(f"{2 * index + 1}; 1259 {16384 + index} TABLE public {table} finpay")
            toc_entries.append(f"{2 * index + 2}; 0 {16384 + index} TABLE DATA public {table} finpay")
        toc_entries.append(
            f"{len(toc_entries) + 1}; 0 0 VIEW public finpay_cell_current_results_v1 finpay",
        )
        archive_entries = recovery_module._APPROVED_DATABASE_TOC_COUNT
        listed_entries = recovery_module._APPROVED_DATABASE_LISTED_ENTRY_COUNT
        for index in range(listed_entries - len(toc_entries)):
            toc_entries.append(f"{len(toc_entries) + 1}; 0 0 COMMENT - SCHEMA public synthetic_{index}")
        restore_listing = self._write(
            "database-restore-list.txt",
            ("; Archive created at 2026-11-02\n; dbname: finpay\n; Format: CUSTOM\n"
             f"; TOC Entries: {archive_entries}\n" + "\n".join(toc_entries) + "\n").encode(),
        )
        database_backup_receipt = self._json("database-backup-receipt.json", {
            "archive_verified": True,
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "bytes": dump.stat().st_size,
            "archive_entries": archive_entries,
            "listed_entries": listed_entries,
            "verification_tool": "pg_restore --list",
            "verification_exit_code": 0,
            "archive_listing_sha256": hashlib.sha256(restore_listing.read_bytes()).hexdigest(),
            "database_target": approval_payload(self.approval)["database_target"],
        })
        workbook_binding_receipt = self._json("workbook-binding-refresh.json", {
            "source_workbook_backup_sha256": hashlib.sha256(workbook_path.read_bytes()).hexdigest(),
            "binding_tabs": self.policy.workbook_sheet_count,
            "grid_property_count": self.policy.workbook_sheet_count,
            "binding_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
            "metadata_sha256": metadata_sha,
            "permissions_sha256": permissions_sha,
            "grid_page_count": len(archive_pages),
        })
        pins_receipt = self._json("recovery-pins.json", {
            "approval": approval_payload(self.approval),
        })
        scope_receipt = self._json("scope.json", {
            "local_mode": True,
            "namespace": "finance.finpay",
            "flow_id": "finpay_cell_bulk_v1",
            "flow_revision": self.policy.failed_flow_revision,
            "execution_id": self.approval.failed_execution_id,
            "variables": {
                "cell_spreadsheet_id": self.spreadsheet_id,
                "cell_finance_emails": ["finance@example.test"],
            },
            "inputs": dict(self.policy.scope_inputs),
        })
        self.backup_manifest_path = self._json("backup-manifest.json", {
            "schema_version": 1,
            "purpose": "finpay-cell-unknown-recovery",
            "approval": approval_payload(self.approval),
            "database_dump": self._entry(dump),
            "workbook_backup": self._entry(workbook_path),
            "workbook_binding": self._entry(binding),
            "audit_receipts": [
                self._entry(audit), self._entry(database_backup_receipt),
                self._entry(workbook_binding_receipt), self._entry(pins_receipt),
                self._entry(scope_receipt), self._entry(restore_listing),
            ],
        })
        quiescence_time = self.quiescence_time
        writer_receipt = self._json("writer-stop.json", {
            "schema_version": 1,
            "kind": "writer-stop",
            "writer": "kestra",
            "container_id": "synthetic-container",
            "state_before": "running",
            "state_after": "exited",
            "observed_at": (quiescence_time - timedelta(seconds=2)).isoformat(),
        })
        active_receipt = self._json("active-jobs.json", {
            "schema_version": 1,
            "kind": "kestra-active-executions",
            "observed_at": (quiescence_time - timedelta(seconds=1)).isoformat(),
            "active_executions": [],
            "kestra_container_state": "exited",
            "running_containers": ["postgres"],
        })
        self.quiescence_manifest_path = self._json("quiescence-manifest.json", {
            "schema_version": 1,
            "purpose": "finpay-cell-writer-quiescence",
            "approval_sha256": approval_fingerprint(self.approval),
            "observed_at": quiescence_time.isoformat(),
            "stopped_writers": [{
                "name": next(iter(self.policy.stopped_writers)),
                "receipt": self._entry(writer_receipt),
            }],
            "no_active_job": {"receipt": self._entry(active_receipt)},
            "running_containers": sorted(self.policy.running_local_containers),
        })
    @contextmanager
    def selected_policy(self):
        with _selected_test_policy(self.policy):
            yield

    def reconcile(self, *, approval=None, receipt_path=None):
        with self.selected_policy():
            return reconcile_unknown_publication(
                approval=approval or self.approval,
                connection=self.connection,
                adapters=self.adapters,
                backup_manifest_path=self.backup_manifest_path,
                quiescence_manifest_path=self.quiescence_manifest_path,
                audit_receipt_path=receipt_path or self.audit_receipt_path,
            )

    def check_workbook_binding(self, binding, *, approval=None):
        with self.selected_policy():
            return _check_workbook_binding(binding, approval or self.approval)

    def verify_workbook_archive(self, archive_path, binding, *, approval=None):
        with self.selected_policy():
            return recovery_module._verify_workbook_archive(
                approval or self.approval, archive_path, binding,
            )

    def verify_execution_scope(self, scope, *, approval=None):
        with self.selected_policy():
            return recovery_module._verify_execution_scope(scope, approval or self.approval)

    def validate_approval(self, **changes):
        values = _approval_values(self.approval)
        values.update(changes)
        candidate = _raw_approval(values)
        with self.selected_policy():
            recovery_module._validate_approval(candidate)


class CellRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fixture = RecoveryFixture(Path(self.temp_dir.name))
        self.style_patch = patch("finpay_pipeline.cell_recovery._verify_style")
        self.style_mock = self.style_patch.start()
        self.addCleanup(self.style_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_positive_reconciliation_is_read_only_on_sheets_and_atomic_in_database(self):
        result = self.fixture.reconcile()

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["dates_completed"], (self.fixture.end - self.fixture.start).days + 1)
        self.assertEqual(self.fixture.state["publications"][0]["status"], "COMPLETE")
        self.assertEqual(self.fixture.state["publications"][0]["error_class"], "ReadTimeout")
        self.assertEqual(self.fixture.state["publications"][0]["confirmed_chunks"][0], "chunk-0")
        self.assertEqual(len(self.fixture.state["publications"][0]["confirmed_chunks"]), 2)
        self.assertEqual({row["status"] for row in self.fixture.state["dates"]}, {"COMPLETE"})
        self.assertEqual(self.fixture.state["operations"][0]["status"], "IDLE")
        self.assertEqual(self.fixture.state["operations"][0]["error_class"], "ReadTimeout")
        self.assertEqual(self.fixture.state["operations"][0]["started_at"], self.fixture.started_at)
        self.assertNotIn("profile", approval_payload(self.fixture.approval))
        self.assertEqual(self.fixture.state["dirty"], {
            (self.fixture.cluster_id, date(2026, 10, 15)): "f" * 64,
        })
        self.assertEqual(self.fixture.connection.commits, 1)
        self.assertEqual(self.fixture.connection.rollbacks, 0)
        self.assertFalse(self.fixture.connection.locks)
        self.assertEqual(self.style_mock.call_count, 4)
        for args, _ in self.style_mock.call_args_list:
            self.assertEqual(len(args[2]["values"]), 3)
            self.assertEqual(len(args[2]["values"][0]), 2)
        self.assertEqual(self.fixture.adapters["summary"].spreadsheet.write_calls, 0)
        self.assertTrue(all(
            adapter.spreadsheet.write_calls == 0
            for adapter in self.fixture.adapters.values()
        ))
        lines = self.fixture.audit_receipt_path.read_text().splitlines()
        self.assertEqual([json.loads(line)["event"] for line in lines], ["intent", "completed"])
        self.assertEqual(
            result["audit_receipt_sha256"],
            hashlib.sha256(self.fixture.audit_receipt_path.read_bytes()).hexdigest(),
        )
        self.assertIn(
            f"operator-recovery:{result['audit_proof_sha256']}",
            self.fixture.state["publications"][0]["confirmed_chunks"],
        )

    def test_approval_rejects_wrong_epoch_range_database_and_error(self):
        approval = self.fixture.approval
        with self.assertRaisesRegex(ValueError, "epoch"):
            self.fixture.validate_approval(epoch=5)
        with self.assertRaisesRegex(ValueError, "report-date range"):
            self.fixture.validate_approval(range_end="2026-10-28")
        with self.assertRaisesRegex(ValueError, "ambiguous error"):
            self.fixture.validate_approval(expected_error="TimeoutError")
        with self.assertRaisesRegex(ValueError, "FinPay database"):
            self.fixture.validate_approval(
                database_target=CellDatabaseTarget("postgres", 5432, "other", "public", "finpay"),
            )

    def test_approval_is_pinned_to_the_exact_local_unknown_incident(self):
        approval = self.fixture.approval
        cases = (
            ("spreadsheet_id", "other-workbook", "approved non-production workbook"),
            ("cluster_id", "422040", "specifically approved cluster"),
            ("operation_id", "b" * 32, "target-operation instance"),
            ("failed_execution_id", "different-execution", "failed local Kestra execution"),
            ("operator_reference", "different-approval", "user-approved recovery authorization"),
        )
        for field, value, error in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, error):
                    self.fixture.validate_approval(**{field: value})

    def test_workbook_binding_pages_must_cover_the_complete_grid(self):
        binding_path = self.fixture.directory / "workbook-binding.json"
        binding = json.loads(binding_path.read_text())
        binding["sheets"][0]["grid_pages"] = [{
            "range": f"'{self.fixture.targets[0].title}'!A1:B2",
            "sha256": "b" * 64,
        }]
        with self.assertRaisesRegex(ValueError, "complete saved sheet extent"):
            self.fixture.check_workbook_binding(binding)

    def test_database_identity_mismatch_fails_before_lock_or_mutation(self):
        self.fixture.connection.database_identity = ("other", "finpay", "public")
        before = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(ValueError, "connected database/user/schema"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, before)
        self.assertFalse(self.fixture.connection.locks)
        self.assertEqual(self.fixture.connection.commits, 0)

    def test_other_unknown_or_pending_history_blocks_recovery(self):
        for status in ("UNKNOWN", "IN_FLIGHT", "PENDING"):
            with self.subTest(status=status):
                fixture = self.fixture
                extra = copy.deepcopy(fixture.state["publications"][0])
                extra.update({"target_id": "other-workbook", "epoch": 3, "status": status})
                fixture.state["publications"].append(extra)
                before = copy.deepcopy(fixture.state)
                with self.assertRaisesRegex(RuntimeError, "another non-idle"):
                    fixture.reconcile()
                self.assertEqual(fixture.state, before)
                fixture.state["publications"].pop()

    def test_other_nonidle_operation_blocks_recovery(self):
        extra = copy.deepcopy(self.fixture.state["operations"][0])
        extra.update({"target_id": "other-workbook", "operation_id": "b" * 32, "status": "IN_FLIGHT"})
        self.fixture.state["operations"].append(extra)
        before = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(RuntimeError, "exactly one unresolved"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, before)

    def test_failed_cluster_lock_releases_previously_acquired_target_lock(self):
        self.fixture.connection.lock_results["finpay-cell-cluster-v1"] = False
        with self.assertRaisesRegex(RuntimeError, "cluster is already locked"):
            self.fixture.reconcile()
        self.assertFalse(self.fixture.connection.locks)
        self.assertEqual(self.fixture.connection.commits, 0)

    def test_target_lock_conflict_fails_closed(self):
        self.fixture.connection.lock_results["finpay-cell-publisher-v1"] = False
        with self.assertRaisesRegex(RuntimeError, "target is already locked"):
            self.fixture.reconcile()
        self.assertFalse(self.fixture.connection.locks)
        self.assertEqual(self.fixture.connection.commits, 0)

    def test_stale_publication_history_and_snapshot_fingerprint_fail_closed(self):
        newer = copy.deepcopy(self.fixture.state["publications"][0])
        newer.update({"epoch": 5, "status": "COMPLETE"})
        self.fixture.state["publications"].append(newer)
        with self.assertRaisesRegex(ValueError, "not the latest"):
            self.fixture.reconcile()
        self.fixture.state["publications"].pop()

        self.fixture.state["publications"][0]["snapshot_fingerprint"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "differs from exact approved pins"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_wrong_operation_pins_and_unknown_status_fail_closed(self):
        self.fixture.state["operations"][0]["started_at"] += timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "target operation row"):
            self.fixture.reconcile()
        self.fixture.state["operations"][0]["started_at"] = self.fixture.started_at
        self.fixture.state["operations"][0]["status"] = "IN_FLIGHT"
        with self.assertRaisesRegex(ValueError, "target operation row"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_current_roster_coverage_and_calculation_fingerprints_are_pinned(self):
        self.fixture.state["current"].pop()
        with self.assertRaisesRegex(ValueError, "roster coverage is incomplete"):
            self.fixture.reconcile()
        self.fixture.state["current"].append(("422010_B", "422013", date(2026, 10, 29), "changed-calc"))
        with self.assertRaisesRegex(ValueError, "calculation fingerprints differ"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_live_values_styles_markers_protections_and_owners_are_all_checked(self):
        self.fixture.adapters["summary"].worksheet.values[0][0] = "changed"
        with self.assertRaisesRegex(ValueError, "value/formula/tail readback mismatch"):
            self.fixture.reconcile()
        self.fixture.adapters["summary"].worksheet.values[0][0] = "header"

        self.style_patch.stop()
        with patch("finpay_pipeline.cell_recovery._verify_style", side_effect=ValueError("style mismatch")):
            with self.assertRaisesRegex(ValueError, "style mismatch"):
                self.fixture.reconcile()
        self.style_patch = patch("finpay_pipeline.cell_recovery._verify_style")
        self.style_mock = self.style_patch.start()
        self.addCleanup(self.style_patch.stop)

        summary = self.fixture.adapters["summary"].spreadsheet.sheets[0]
        summary["developerMetadata"][0]["metadataValue"] = json.dumps({"status": "PENDING"})
        with self.assertRaisesRegex(ValueError, "metadata readback mismatch"):
            self.fixture.reconcile()
        summary["developerMetadata"][0]["metadataValue"] = json.dumps({
            "status": "COMPLETE", "epoch": self.fixture.epoch,
            "fingerprint": self.fixture.snapshot_fingerprint,
        })

        summary["protectedRanges"].append({"description": "unmanaged"})
        with self.assertRaisesRegex(ValueError, "protections contain missing, extra"):
            self.fixture.reconcile()
        summary["protectedRanges"].pop()
        self.fixture.adapters["summary"].spreadsheet.client.owners = ["other@example.test"]
        self.fixture.adapters["summary"]._owners = None
        with self.assertRaisesRegex(ValueError, "owners differ"):
            self.fixture.reconcile()

    def test_live_grid_extent_must_match_backup_before_grid_reads(self):
        summary = self.fixture.adapters["summary"]
        summary.spreadsheet.sheets[0]["properties"]["gridProperties"]["rowCount"] = 4
        with self.assertRaisesRegex(ValueError, "differs from the pinned backup"):
            self.fixture.reconcile()
        self.assertEqual(summary.worksheet.get_calls, 0)
        self.assertEqual(self.style_mock.call_count, 0)
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")
        self.assertFalse(self.fixture.connection.locks)

    def test_excessive_backup_grid_is_rejected_before_database_or_sheet_calls(self):
        binding_path = self.fixture.directory / "workbook-binding.json"
        binding = json.loads(binding_path.read_text())
        binding["sheets"][0]["gridProperties"]["rowCount"] = 100_001
        binding_path.write_text(json.dumps(binding, sort_keys=True))
        binding_path.chmod(0o600)
        manifest = json.loads(self.fixture.backup_manifest_path.read_text())
        manifest["workbook_binding"] = RecoveryFixture._entry(binding_path)
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "exceeds the bounded read contract"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.connection.calls, [])
        self.assertTrue(all(adapter.worksheet.get_calls == 0 for adapter in self.fixture.adapters.values()))

    def test_stale_value_outside_saved_rectangle_is_rejected(self):
        summary = self.fixture.adapters["summary"]
        summary.worksheet.values.append(["", "leftover"])
        with self.assertRaisesRegex(ValueError, "value/formula/tail readback mismatch"):
            self.fixture.reconcile()
        self.assertEqual(summary.worksheet.get_calls, 1)
        self.assertFalse(self.fixture.audit_receipt_path.exists())
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_real_style_verification_accepts_blank_extent_and_rejects_styled_tail(self):
        from finpay_pipeline.cell_detail_sheets import UNUSUAL_HEADERS
        from finpay_pipeline.cell_publication_v2 import (
            _expected_style,
            _verify_style as verify_style,
            bind_cell_style_contract,
        )

        matrix = {
            "layout_version": 2,
            "headers": list(UNUSUAL_HEADERS),
            "values": [list(UNUSUAL_HEADERS), [""] * len(UNUSUAL_HEADERS)],
            "editable_inputs": [],
        }
        bind_cell_style_contract("unusual", matrix)
        width = len(UNUSUAL_HEADERS)

        def rgb(value):
            return {channel: int(value[index:index + 2], 16) / 255
                    for channel, index in (("red", 0), ("green", 2), ("blue", 4))}

        def entered_format(expected):
            raw = {
                "backgroundColor": rgb(expected["background"]),
                "textFormat": {
                    "foregroundColor": rgb(expected["foreground"]),
                    "fontFamily": expected["family"],
                    "fontSize": expected["size"],
                    "bold": expected["bold"],
                },
                "horizontalAlignment": expected["align"],
                "verticalAlignment": expected["vertical"],
                "wrapStrategy": expected["wrap"],
            }
            if expected.get("number_type") is not None:
                raw["numberFormat"] = {"type": expected["number_type"]}
                if expected.get("number_pattern") is not None:
                    raw["numberFormat"]["pattern"] = expected["number_pattern"]
            raw["borders"] = {
                side: {"style": style, **({"color": rgb(color)} if color else {})}
                for side, (style, color) in expected.get("borders", {}).items()
                if style != "NONE"
            }
            return raw

        class StyleAdapter:
            tail_style = False

            def read_style(self, rows, columns):
                formats = [
                    [entered_format(_expected_style(matrix, row, column, None)) for column in range(width)]
                    for row in range(len(matrix["values"]))
                ]
                entered = [[{} for _ in range(columns)] for _ in range(rows)]
                if self.tail_style:
                    entered[2][0] = {"textFormat": {"bold": True}}
                yield {
                    "start": 0,
                    "grid": {"frozenRowCount": 1, "frozenColumnCount": 0, "hideGridlines": True},
                    "merges": [],
                    "banding": [],
                    "default": {},
                    "widths": matrix["style_contract"]["widths"],
                    "heights": [21] * rows,
                    "formats": formats + [[{} for _ in range(columns)] for _ in range(rows - len(formats))],
                    "entered": entered,
                }

        style_adapter = StyleAdapter()
        extent = {"values": [("",) * width] * 3}
        verify_style(style_adapter, matrix, extent)
        style_adapter.tail_style = True
        with self.assertRaisesRegex(ValueError, "obsolete tail formatting"):
            verify_style(style_adapter, matrix, extent)

    def directory_path(self, name):
        return self.fixture.directory / name

    def test_unresolved_temporary_guard_blocks_without_any_write(self):
        from finpay_pipeline.cell_publication import GUARD_PREFIX

        self.fixture.adapters["summary"].spreadsheet.sheets[0]["protectedRanges"].append({
            "description": GUARD_PREFIX + "unfinished",
        })
        with self.assertRaisesRegex(RuntimeError, "temporary worksheet guard remains"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_bad_backup_hash_scope_or_workbook_pin_blocks_before_database_access(self):
        dump = self.fixture.directory / "database.dump"
        dump.write_bytes(b"changed after manifest")
        dump.chmod(0o600)
        before_calls = len(self.fixture.connection.calls)
        with self.assertRaisesRegex(ValueError, "database dump file checksum"):
            self.fixture.reconcile()
        self.assertEqual(len(self.fixture.connection.calls), before_calls)

        dump.write_bytes(b"PGDMP synthetic custom-format archive")
        dump.chmod(0o600)
        workbook = self.fixture.directory / "workbook-backup.zip"
        original_workbook_bytes = workbook.read_bytes()
        workbook.write_bytes(b"changed workbook archive")
        workbook.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "workbook backup file checksum"):
            self.fixture.reconcile()
        workbook.write_bytes(original_workbook_bytes)
        workbook.chmod(0o600)

        manifest = json.loads(self.fixture.backup_manifest_path.read_text())
        manifest["approval"]["spreadsheet_id"] = "different-workbook"
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "manifest scope differs"):
            self.fixture.reconcile()

        manifest["approval"] = approval_payload(self.fixture.approval)
        binding_path = self.fixture.directory / "workbook-binding.json"
        binding = json.loads(binding_path.read_text())
        binding["spreadsheet_id"] = "different-workbook"
        binding_path.write_text(json.dumps(binding, sort_keys=True))
        binding_path.chmod(0o600)
        manifest["workbook_binding"] = RecoveryFixture._entry(binding_path)
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "binding spreadsheet ID"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_backup_archive_signature_and_grid_page_hashes_are_verified(self):
        dump = self.fixture.directory / "database.dump"
        dump.write_bytes(b"not-a-postgres-archive")
        dump.chmod(0o600)
        manifest = json.loads(self.fixture.backup_manifest_path.read_text())
        manifest["database_dump"] = RecoveryFixture._entry(dump)
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "not a PostgreSQL custom-format archive"):
            self.fixture.reconcile()

        dump.write_bytes(b"PGDMP synthetic custom-format archive")
        dump.chmod(0o600)
        manifest["database_dump"] = RecoveryFixture._entry(dump)
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        workbook_receipt_path = self.fixture.directory / "workbook-binding-refresh.json"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        first_page = archive_manifest["sheets"][0]["grid_pages"][0]["path"]
        entries[first_page] += b" "
        tampered_path = self.fixture.directory / "workbook-tampered.zip"
        with zipfile.ZipFile(tampered_path, "w", compression=zipfile.ZIP_STORED) as tampered:
            for name, contents in entries.items():
                tampered.writestr(name, contents)
        tampered_path.chmod(0o600)
        workbook_receipt = json.loads(workbook_receipt_path.read_text())
        workbook_receipt["source_workbook_backup_sha256"] = hashlib.sha256(
            tampered_path.read_bytes(),
        ).hexdigest()
        workbook_receipt_path.write_text(json.dumps(workbook_receipt, sort_keys=True))
        workbook_receipt_path.chmod(0o600)
        manifest["workbook_backup"] = RecoveryFixture._entry(tampered_path)
        manifest["audit_receipts"] = [
            RecoveryFixture._entry(workbook_receipt_path)
            if Path(item["path"]) == workbook_receipt_path else item
            for item in manifest["audit_receipts"]
        ]
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "grid-page checksum mismatch"):
            self.fixture.reconcile()

    def test_database_and_receipt_size_bounds_precede_hashing(self):
        with patch.object(recovery_module, "_MAX_DATABASE_ARCHIVE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "database dump exceeds its size bound"):
                self.fixture.reconcile()
        self.assertEqual(self.fixture.connection.calls, [])

        checked_receipts = []
        original_check = recovery_module._checked_file

        def track_checked_receipt(entry, label, **kwargs):
            result = original_check(entry, label, **kwargs)
            if label == "backup audit receipt":
                checked_receipts.append(Path(entry["path"]).name)
            return result

        with patch.object(recovery_module, "_MAX_BACKUP_RECEIPT_BYTES", 1), patch.object(
            recovery_module, "_checked_file", side_effect=track_checked_receipt,
        ):
            with self.assertRaisesRegex(ValueError, "backup audit receipt exceeds its size bound"):
                self.fixture.reconcile()
        self.assertIn("backup.audit", checked_receipts)
        self.assertNotIn("database-backup-receipt.json", checked_receipts)
        self.assertEqual(self.fixture.connection.calls, [])

        with patch.object(recovery_module, "_MAX_RESTORE_LIST_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "backup audit receipt exceeds its size bound"):
                self.fixture.reconcile()
        self.assertEqual(self.fixture.connection.calls, [])

    def test_workbook_archive_requires_saved_grid_rows(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        first_page = archive_manifest["sheets"][0]["grid_pages"][0]
        page = json.loads(entries[first_page["path"]])
        page["response"]["sheets"][0]["data"] = []
        page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        first_page["sha256"] = hashlib.sha256(page_bytes).hexdigest()
        binding["sheets"][0]["grid_pages"][0]["sha256"] = first_page["sha256"]
        entries[first_page["path"]] = page_bytes
        entries["manifest.json"] = json.dumps(
            archive_manifest, sort_keys=True, separators=(",", ":"),
        ).encode()
        tampered_path = self.fixture.directory / "workbook-no-grid-rows.zip"
        with zipfile.ZipFile(tampered_path, "w", compression=zipfile.ZIP_STORED) as tampered:
            for name, contents in entries.items():
                tampered.writestr(name, contents)
        tampered_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "omits its requested grid data"):
            self.fixture.verify_workbook_archive(tampered_path, binding)

    def test_workbook_archive_accepts_omitted_trailing_blank_rows(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        first_page = archive_manifest["sheets"][0]["grid_pages"][0]
        page_path = first_page["path"]
        page = json.loads(entries[page_path])
        page["response"]["sheets"][0]["data"][0]["rowData"].pop()
        page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        page_sha = hashlib.sha256(page_bytes).hexdigest()
        first_page["sha256"] = page_sha
        binding["sheets"][0]["grid_pages"][0]["sha256"] = page_sha
        entries[page_path] = page_bytes
        entries["manifest.json"] = json.dumps(
            archive_manifest, sort_keys=True, separators=(",", ":"),
        ).encode()
        sparse_path = self.fixture.directory / "workbook-sparse-rows.zip"
        with zipfile.ZipFile(sparse_path, "w", compression=zipfile.ZIP_STORED) as sparse:
            for name, contents in entries.items():
                sparse.writestr(name, contents)
        sparse_path.chmod(0o600)
        result = self.fixture.verify_workbook_archive(sparse_path, binding)
        self.assertEqual(result[2], 32)

    def test_workbook_archive_accepts_an_explicit_blank_grid_page(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        blank_sheet_index = 4
        blank_page = archive_manifest["sheets"][blank_sheet_index]["grid_pages"][0]
        page_path = blank_page["path"]
        page = json.loads(entries[page_path])
        page["response"]["sheets"][0]["data"] = [{"startRow": 0, "startColumn": 0}]
        page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        page_sha = hashlib.sha256(page_bytes).hexdigest()
        blank_page["sha256"] = page_sha
        binding["sheets"][blank_sheet_index]["grid_pages"][0]["sha256"] = page_sha
        entries[page_path] = page_bytes
        entries["manifest.json"] = json.dumps(
            archive_manifest, sort_keys=True, separators=(",", ":"),
        ).encode()
        blank_path = self.fixture.directory / "workbook-explicit-blank-page.zip"
        with zipfile.ZipFile(blank_path, "w", compression=zipfile.ZIP_STORED) as blank_archive:
            for name, contents in entries.items():
                blank_archive.writestr(name, contents)
        blank_path.chmod(0o600)
        result = self.fixture.verify_workbook_archive(blank_path, binding)
        self.assertEqual(result[2], 32)

    def test_workbook_archive_rejects_non_cell_data_values(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        first_page = archive_manifest["sheets"][0]["grid_pages"][0]
        page_path = first_page["path"]
        page = json.loads(entries[page_path])
        page["response"]["sheets"][0]["data"][0]["rowData"][0]["values"][0] = "not CellData"
        page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        page_sha = hashlib.sha256(page_bytes).hexdigest()
        first_page["sha256"] = page_sha
        binding["sheets"][0]["grid_pages"][0]["sha256"] = page_sha
        entries[page_path] = page_bytes
        entries["manifest.json"] = json.dumps(
            archive_manifest, sort_keys=True, separators=(",", ":"),
        ).encode()
        malformed_path = self.fixture.directory / "workbook-malformed-cell.zip"
        with zipfile.ZipFile(malformed_path, "w", compression=zipfile.ZIP_STORED) as malformed:
            for name, contents in entries.items():
                malformed.writestr(name, contents)
        malformed_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "malformed cell data"):
            self.fixture.verify_workbook_archive(malformed_path, binding)

    def test_workbook_archive_rejects_rows_beyond_requested_extent(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        archive_manifest = json.loads(entries["manifest.json"])
        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        first_page = archive_manifest["sheets"][0]["grid_pages"][0]
        page_path = first_page["path"]
        page = json.loads(entries[page_path])
        page["response"]["sheets"][0]["data"][0]["rowData"].append({"values": []})
        page_bytes = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        page_sha = hashlib.sha256(page_bytes).hexdigest()
        first_page["sha256"] = page_sha
        binding["sheets"][0]["grid_pages"][0]["sha256"] = page_sha
        entries[page_path] = page_bytes
        entries["manifest.json"] = json.dumps(
            archive_manifest, sort_keys=True, separators=(",", ":"),
        ).encode()
        oversized_path = self.fixture.directory / "workbook-oversized-rows.zip"
        with zipfile.ZipFile(oversized_path, "w", compression=zipfile.ZIP_STORED) as oversized:
            for name, contents in entries.items():
                oversized.writestr(name, contents)
        oversized_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "exceeds requested rows"):
            self.fixture.verify_workbook_archive(oversized_path, binding)

    def test_database_listing_requires_matching_toc_count_and_recovery_tables(self):
        listing_path = self.fixture.directory / "database-restore-list.txt"
        database_receipt_path = self.fixture.directory / "database-backup-receipt.json"

        def rewrite_listing(lines, archive_entries, listed_entries):
            listing_path.write_text("\n".join(lines) + "\n")
            listing_path.chmod(0o600)
            receipt = json.loads(database_receipt_path.read_text())
            receipt["archive_entries"] = archive_entries
            receipt["listed_entries"] = listed_entries
            receipt["archive_listing_sha256"] = hashlib.sha256(listing_path.read_bytes()).hexdigest()
            database_receipt_path.write_text(json.dumps(receipt, sort_keys=True))
            database_receipt_path.chmod(0o600)
            manifest = json.loads(self.fixture.backup_manifest_path.read_text())
            manifest["audit_receipts"] = [
                RecoveryFixture._entry(listing_path) if Path(item["path"]) == listing_path else item
                for item in manifest["audit_receipts"]
            ]
            manifest["audit_receipts"] = [
                RecoveryFixture._entry(database_receipt_path)
                if Path(item["path"]) == database_receipt_path else item
                for item in manifest["audit_receipts"]
            ]
            self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
            self.fixture.backup_manifest_path.chmod(0o600)

        original_lines = listing_path.read_text().splitlines()
        header_lines = [line for line in original_lines if not line or line.startswith(";")]
        toc_entries = [line for line in original_lines if line and not line.startswith(";")]
        approved_toc_count = recovery_module._APPROVED_DATABASE_TOC_COUNT
        mismatch_lines = [
            line.replace(f"TOC Entries: {approved_toc_count}", "TOC Entries: 1")
            for line in original_lines
        ]
        rewrite_listing(mismatch_lines, approved_toc_count, len(toc_entries))
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

        rewrite_listing(original_lines, approved_toc_count, len(toc_entries) - 1)
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

        extra_record_lines = original_lines + ["20003; 0 0 COMMENT - SCHEMA public finpay"]
        rewrite_listing(extra_record_lines, approved_toc_count, len(toc_entries))
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

        rewrite_listing(original_lines, approved_toc_count, len(toc_entries))

        missing_table = "finpay_cell_target_operations_v1"
        incomplete_entries = [
            line for line in toc_entries if missing_table not in line
        ]
        incomplete_entries.extend(
            f"{20000 + index}; 0 0 COMMENT - SCHEMA public finpay" for index in range(2)
        )
        incomplete_lines = [
            line.replace(f"TOC Entries: {approved_toc_count}", f"TOC Entries: {approved_toc_count}")
            for line in header_lines
        ] + incomplete_entries
        rewrite_listing(incomplete_lines, approved_toc_count, len(incomplete_entries))
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

        missing_data_table = "finpay_cell_target_operations_v1"
        without_table_data = [
            line for line in toc_entries
            if f"TABLE DATA public {missing_data_table}" not in line
        ]
        without_table_data.append("20004; 0 0 COMMENT - SCHEMA public finpay")
        no_data_lines = header_lines + without_table_data
        rewrite_listing(no_data_lines, approved_toc_count, len(without_table_data))
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

        missing_view = "finpay_cell_current_results_v1"
        without_view = [line for line in toc_entries if missing_view not in line]
        without_view.append("20002; 0 0 COMMENT - SCHEMA public finpay")
        view_lines = [
            line.replace(f"TOC Entries: {approved_toc_count}", f"TOC Entries: {approved_toc_count}")
            for line in header_lines
        ] + without_view
        rewrite_listing(view_lines, approved_toc_count, len(without_view))
        with self.assertRaisesRegex(ValueError, "complete matching pg_restore archive listing"):
            self.fixture.reconcile()

    def test_database_listing_accepts_the_pinned_postgres_18_50_45_counts(self):
        receipt = json.loads((self.fixture.directory / "database-backup-receipt.json").read_text())
        self.assertEqual(receipt["archive_entries"], 50)
        self.assertEqual(receipt["listed_entries"], 45)

        result = self.fixture.reconcile()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(self.fixture.connection.commits, 1)

    def test_workbook_archive_rejects_unpinned_read_permissions(self):
        workbook_path = self.fixture.directory / "workbook-backup.zip"
        with zipfile.ZipFile(workbook_path) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        permissions = json.loads(entries["permissions.json"])
        permissions.append({
            "type": "user", "role": "reader", "emailAddress": "unexpected-reader@example.test",
        })
        permission_bytes = json.dumps(permissions, sort_keys=True, separators=(",", ":")).encode()
        entries["permissions.json"] = permission_bytes
        tampered_path = self.fixture.directory / "workbook-reader-added.zip"
        with zipfile.ZipFile(tampered_path, "w", compression=zipfile.ZIP_STORED) as tampered:
            for name, contents in entries.items():
                tampered.writestr(name, contents)
        tampered_path.chmod(0o600)

        receipt_path = self.fixture.directory / "workbook-binding-refresh.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["source_workbook_backup_sha256"] = hashlib.sha256(tampered_path.read_bytes()).hexdigest()
        receipt["permissions_sha256"] = hashlib.sha256(permission_bytes).hexdigest()
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))
        receipt_path.chmod(0o600)
        manifest = json.loads(self.fixture.backup_manifest_path.read_text())
        manifest["workbook_backup"] = RecoveryFixture._entry(tampered_path)
        manifest["audit_receipts"] = [
            RecoveryFixture._entry(receipt_path)
            if Path(item["path"]) == receipt_path else item
            for item in manifest["audit_receipts"]
        ]
        self.fixture.backup_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.backup_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "readers differ from approved read permissions"):
            self.fixture.reconcile()

    def test_bad_quiescence_receipt_blocks_before_database_access(self):
        manifest = json.loads(self.fixture.quiescence_manifest_path.read_text())
        active_path = self.fixture.directory / "active-jobs.json"
        active_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "kestra-active-executions",
            "observed_at": (self.fixture.quiescence_time - timedelta(seconds=1)).isoformat(),
            "active_executions": ["still-running"],
            "kestra_container_state": "exited",
            "running_containers": ["postgres"],
        }))
        active_path.chmod(0o600)
        manifest["no_active_job"]["receipt"] = RecoveryFixture._entry(active_path)
        self.fixture.quiescence_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.quiescence_manifest_path.chmod(0o600)
        before_calls = len(self.fixture.connection.calls)
        with self.assertRaisesRegex(ValueError, "no execution was active"):
            self.fixture.reconcile()
        self.assertEqual(len(self.fixture.connection.calls), before_calls)
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")

    def test_quiescence_rejects_a_writer_receipt_that_does_not_prove_stopped(self):
        manifest = json.loads(self.fixture.quiescence_manifest_path.read_text())
        writer_path = self.fixture.directory / "writer-stop.json"
        writer_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "writer-stop",
            "writer": "kestra",
            "container_id": "synthetic-container",
            "state_before": "running",
            "state_after": "running",
            "observed_at": (self.fixture.quiescence_time - timedelta(seconds=2)).isoformat(),
        }))
        writer_path.chmod(0o600)
        manifest["stopped_writers"][0]["receipt"] = RecoveryFixture._entry(writer_path)
        self.fixture.quiescence_manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self.fixture.quiescence_manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "does not prove the writer stopped"):
            self.fixture.reconcile()

    def test_partial_cas_rolls_back_publication_and_all_dates_and_preserves_receipt(self):
        self.fixture.connection.fail_at = "operation"
        before = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(RuntimeError, "target-operation compare-and-swap"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, before)
        self.assertEqual(self.fixture.connection.rollbacks, 1)
        self.assertFalse(self.fixture.connection.locks)
        events = [json.loads(line)["event"] for line in self.fixture.audit_receipt_path.read_text().splitlines()]
        self.assertEqual(events, ["intent", "rolled_back"])

    def test_commit_ack_loss_records_commit_unknown_and_refuses_retry(self):
        self.fixture.connection.fail_commit = True
        with self.assertRaisesRegex(RuntimeError, "synthetic commit acknowledgement lost"):
            self.fixture.reconcile()

        self.assertEqual(self.fixture.state["publications"][0]["status"], "COMPLETE")
        self.assertEqual({row["status"] for row in self.fixture.state["dates"]}, {"COMPLETE"})
        self.assertEqual(self.fixture.profile, CellRecoveryProfile.EPOCH4)
        expected_epoch4_dates = [
            (date(2026, 10, 1) + timedelta(days=offset)).isoformat()
            for offset in range(29)
        ]
        self.assertEqual(
            (expected_epoch4_dates[0], expected_epoch4_dates[-1], len(expected_epoch4_dates)),
            ("2026-10-01", "2026-10-29", 29),
        )
        self.assertEqual(
            [row["report_date"].isoformat() for row in self.fixture.state["dates"]],
            expected_epoch4_dates,
        )
        self.assertEqual(self.fixture.state["operations"][0]["status"], "IDLE")
        self.assertEqual(self.fixture.connection.commits, 1)
        self.assertEqual(self.fixture.connection.rollbacks, 0)
        self.assertFalse(self.fixture.connection.locks)
        events = [json.loads(line)["event"] for line in self.fixture.audit_receipt_path.read_text().splitlines()]
        self.assertEqual(events, ["intent", "commit_unknown"])
        completed_state = copy.deepcopy(self.fixture.state)

        with self.assertRaisesRegex(ValueError, "must be new and exclusive"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, completed_state)
        self.assertEqual(self.fixture.connection.commits, 1)

    def test_rolled_back_commit_unknown_refuses_alternate_receipt_path(self):
        self.fixture.connection.fail_commit = True
        self.fixture.connection.fail_commit_rolls_back = True
        before = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(RuntimeError, "synthetic commit acknowledgement lost"):
            self.fixture.reconcile()

        self.assertEqual(self.fixture.state, before)
        self.assertEqual(self.fixture.connection.commits, 1)
        self.assertEqual(self.fixture.connection.rollbacks, 1)
        calls_before_retry = len(self.fixture.connection.calls)
        with self.assertRaisesRegex(ValueError, "fixed for the exact approved recovery incident"):
            self.fixture.reconcile(receipt_path=self.fixture.directory / "alternate-receipt.jsonl")
        self.assertEqual(len(self.fixture.connection.calls), calls_before_retry)
        self.assertEqual(self.fixture.state, before)
        self.assertEqual(self.fixture.connection.commits, 1)

    def test_publication_and_date_cas_rowcount_mismatches_rollback(self):
        for fail_at, message in (
            ("publication", "publication compare-and-swap"),
            ("dates", "publication-date compare-and-swap count"),
        ):
            with self.subTest(fail_at=fail_at):
                fixture = RecoveryFixture(Path(tempfile.mkdtemp(dir=self.fixture.directory)))
                fixture.connection.fail_at = fail_at
                before = copy.deepcopy(fixture.state)
                with self.assertRaisesRegex(RuntimeError, message):
                    fixture.reconcile()
                self.assertEqual(fixture.state, before)
                self.assertEqual(fixture.connection.rollbacks, 1)
                self.assertFalse(fixture.connection.locks)

    def test_existing_receipt_and_completed_reinvoke_fail_closed(self):
        self.fixture.reconcile()
        completed = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(ValueError, "must be new and exclusive"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, completed)
        with self.assertRaisesRegex(ValueError, "fixed for the exact approved recovery incident"):
            self.fixture.reconcile(receipt_path=self.fixture.directory / "second-receipt.jsonl")
        self.assertEqual(self.fixture.state, completed)
        self.assertEqual(self.fixture.connection.commits, 1)

    def test_external_manifest_files_must_be_private_single_link_files(self):
        audit = self.fixture.directory / "backup.audit"
        audit.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only 0600 single-link"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")


class Epoch5RecoveryTests(unittest.TestCase):
    def setUp(self):
        from finpay_pipeline.cell_publication_v2 import (
            STYLE_PALETTE,
            STYLE_REVISION,
            _expected_style,
            _verify_style as verify_style,
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.fixture = RecoveryFixture(
            Path(self.temp_dir.name), profile=CellRecoveryProfile.EPOCH5,
        )

        def probe_matrix(role, values):
            values = copy.deepcopy(values)
            matrix = {"layout_version": 2, "values": values, "section_ranges": []}
            if role == "summary":
                matrix.update({
                    "layout_version": 5,
                    "values": [values[0], ["", ""], ["", ""]],
                    "blocks": [],
                    "row_keys": ["HEADER", "HEADER", "CASH:cash:MANDIRI"],
                    "widths": [115, 100],
                    "frozen_rows": 2,
                    "frozen_columns": 3,
                })
            elif role == "invoice":
                matrix.update({
                    "values": [values[0], ["", ""], ["", ""]],
                    "panels": [{"start_column": 1, "end_column": 2}],
                    "widths": [110, 410],
                    "frozen_rows": 2,
                    "frozen_columns": 0,
                })
            else:
                matrix.update({
                    "values": [values[0], ["", ""]],
                    "headers": ["REPORT DATE", "CELL"],
                    "day_separator_rows": [2],
                    "widths": [110, 50],
                    "frozen_rows": 1,
                    "frozen_columns": 0,
                })
            matrix["style_contract"] = {
                "schema_version": 1,
                "revision": STYLE_REVISION,
                "role": role,
                "height": len(matrix["values"]),
                "width": 2,
                "widths": matrix.pop("widths"),
                "merges": [],
                "frozen_rows": matrix.pop("frozen_rows"),
                "frozen_columns": matrix.pop("frozen_columns"),
                "palette": dict(STYLE_PALETTE),
            }
            return matrix

        def color(hex_color):
            return {
                channel: int(hex_color[index:index + 2], 16) / 255
                for channel, index in (("red", 0), ("green", 2), ("blue", 4))
            }

        def entered_format(expected):
            raw = {
                "backgroundColor": color(expected["background"]),
                "textFormat": {
                    "foregroundColor": color(expected["foreground"]),
                    "fontFamily": expected["family"],
                    "fontSize": expected["size"],
                    "bold": expected["bold"],
                },
                "horizontalAlignment": expected["align"],
                "verticalAlignment": expected["vertical"],
                "wrapStrategy": expected["wrap"],
                "borders": {
                    side: {"style": style, **({"color": color(border_color)} if border_color else {})}
                    for side, (style, border_color) in expected["borders"].items()
                    if style != "NONE"
                },
            }
            if expected.get("number_type") is not None:
                raw["numberFormat"] = {"type": expected["number_type"]}
                if expected.get("number_pattern") is not None:
                    raw["numberFormat"]["pattern"] = expected["number_pattern"]
            return raw

        class StyleProbeAdapter:
            def __init__(self, matrix):
                self.matrix = matrix

            def read_style(self, rows, columns):
                matrix = self.matrix
                contract = matrix["style_contract"]
                height = len(matrix["values"])
                formats = [
                    [entered_format(_expected_style(matrix, row, column, None)) for column in range(columns)]
                    for row in range(height)
                ]
                yield {
                    "start": 0,
                    "grid": {
                        "frozenRowCount": contract["frozen_rows"],
                        "frozenColumnCount": contract["frozen_columns"],
                        "hideGridlines": True,
                    },
                    "merges": [],
                    "banding": [],
                    "default": {},
                    "widths": contract["widths"],
                    "heights": [21] * rows,
                    "formats": formats + [[{} for _ in range(columns)] for _ in range(rows - height)],
                    "entered": [[{} for _ in range(columns)] for _ in range(rows)],
                }

        self.real_style_roles = []

        def verify_style_side_effect(adapter, matrix, previous=None, *, allow_unattested=False):
            role = matrix["style_contract"]["role"]
            self.real_style_roles.append(role)
            probe = probe_matrix(role, matrix["values"])
            return verify_style(
                StyleProbeAdapter(probe), probe, previous, allow_unattested=allow_unattested,
            )

        self.style_patch = patch(
            "finpay_pipeline.cell_recovery._verify_style",
            side_effect=verify_style_side_effect,
        )
        self.style_mock = self.style_patch.start()
        self.addCleanup(self.style_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_epoch5_positive_reconciliation_reads_all_tabs_and_completes_32_pending_dates(self):
        self.assertEqual(self.fixture.state["publications"][0]["status"], "UNKNOWN")
        self.assertEqual(self.fixture.state["operations"][0]["status"], "UNKNOWN")
        self.assertEqual(len(self.fixture.state["dates"]), 32)
        self.assertEqual({row["status"] for row in self.fixture.state["dates"]}, {"PENDING"})

        result = self.fixture.reconcile()

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["epoch"], 5)
        self.assertEqual(result["dates_completed"], 32)
        self.assertEqual(self.fixture.state["publications"][0]["status"], "COMPLETE")
        self.assertEqual(self.fixture.state["publications"][0]["snapshot_fingerprint"],
                         self.fixture.approval.snapshot_fingerprint)
        self.assertEqual({row["status"] for row in self.fixture.state["dates"]}, {"COMPLETE"})
        self.assertEqual(self.fixture.state["operations"][0]["status"], "IDLE")
        self.assertEqual(self.fixture.connection.commits, 1)
        self.assertFalse(self.fixture.connection.locks)

        binding = json.loads(self.fixture.workbook_binding_path.read_text())
        markers = {
            item["worksheet_id"]: json.loads(item["developerMetadata"][0]["metadataValue"])
            for item in binding["sheets"] if item["developerMetadata"]
        }
        self.assertEqual(len(markers), 4)
        self.assertEqual(set(markers), {target.worksheet_id for target in self.fixture.targets})
        self.assertEqual({marker["status"] for marker in markers.values()}, {"COMPLETE"})
        self.assertEqual({marker["epoch"] for marker in markers.values()}, {5})
        self.assertEqual(
            {marker["fingerprint"] for marker in markers.values()},
            {self.fixture.approval.snapshot_fingerprint},
        )
        for adapter in self.fixture.adapters.values():
            self.assertEqual(adapter.worksheet.get_calls, 1)
            self.assertEqual(adapter.spreadsheet.write_calls, 0)
            self.assertEqual(set(adapter.spreadsheet.client.methods), {"get"})
        self.assertEqual(self.style_mock.call_count, 4)
        self.assertEqual(self.real_style_roles, list(ROLES))
        self.assertEqual(
            [json.loads(line)["event"] for line in self.fixture.audit_receipt_path.read_text().splitlines()],
            ["intent", "completed"],
        )

    def test_epoch5_profile_pins_are_separate_and_only_named_profiles_are_selectable(self):
        policy = recovery_module._policy_for_profile(CellRecoveryProfile.EPOCH5)
        epoch4 = recovery_module._policy_for_profile(CellRecoveryProfile.EPOCH4)
        self.assertEqual(policy.report_range, ("2026-10-01", "2026-11-01"))
        self.assertEqual(policy.epoch, 5)
        self.assertEqual(policy.cluster_id, "422010")
        self.assertEqual(policy.expected_error, "ReadTimeout")
        self.assertEqual(policy.operation_pin_sha256,
                         "7e63ec7d76235175d5b4eb94c3c93945c6596624f9657cff768ac873fe56cd39")
        self.assertEqual(policy.snapshot_fingerprint,
                         "e4497cd8174132351c2610644ec25945e565be75cc60206a4ec766ff0e8df10b")
        self.assertEqual(policy.failed_execution_id_sha256,
                         "505f24f66396617414c5c0ce317eafc3873265863e48e92e585130cbeab30aff")
        self.assertEqual(policy.operator_reference, "epoch5-owner-authorized-20261102")
        self.assertEqual(policy.failed_flow_revision, 6)
        self.assertEqual(dict(policy.scope_inputs), {
            "start_date": "2026-10-30",
            "end_date": "2026-11-01",
            "cluster_id": "ALL",
            "scrape_workers": 2,
            "dry_run": False,
            "write_gsheet": True,
        })
        self.assertEqual(policy.workbook_id_sha256, epoch4.workbook_id_sha256)
        self.assertEqual(policy.database_target, epoch4.database_target)
        self.assertEqual(policy.database_toc_count, 50)
        self.assertEqual(policy.database_listed_entry_count, 45)
        self.assertEqual(policy.audit_receipt_path,
                         Path("/tmp/kilo/finpay-epoch5-recovery-20261102/reconciliation-audit.jsonl"))
        self.assertNotEqual(policy.audit_receipt_path, epoch4.audit_receipt_path)
        self.assertEqual(set(recovery_module._RECOVERY_POLICIES), set(CellRecoveryProfile))
        with self.assertRaises(ValueError):
            recovery_module._policy_for_profile("epoch5")
        with self.assertRaises(TypeError):
            recovery_module._RECOVERY_POLICIES["epoch5"] = policy
        with self.assertRaises(AttributeError):
            policy.epoch = 4

    def test_public_api_rejects_arbitrary_profile_without_database_access(self):
        self.assertIs(
            inspect.Parameter.empty,
            inspect.signature(CellRecoveryApproval).parameters["profile"].default,
        )
        values = _approval_values(self.fixture.approval)
        values["profile"] = "synthetic-policy"
        approval = _raw_approval(values)
        with self.assertRaisesRegex(ValueError, "explicit supported CellRecoveryProfile"):
            reconcile_unknown_publication(
                approval=approval,
                connection=self.fixture.connection,
                adapters=self.fixture.adapters,
                backup_manifest_path=self.fixture.backup_manifest_path,
                quiescence_manifest_path=self.fixture.quiescence_manifest_path,
                audit_receipt_path=self.fixture.audit_receipt_path,
            )
        self.assertEqual(self.fixture.connection.calls, [])

    def test_public_api_rejects_dataclass_replaced_policy_and_has_no_private_policy_route(self):
        forged = replace(
            self.fixture.policy,
            audit_receipt_path=self.fixture.directory / "forged-receipt.jsonl",
        )
        with self.fixture.selected_policy():
            with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'policy'"):
                reconcile_unknown_publication(
                    approval=self.fixture.approval,
                    connection=self.fixture.connection,
                    adapters=self.fixture.adapters,
                    backup_manifest_path=self.fixture.backup_manifest_path,
                    quiescence_manifest_path=self.fixture.quiescence_manifest_path,
                    audit_receipt_path=forged.audit_receipt_path,
                    policy=forged,
                )
        self.assertFalse(hasattr(recovery_module, "_reconcile_unknown_publication_for_policy"))
        self.assertEqual(self.fixture.connection.calls, [])
        self.assertFalse(forged.audit_receipt_path.exists())

    def test_epoch5_approval_rejects_wrong_execution_operation_snapshot_and_range(self):
        cases = (
            ("failed_execution_id", "wrong-execution", "failed local Kestra execution"),
            ("operation_id", "b" * 32, "target-operation instance"),
            ("operation_started_at", self.fixture.started_at + timedelta(seconds=1),
             "target-operation instance"),
            ("snapshot_fingerprint", "f" * 64, "approved persisted snapshot"),
            ("range_start", "2026-10-02", "report-date range"),
            ("range_end", "2026-10-31", "report-date range"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, message):
                    self.fixture.validate_approval(**{field: value})

    def test_epoch5_execution_scope_rejects_wrong_inputs_and_flow_identity(self):
        scope_path = self.fixture.directory / "scope.json"
        valid_scope = json.loads(scope_path.read_text())
        self.fixture.verify_execution_scope(valid_scope)
        mutations = (
            ("start_date", lambda scope: scope["inputs"].update(start_date="2026-10-29")),
            ("end_date", lambda scope: scope["inputs"].update(end_date="2026-11-02")),
            ("cluster_id", lambda scope: scope["inputs"].update(cluster_id="422010")),
            ("scrape_workers", lambda scope: scope["inputs"].update(scrape_workers=1)),
            ("dry_run", lambda scope: scope["inputs"].update(dry_run=True)),
            ("write_gsheet", lambda scope: scope["inputs"].update(write_gsheet=False)),
            ("flow_revision", lambda scope: scope.update(flow_revision=5)),
            ("execution_id", lambda scope: scope.update(execution_id="other-execution")),
            ("namespace", lambda scope: scope.update(namespace="other.namespace")),
            ("cell_finance_emails", lambda scope: scope["variables"].update(
                cell_finance_emails=["other-finance@example.test"],
            )),
        )
        for name, mutate in mutations:
            with self.subTest(scope_field=name):
                changed = copy.deepcopy(valid_scope)
                mutate(changed)
                with self.assertRaisesRegex(ValueError, "approved local execution scope"):
                    self.fixture.verify_execution_scope(changed)

    def test_epoch5_keeps_publication_unknown_distinct_from_pending_dates(self):
        self.fixture.state["dates"][0]["status"] = "UNKNOWN"
        before = copy.deepcopy(self.fixture.state)
        with self.assertRaisesRegex(ValueError, "exact pending approved range"):
            self.fixture.reconcile()
        self.assertEqual(self.fixture.state, before)
        self.assertFalse(self.fixture.audit_receipt_path.exists())
        self.assertEqual(self.fixture.connection.commits, 0)

        pending_directory = Path(tempfile.mkdtemp(dir=self.fixture.directory))
        fixture = RecoveryFixture(pending_directory, profile=CellRecoveryProfile.EPOCH5)
        fixture.state["publications"][0]["status"] = "PENDING"
        with self.assertRaisesRegex(RuntimeError, "blocks recovery"):
            fixture.reconcile()
        self.assertEqual(fixture.state["dates"][0]["status"], "PENDING")
        self.assertFalse(fixture.audit_receipt_path.exists())

    def test_epoch5_commit_unknown_receipt_is_a_one_shot_retry_barrier(self):
        self.fixture.connection.fail_commit = True
        with self.assertRaisesRegex(RuntimeError, "synthetic commit acknowledgement lost"):
            self.fixture.reconcile()

        self.assertEqual(
            [json.loads(line)["event"] for line in self.fixture.audit_receipt_path.read_text().splitlines()],
            ["intent", "commit_unknown"],
        )
        self.assertEqual(self.fixture.state["publications"][0]["status"], "COMPLETE")
        self.assertEqual({row["status"] for row in self.fixture.state["dates"]}, {"COMPLETE"})
        self.assertEqual(self.fixture.state["operations"][0]["status"], "IDLE")
        self.assertFalse(self.fixture.connection.locks)
        calls_before_retry = len(self.fixture.connection.calls)
        with self.assertRaisesRegex(ValueError, "must be new and exclusive"):
            self.fixture.reconcile()
        self.assertEqual(len(self.fixture.connection.calls), calls_before_retry)


class ProductionRecoveryPinTests(unittest.TestCase):
    def test_production_policy_pins_remain_specific_to_the_approved_incident(self):
        self.assertEqual(recovery_module._APPROVED_RANGE, ("2026-10-01", "2026-10-29"))
        self.assertEqual(recovery_module._APPROVED_EPOCH, 4)
        self.assertEqual(recovery_module._APPROVED_ERROR, "ReadTimeout")
        self.assertEqual(recovery_module._APPROVED_CLUSTER_ID, "422010")
        self.assertEqual(
            recovery_module._APPROVED_LOCAL_DATABASE_TARGET,
            CellDatabaseTarget("postgres", 5432, "finpay", "public", "finpay"),
        )
        self.assertEqual(
            recovery_module._APPROVED_WORKBOOK_ID_SHA256,
            "bf9335230a04911dbf749c9d0fc8af83fa5b00c2aa838f1ccf549a619642f962",
        )
        self.assertEqual(
            recovery_module._APPROVED_OPERATION_PIN_SHA256,
            "397c0b951543e39a95c0d1ee1c1b23a3fcbeff8bcd60a1f6d40fac4b7eadf591",
        )
        self.assertEqual(
            recovery_module._APPROVED_SNAPSHOT_FINGERPRINT,
            "12169484bb133f8789680b41a93791348dafba096b9ac2a45e833b9cdb9825a3",
        )
        self.assertEqual(
            recovery_module._APPROVED_OWNER_SET_SHA256,
            "d2fd58394c2563fc98406d3b86bc78b35411eb6273ddd48e4b1844acec0bbfcf",
        )
        self.assertEqual(
            recovery_module._APPROVED_FINANCE_SET_SHA256,
            "145dc3ce54c60614f307ba83f93f1e7e995beadc338e57eb007914dd55c41542",
        )
        self.assertEqual(
            recovery_module._APPROVED_WRITER_SET_SHA256,
            "a184484faa26cfde4f710e1ef95f80088f3729f284031bbbb065d05d1d69aa44",
        )
        self.assertEqual(
            recovery_module._APPROVED_READER_SET_SHA256,
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        )
        self.assertEqual(
            recovery_module._APPROVED_FAILED_EXECUTION_ID_SHA256,
            "3053ae00c85f6a76048042b29cf10674a67b4423a8046c47527aeafff0145175",
        )
        self.assertEqual(
            recovery_module._APPROVED_OPERATOR_REFERENCE,
            "current-user-approved-guarded-oct29-recovery-20261030",
        )
        self.assertEqual(recovery_module._APPROVED_FAILED_FLOW_REVISION, 6)
        self.assertEqual(recovery_module._APPROVED_FINANCE_NAMESPACE, "finance.finpay")
        self.assertEqual(recovery_module._APPROVED_FINANCE_FLOW_ID, "finpay_cell_bulk_v1")
        self.assertEqual(recovery_module._APPROVED_WORKBOOK_SHEET_COUNT, 32)
        self.assertEqual(
            recovery_module._APPROVED_STOPPED_WRITERS,
            {"mmpp-finpay-replication-20261021-kestra-1"},
        )
        self.assertEqual(
            recovery_module._APPROVED_RUNNING_LOCAL_CONTAINERS,
            {"mmpp-finpay-replication-20261021-postgres-1"},
        )
        self.assertEqual(
            recovery_module._APPROVED_AUDIT_RECEIPT_PATH,
            Path("/tmp/kilo/finpay-oct29-recovery-20261102/reconciliation-audit.jsonl"),
        )
        self.assertEqual(recovery_module._APPROVED_DATABASE_TOC_COUNT, 50)
        self.assertEqual(recovery_module._APPROVED_DATABASE_LISTED_ENTRY_COUNT, 45)
        self.assertEqual(recovery_module._REQUIRED_CELL_BACKUP_TABLES, (
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
        ))
        self.assertEqual(
            recovery_module._REQUIRED_CELL_BACKUP_VIEWS,
            ("finpay_cell_current_results_v1",),
        )


if __name__ == "__main__":
    unittest.main()
