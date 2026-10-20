"""Explicit cell-aware persistence and publication session (no automatic DDL).

Methods require an already approved target. Offline tests inject a fake connection;
this module never obtains a legacy DSN or applies a migration as a write side effect.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from psycopg import sql

from .cell_contracts import (
    CellIdentity, business_date, fingerprint, validate_source_entry,
)

CELL_MIGRATION = Path(__file__).parent / "migrations/003_finpay_cell_reporting_v1.sql"


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class CellDatabaseTarget:
    host: str
    port: int
    database: str
    schema: str
    user: str

    def validate(self, approved_targets) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (
            self.host, self.database, self.schema, self.user,
        )):
            raise ValueError("all cell database target fields must be explicit")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("explicit valid database port required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.schema):
            raise ValueError("invalid database schema identifier")
        if self not in approved_targets:
            raise ValueError("cell database target is not allowlisted")


def prepare_cell_day(identity: CellIdentity, entry: dict, raw_rows: list[dict],
                     calculation: dict, configuration: dict, rules_version: str) -> dict:
    """Produce immutable, content-addressed evidence without opening a database."""
    report_date = entry.get("report_date")
    validate_source_entry(entry, identity, report_date)
    if len(raw_rows) != entry["validated_rows"]:
        raise ValueError("raw row count differs from validated manifest")
    if not configuration or not isinstance(rules_version, str) or not rules_version.strip():
        raise ValueError("configuration and rules version are required")
    if not isinstance(calculation, dict):
        raise ValueError("cell calculation must be an explicit object")
    rows = []
    for row in raw_rows:
        cleaned = {key: value for key, value in row.items() if key != "No"}
        transaction_id = cleaned.get("Transaction ID")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("raw transaction ID must remain exact source text")
        for column in ("Saldo Awal", "Kredit", "Debet", "Saldo Akhir"):
            if type(cleaned.get(column)) is not int:
                raise ValueError(f"raw {column} must be a validated integer")
        timestamp = str(cleaned.get("Transaction Date") or "")
        parsed = None
        for pattern in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(timestamp, pattern)
                break
            except ValueError:
                continue
        if parsed is None or parsed.date() != business_date(report_date):
            raise ValueError("raw evidence outside report date")
        _json(cleaned)
        rows.append(cleaned)
    generation_id = fingerprint({
        **identity.to_dict(), "report_date": report_date,
        "source_sha256": entry["source_sha256"],
    })
    config_id = fingerprint(configuration)
    calculation_id = fingerprint({
        "generation_id": generation_id, "config_id": config_id,
        "rules_version": rules_version, "calculation": calculation,
    })
    return {
        "identity": identity.to_dict(), "report_date": report_date,
        "generation_id": generation_id, "source_sha256": entry["source_sha256"],
        "raw_fingerprint": fingerprint(rows), "manifest": entry, "raw_rows": rows,
        "config_id": config_id, "configuration": configuration,
        "rules_version": rules_version, "calculation_id": calculation_id,
        "calculation": calculation,
    }


def validate_cluster_batch(batch: list[dict], expected_cells: tuple[CellIdentity, ...]) -> None:
    if not batch or not expected_cells:
        raise ValueError("complete cluster cell batch is required")
    expected = [cell.to_dict() for cell in expected_cells]
    if len({cell.cluster_id for cell in expected_cells}) != 1:
        raise ValueError("a transaction may select only one cluster")
    actual = [item["identity"] for item in batch]
    if sorted(actual, key=_json) != sorted(expected, key=_json):
        raise ValueError("cluster batch has missing/duplicate/unknown cells")
    if len({item["report_date"] for item in batch}) != 1:
        raise ValueError("cluster batch mixes report dates")
    if len({item["config_id"] for item in batch}) != 1:
        raise ValueError("cluster batch mixes configuration versions")
    # Recompute derived hashes and gates; callers cannot mutate a prepared batch.
    for item in batch:
        identity = CellIdentity(**item["identity"])
        recomputed = prepare_cell_day(
            identity, item["manifest"], item["raw_rows"], item["calculation"],
            item["configuration"], item["rules_version"],
        )
        if recomputed != item:
            raise ValueError("prepared cluster batch was modified")


class CellStore:
    """One phase session holds target/cluster locks until its own work completes.

    The staged path persists sources in a DB-only session, then reacquires the
    locks in a separate publication session; the combined helper remains for the
    existing all-in-one caller.
    """

    def __init__(self, connection, target, cluster_id, target_id):
        self.connection = connection
        self.target = target
        self.cluster_id = cluster_id
        self.target_id = target_id
        self.locked = False
        self.cluster_locked = False

    def _query(self, statement: str, table: str):
        return sql.SQL(statement).format(sql.Identifier(self.target.schema, table))

    def _execute(self, statement, values=()):
        return self.connection.execute(statement, values)

    def assert_alive(self):
        if not self.locked or not self.cluster_locked or self.connection.closed:
            raise RuntimeError("cell workflow session lock is not available")
        self._execute("SELECT 1")

    def acquire(self):
        row = self._execute("SELECT current_database(), current_user, current_schema()").fetchone()
        if tuple(row or ()) != (self.target.database, self.target.user, self.target.schema):
            raise ValueError("connected cell database identity differs from approved target")
        # Serialize the whole spreadsheet, including independent cluster callers.
        acquired = self._execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            ("finpay-cell-publisher-v1", self.target_id),
        ).fetchone()
        if not acquired or acquired[0] is not True:
            raise RuntimeError("cell publication target is already locked")
        self.locked = True
        cluster_lock = self._execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            ("finpay-cell-cluster-v1", self.cluster_id),
        ).fetchone()
        if not cluster_lock or cluster_lock[0] is not True:
            raise RuntimeError("cell source/publication cluster is already locked")
        self.cluster_locked = True
        operation = self._execute(self._query(
            "SELECT operation_id FROM {} WHERE (target_id=%s OR cluster_id=%s) AND status <> 'IDLE'",
            "finpay_cell_target_operations_v1",
        ), (self.target_id, self.cluster_id)).fetchone()
        if operation:
            raise RuntimeError("target setup/write is ambiguous; operator reconciliation required")
        unknown = self._execute(self._query(
            "SELECT epoch FROM {} WHERE (target_id=%s OR cluster_id=%s) "
            "AND status IN ('IN_FLIGHT','UNKNOWN') LIMIT 1",
            "finpay_cell_publications_v1",
        ), (self.target_id, self.cluster_id)).fetchone()
        if unknown:
            raise RuntimeError("target has an ambiguous publication; operator reconciliation required")

    def release(self):
        if self.cluster_locked and not self.connection.closed:
            self._execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-cluster-v1", self.cluster_id),
            )
        self.cluster_locked = False
        if self.locked and not self.connection.closed:
            self._execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-publisher-v1", self.target_id),
            )
        self.locked = False

    def commit_cluster_day(self, batch: list[dict], expected_cells: tuple[CellIdentity, ...]):
        validate_cluster_batch(batch, expected_cells)
        if expected_cells[0].cluster_id != self.cluster_id:
            raise ValueError("cluster batch differs from session lock scope")
        self.assert_alive()
        with self.connection.transaction():
            changed = False
            reset_required = False
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-current-v1", self.cluster_id),
            )
            for item in batch:
                identity = item["identity"]
                old = self._execute(self._query(
                    "SELECT generation_id,calculation_id FROM {} WHERE account=%s AND cluster_id=%s "
                    "AND cell_code=%s AND report_date=%s", "finpay_cell_current_v1",
                ), (identity["account"], identity["cluster_id"], identity["cell_code"],
                    item["report_date"])).fetchone()
                changed = changed or not old or old[1] != item["calculation_id"]
                reset_required = reset_required or not old or old[0] != item["generation_id"]
                self._execute(self._query(
                    "INSERT INTO {} (config_id,configuration) VALUES (%s,%s::jsonb) "
                    "ON CONFLICT (config_id) DO NOTHING", "finpay_cell_configs_v1",
                ), (item["config_id"], _json(item["configuration"])))
                inserted = self._execute(self._query(
                    "INSERT INTO {} (generation_id,account,cluster_id,cell_code,parentcode,report_date,"
                    "source_sha256,raw_fingerprint,row_count,manifest) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (generation_id) DO NOTHING RETURNING generation_id",
                    "finpay_cell_sources_v1",
                ), (
                    item["generation_id"], identity["account"], identity["cluster_id"],
                    identity["cell_code"], identity["parentcode"], item["report_date"],
                    item["source_sha256"], item["raw_fingerprint"], len(item["raw_rows"]),
                    _json(item["manifest"]),
                )).fetchone()
                if inserted:
                    for position, row in enumerate(item["raw_rows"], 1):
                        self._execute(self._query(
                            "INSERT INTO {} (generation_id,source_position,transaction_id,"
                            "row_fingerprint,raw_row) VALUES (%s,%s,%s,%s,%s::jsonb)",
                            "finpay_cell_ledger_v1",
                        ), (item["generation_id"], position, row["Transaction ID"],
                            fingerprint(row), _json(row)))
                else:
                    existing = self._execute(self._query(
                        "SELECT raw_fingerprint,row_count FROM {} WHERE generation_id=%s",
                        "finpay_cell_sources_v1",
                    ), (item["generation_id"],)).fetchone()
                    if tuple(existing or ()) != (item["raw_fingerprint"], len(item["raw_rows"])):
                        raise ValueError("source checksum reused for conflicting immutable evidence")
                self._execute(self._query(
                    "INSERT INTO {} (calculation_id,generation_id,config_id,rules_version,result) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT (calculation_id) DO NOTHING",
                    "finpay_cell_calculations_v1",
                ), (item["calculation_id"], item["generation_id"], item["config_id"],
                    item["rules_version"], _json(item["calculation"])))
                self._execute(self._query(
                    "INSERT INTO {} (account,cluster_id,cell_code,report_date,generation_id,calculation_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (account,cluster_id,cell_code,report_date) "
                    "DO UPDATE SET generation_id=EXCLUDED.generation_id,"
                    "calculation_id=EXCLUDED.calculation_id,selected_at=CURRENT_TIMESTAMP",
                    "finpay_cell_current_v1",
                ), (identity["account"], identity["cluster_id"], identity["cell_code"],
                    item["report_date"], item["generation_id"], item["calculation_id"]))
            if changed:
                self._execute(self._query(
                    "INSERT INTO {} (cluster_id,report_date,calculation_fingerprint,reset_required) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (cluster_id,report_date) DO UPDATE SET "
                    "calculation_fingerprint=EXCLUDED.calculation_fingerprint,"
                    "reset_required=finpay_cell_dirty_dates_v1.reset_required OR EXCLUDED.reset_required,"
                    "changed_at=CURRENT_TIMESTAMP",
                    "finpay_cell_dirty_dates_v1",
                ), (self.cluster_id, batch[0]["report_date"],
                    fingerprint(sorted(item["calculation_id"] for item in batch)), reset_required))
        return [item["calculation_id"] for item in batch]

    def current_results(self, start: str, end: str) -> list[dict]:
        self.assert_alive()
        rows = self._execute(self._query(
            "SELECT account,cell_code,report_date,generation_id,calculation_id,config_id,result "
            "FROM {} WHERE cluster_id=%s AND report_date BETWEEN %s AND %s "
            "ORDER BY report_date,cell_code", "finpay_cell_current_results_v1",
        ), (self.cluster_id, business_date(start), business_date(end))).fetchall()
        return [dict(zip(("account", "cell_code", "report_date", "generation_id",
                          "calculation_id", "config_id", "result"), row)) for row in rows]

    def commit_and_stage(self, batch, expected_cells, render_committed, *, write_gsheet,
                         additional_batches=()):
        """Legacy combined commit/stage path; staged callers persist separately.

        The callback reads only this session's committed-candidate facts and
        performs no Google calls. The outer transaction finishes before sending
        anything to Sheets, while the session advisory lock remains held. New
        split flows use commit_cluster_day, then stage_publication after reacquiring
        their target/cluster locks and validating the persisted handoff.
        """
        self.assert_alive()
        cohorts = [*additional_batches, batch]
        for cohort in cohorts:
            validate_cluster_batch(cohort, expected_cells)
        cohorts.sort(key=lambda cohort: cohort[0]["report_date"])
        if len({cohort[0]["report_date"] for cohort in cohorts}) != len(cohorts):
            raise ValueError("duplicate cluster/date cohorts in atomic selection")
        for cohort in cohorts:
            if cohort[0]["config_id"] != batch[0]["config_id"]:
                raise ValueError("atomic selection mixes configuration versions")
        with self.connection.transaction():
            for cohort in cohorts:
                self.commit_cluster_day(cohort, expected_cells)
            render = render_committed(self)
            return self.stage_publication(
                render["start"], render["end"], render["snapshot"], write_gsheet=write_gsheet,
                refresh_audit_required=render.get("refresh_audit_required", False),
            )

    def stage_publication(self, start: str, end: str, snapshot: dict, *, write_gsheet: bool,
                          refresh_audit_required: bool = False):
        self.assert_alive()
        first, last = business_date(start), business_date(end)
        if first > last:
            raise ValueError("publication range is reversed")
        digest = fingerprint({
            "cluster_id": self.cluster_id, "target_id": self.target_id,
            "range_start": start, "range_end": end, "snapshot": snapshot,
        })
        with self.connection.transaction():
            previous = self._execute(self._query(
                "SELECT epoch,snapshot_fingerprint,status FROM {} WHERE cluster_id=%s "
                "AND target_id=%s ORDER BY epoch DESC LIMIT 1 FOR UPDATE",
                "finpay_cell_publications_v1",
            ), (self.cluster_id, self.target_id)).fetchone()
            if previous and previous[2] in ("IN_FLIGHT", "UNKNOWN"):
                raise RuntimeError("cannot supersede an ambiguous in-flight publication")
            if previous and previous[1] == digest:
                if previous[2] == "PENDING" or (previous[2] == "COMPLETE" and not refresh_audit_required) or (
                    previous[2] == "DB_ONLY" and not write_gsheet
                ):
                    return {"epoch": previous[0], "fingerprint": digest, "status": previous[2]}
            epoch = previous[0] + 1 if previous else 1
            self._execute(self._query(
                "UPDATE {} SET status='SUPERSEDED' WHERE cluster_id=%s AND target_id=%s "
                "AND status='PENDING'", "finpay_cell_publications_v1",
            ), (self.cluster_id, self.target_id))
            self._execute(self._query(
                "UPDATE {} SET status='SUPERSEDED' WHERE cluster_id=%s AND target_id=%s "
                "AND status='PENDING'", "finpay_cell_publication_dates_v1",
            ), (self.cluster_id, self.target_id))
            status = "PENDING" if write_gsheet else "DB_ONLY"
            self._execute(self._query(
                "INSERT INTO {} (cluster_id,target_id,epoch,range_start,range_end,"
                "snapshot_fingerprint,snapshot,status) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                "finpay_cell_publications_v1",
            ), (self.cluster_id, self.target_id, epoch, first, last, digest, _json(snapshot), status))
            day = first
            while day <= last:
                self._execute(self._query(
                    "INSERT INTO {} (cluster_id,target_id,epoch,report_date,status) "
                    "VALUES (%s,%s,%s,%s,%s)", "finpay_cell_publication_dates_v1",
                ), (self.cluster_id, self.target_id, epoch, day, status))
                day += timedelta(days=1)
        return {"epoch": epoch, "fingerprint": digest, "status": status}

    def publication_snapshot(self, epoch: int) -> dict:
        self.assert_alive()
        row = self._execute(self._query(
            "SELECT snapshot,status FROM {} WHERE cluster_id=%s AND target_id=%s AND epoch=%s",
            "finpay_cell_publications_v1",
        ), (self.cluster_id, self.target_id, epoch)).fetchone()
        if not row:
            raise ValueError("publication snapshot missing")
        return {"snapshot": row[0], "status": row[1]}

    def latest_publication(self, *, sheet_only=False):
        self.assert_alive()
        row = self._execute(self._query(
            "SELECT epoch,snapshot,status,snapshot_fingerprint FROM {} WHERE cluster_id=%s AND target_id=%s "
            + ("AND status <> 'DB_ONLY' " if sheet_only else "")
            + "ORDER BY epoch DESC LIMIT 1", "finpay_cell_publications_v1",
        ), (self.cluster_id, self.target_id)).fetchone()
        return {"epoch": row[0], "snapshot": row[1], "status": row[2],
                "fingerprint": row[3]} if row else None

    def latest_report_date(self):
        self.assert_alive()
        row = self._execute(self._query(
            "SELECT MAX(report_date) FROM {} WHERE cluster_id=%s", "finpay_cell_current_v1",
        ), (self.cluster_id,)).fetchone()
        return row[0].isoformat() if row and row[0] else None

    def earliest_report_date(self):
        self.assert_alive()
        row = self._execute(self._query(
            "SELECT MIN(report_date) FROM {} WHERE cluster_id=%s", "finpay_cell_current_v1",
        ), (self.cluster_id,)).fetchone()
        return row[0].isoformat() if row and row[0] else None

    def current_source_rows(self, start, end):
        self.assert_alive()
        statement = sql.SQL(
            "SELECT source.manifest, COALESCE(jsonb_agg(ledger.raw_row ORDER BY ledger.source_position) "
            "FILTER (WHERE ledger.source_position IS NOT NULL), '[]'::jsonb) "
            "FROM {current} AS selected JOIN {sources} AS source ON selected.generation_id=source.generation_id "
            "LEFT JOIN {ledger} AS ledger ON ledger.generation_id=source.generation_id "
            "WHERE selected.cluster_id=%s AND selected.report_date BETWEEN %s AND %s "
            "GROUP BY source.generation_id,source.manifest,selected.report_date,selected.cell_code "
            "ORDER BY selected.report_date,selected.cell_code"
        ).format(**{key: sql.Identifier(self.target.schema, name) for key, name in {
            "current": "finpay_cell_current_v1", "sources": "finpay_cell_sources_v1",
            "ledger": "finpay_cell_ledger_v1",
        }.items()})
        rows = self._execute(statement, (self.cluster_id, business_date(start), business_date(end))).fetchall()
        return [{"manifest": row[0], "raw_rows": row[1]} for row in rows]

    def dirty_dates(self, *, reset_only=False):
        self.assert_alive()
        return [row[0].isoformat() for row in self._execute(self._query(
            "SELECT report_date FROM {} WHERE cluster_id=%s "
            + ("AND reset_required " if reset_only else "") + "ORDER BY report_date",
            "finpay_cell_dirty_dates_v1",
        ), (self.cluster_id,)).fetchall()]

    def begin_target_operation(self, operation_id):
        self.assert_alive()
        self._execute(self._query(
            "INSERT INTO {} (target_id,cluster_id,operation_id,status) VALUES (%s,%s,%s,'IN_FLIGHT') "
            "ON CONFLICT (target_id) DO UPDATE SET cluster_id=EXCLUDED.cluster_id,operation_id=EXCLUDED.operation_id,"
            "status='IN_FLIGHT',error_class=NULL,started_at=CURRENT_TIMESTAMP "
            "WHERE finpay_cell_target_operations_v1.status='IDLE' RETURNING operation_id",
            "finpay_cell_target_operations_v1",
        ), (self.target_id, self.cluster_id, operation_id)).fetchone() or self._operation_conflict()

    @staticmethod
    def _operation_conflict():
        raise RuntimeError("target already has an unresolved operation")

    def finish_target_operation(self, operation_id, *, unknown_error=None, known_error=None):
        self.assert_alive()
        if unknown_error and known_error:
            raise ValueError("operation error cannot be both known and ambiguous")
        status = "UNKNOWN" if unknown_error else "IDLE"
        row = self._execute(self._query(
            "UPDATE {} SET status=%s,error_class=%s WHERE target_id=%s AND operation_id=%s "
            "AND status='IN_FLIGHT' RETURNING operation_id", "finpay_cell_target_operations_v1",
        ), (status, unknown_error or known_error, self.target_id, operation_id)).fetchone()
        if not row:
            raise RuntimeError("target operation identity/state mismatch")

    def _require_latest(self, epoch):
        row = self._execute(self._query(
            "SELECT epoch,status FROM {} WHERE cluster_id=%s AND target_id=%s "
            "ORDER BY epoch DESC LIMIT 1", "finpay_cell_publications_v1",
        ), (self.cluster_id, self.target_id)).fetchone()
        if not row or row[0] != epoch:
            raise ValueError("publication retry was superseded")
        return row[1]

    def record_manual_reset(self, epoch, cell_code, report_date, previous, replacement):
        self.assert_alive()
        if self._require_latest(epoch) != "PENDING":
            raise ValueError("manual reset audit requires latest pending publication")
        self._execute(self._query(
            "INSERT INTO {} (cluster_id,target_id,epoch,cell_code,report_date,previous_value,"
            "replacement_value) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
            "ON CONFLICT DO NOTHING", "finpay_cell_manual_resets_v1",
        ), (self.cluster_id, self.target_id, epoch, cell_code, business_date(report_date),
            _json(previous), _json(replacement)))

    def begin_sheet_write(self, epoch):
        self.assert_alive()
        if self._require_latest(epoch) != "PENDING":
            raise ValueError("Sheet write requires a latest pending publication")
        self._execute(self._query(
            "UPDATE {} SET status='IN_FLIGHT' WHERE cluster_id=%s AND target_id=%s AND epoch=%s",
            "finpay_cell_publications_v1",
        ), (self.cluster_id, self.target_id, epoch))

    def mark_unknown(self, epoch, error_class: str):
        self.assert_alive()
        if self._require_latest(epoch) != "IN_FLIGHT":
            raise ValueError("only an in-flight publication may become unknown")
        self._execute(self._query(
            "UPDATE {} SET status='UNKNOWN',error_class=%s "
            "WHERE cluster_id=%s AND target_id=%s AND epoch=%s",
            "finpay_cell_publications_v1",
        ), (error_class, self.cluster_id, self.target_id, epoch))

    def mark_pending(self, epoch, error_class: str):
        """All issued mutations acknowledged; verification failed, not outcome-unknown."""
        self.assert_alive()
        if self._require_latest(epoch) not in ("PENDING", "IN_FLIGHT"):
            raise ValueError("only an unfinished publication may remain pending")
        self._execute(self._query(
            "UPDATE {} SET status='PENDING',error_class=%s "
            "WHERE cluster_id=%s AND target_id=%s AND epoch=%s", "finpay_cell_publications_v1",
        ), (error_class, self.cluster_id, self.target_id, epoch))

    def confirm_chunk(self, epoch, chunk_id: str):
        self.assert_alive()
        if self._require_latest(epoch) != "IN_FLIGHT":
            raise ValueError("chunk confirmation requires an in-flight publication")
        self._execute(self._query(
            "UPDATE {} SET confirmed_chunks=confirmed_chunks || %s::jsonb "
            "WHERE cluster_id=%s AND target_id=%s AND epoch=%s",
            "finpay_cell_publications_v1",
        ), (_json([chunk_id]), self.cluster_id, self.target_id, epoch))

    def complete(self, epoch, *, verified_fingerprint: str):
        self.assert_alive()
        if self._require_latest(epoch) != "IN_FLIGHT":
            raise ValueError("only an in-flight publication may complete")
        with self.connection.transaction():
            updated = self._execute(self._query(
                "UPDATE {} SET status='COMPLETE',confirmed_at=CURRENT_TIMESTAMP "
                "WHERE cluster_id=%s AND target_id=%s AND epoch=%s AND snapshot_fingerprint=%s "
                "RETURNING epoch", "finpay_cell_publications_v1",
            ), (self.cluster_id, self.target_id, epoch, verified_fingerprint)).fetchone()
            if not updated:
                raise ValueError("readback fingerprint does not match committed publication")
            self._execute(self._query(
                "UPDATE {} SET status='COMPLETE' WHERE cluster_id=%s AND target_id=%s AND epoch=%s",
                "finpay_cell_publication_dates_v1",
            ), (self.cluster_id, self.target_id, epoch))
            snapshot = self.publication_snapshot(epoch)["snapshot"]
            for report_date, digest in snapshot.get("source_calculations", {}).items():
                self._execute(self._query(
                    "DELETE FROM {} WHERE cluster_id=%s AND report_date=%s AND calculation_fingerprint=%s",
                    "finpay_cell_dirty_dates_v1",
                ), (self.cluster_id, report_date, digest))


@contextmanager
def cell_publication_session(
    target: CellDatabaseTarget, approved_targets, cluster_id: str, target_id: str,
    *, password: str, approved_sheet_ids, dry_run: bool = False,
    write_gsheet: bool = True, connect=None,
):
    """Only this context spans DB commit and Google confirmation in one process.

    Dry runs never call a connection factory. An unresolved IN_FLIGHT/UNKNOWN
    target blocks normal retries; operator recovery must establish quiescence.
    """
    if dry_run:
        yield None
        return
    target.validate(approved_targets)
    if not re.fullmatch(r"\d{6}", cluster_id):
        raise ValueError("explicit cluster ID required")
    if write_gsheet and (not target_id or target_id not in approved_sheet_ids):
        raise ValueError("explicit allowlisted spreadsheet ID required")
    if not write_gsheet:
        target_id = "database-only:" + cluster_id
    if connect is None:
        if target.host.endswith(".test"):
            raise ValueError("synthetic database targets cannot use a live connection factory")
        import psycopg
        connect = psycopg.connect
    connection = connect(
        host=target.host, port=target.port, dbname=target.database, user=target.user,
        password=password, connect_timeout=15, autocommit=True,
    )
    store = CellStore(connection, target, cluster_id, target_id)
    try:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(target.schema)))
        store.acquire()
        yield store
    finally:
        try:
            store.release()
        finally:
            connection.close()
