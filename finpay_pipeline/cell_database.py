"""Explicit cell-aware persistence and publication session (no automatic DDL).

Methods require an already approved target. Offline tests inject a fake connection;
this module never obtains a legacy DSN or applies a migration as a write side effect.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .cell_contracts import (
    CellIdentity, business_date, fingerprint, validate_source_entry,
)

CELL_MIGRATION = Path(__file__).parent / "migrations/003_finpay_cell_reporting_v1.sql"
CELL_STATE_RETIREMENT_MIGRATION = (
    Path(__file__).parent / "migrations/004_finpay_cell_remove_publication_state.sql"
)


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


def safe_cell_database_error(exc: Exception) -> str:
    """Describe DB failures without returning libpq messages, DSNs, or SQL values."""
    state = getattr(exc, "sqlstate", None)
    state = state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) else "unavailable"
    message = str(exc).lower()
    category = next((label for needle, label in (
        ("password authentication failed", "authentication_failed"),
        ("could not translate host", "dns_failed"),
        ("name or service not known", "dns_failed"),
        ("temporary failure in name resolution", "dns_failed"),
        ("connection refused", "connection_refused"),
        ("timeout", "timeout"),
        ("does not exist", "missing_database_or_relation"),
        ("no pg_hba.conf", "pg_hba_rejected"),
        ("permission denied", "permission_denied"),
        ("preflight identity mismatch", "identity_mismatch"),
        ("preflight missing relation", "missing_relation"),
        ("preflight privileges", "permission_denied"),
    ) if needle in message), "database_operation_failed")
    return f"{type(exc).__name__}; category={category}; sqlstate={state}"


def preflight_cell_database(target: CellDatabaseTarget, approved_targets, *, password: str,
                            connect=None) -> dict:
    """Check the configured runtime role read-only, without DDL or advisory locks."""
    target.validate(approved_targets)
    if not password:
        raise ValueError("explicit cell database password is required")
    if connect is None:
        if any(word in target.host.lower() for word in ("dummy", "synthetic")):
            raise ValueError("synthetic database targets require an injected connection")
        import psycopg
        connect = psycopg.connect
    tables = {
        "finpay_cell_configs_v1": ("SELECT", "INSERT"),
        "finpay_cell_sources_v1": ("SELECT", "INSERT"),
        "finpay_cell_ledger_v1": ("SELECT", "INSERT"),
        "finpay_cell_calculations_v1": ("SELECT", "INSERT"),
        "finpay_cell_current_v1": ("SELECT", "INSERT", "UPDATE"),
        "finpay_cell_current_results_v1": ("SELECT",),
    }
    connection = None
    try:
        connection = connect(
            host=target.host, port=target.port, dbname=target.database,
            user=target.user, password=password, autocommit=True, connect_timeout=15,
            options="-c default_transaction_read_only=on",
        )
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(target.schema)))
        actual = connection.execute(
            "SELECT current_database(), current_user, current_schema()",
        ).fetchone()
        if tuple(actual or ()) != (target.database, target.user, target.schema):
            raise ValueError("cell database preflight identity mismatch")
        if not connection.execute("SELECT has_schema_privilege(%s,'USAGE')", (target.schema,)).fetchone()[0]:
            raise ValueError("cell database preflight privileges unavailable")
        for name, privileges in tables.items():
            relation = f"{target.schema}.{name}"
            if connection.execute("SELECT to_regclass(%s)", (relation,)).fetchone()[0] is None:
                raise ValueError("cell database preflight missing relation")
            for privilege in privileges:
                if not connection.execute(
                    "SELECT has_table_privilege(%s,%s)", (relation, privilege),
                ).fetchone()[0]:
                    raise ValueError("cell database preflight privileges unavailable")
        return {"status": "READY", "tables_checked": len(tables) - 1, "views_checked": 1}
    except Exception as exc:
        raise RuntimeError("cell database preflight failed: " + safe_cell_database_error(exc)) from None
    finally:
        if connection is not None:
            connection.close()


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


def _validate_cluster_batch_structure(
    batch: list[dict] | tuple[dict, ...], expected_cells: tuple[CellIdentity, ...],
) -> None:
    if not batch or not expected_cells:
        raise ValueError("complete cluster cell batch is required")
    if not isinstance(batch, (list, tuple)) or any(not isinstance(item, dict) for item in batch):
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


def validate_cluster_batch(batch: list[dict], expected_cells: tuple[CellIdentity, ...]) -> None:
    _validate_cluster_batch_structure(batch, expected_cells)
    # Recompute derived hashes and gates; callers cannot mutate a prepared batch.
    for item in batch:
        identity = CellIdentity(**item["identity"])
        recomputed = prepare_cell_day(
            identity, item["manifest"], item["raw_rows"], item["calculation"],
            item["configuration"], item["rules_version"],
        )
        if recomputed != item:
            raise ValueError("prepared cluster batch was modified")


_SPOOLED_BATCH_SEAL = object()


@dataclass(frozen=True)
class _PrevalidatedClusterBatch:
    items: tuple[dict, ...]
    expected_cells: tuple[CellIdentity, ...]
    spool_sha256: str
    seal: object


def _validated_cluster_batch_from_spool(
    batch: list[dict], expected_cells: tuple[CellIdentity, ...], spool_sha256: str,
) -> _PrevalidatedClusterBatch:
    if not re.fullmatch(r"[0-9a-f]{64}", spool_sha256):
        raise ValueError("validated cluster spool checksum is invalid")
    _validate_cluster_batch_structure(batch, expected_cells)
    return _PrevalidatedClusterBatch(
        tuple(batch), expected_cells, spool_sha256, _SPOOLED_BATCH_SEAL,
    )


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

    def assert_cluster_target(self, target_id: str | None) -> None:
        """A runtime guard for one configured writer; Sheet publication state is not stored."""
        self.assert_alive()
        if target_id != self.target_id:
            raise ValueError("cell session target differs from its acquired writer lock")

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
        return self._commit_cluster_day_items(batch, expected_cells)

    def _commit_prevalidated_cluster_day(self, batch: _PrevalidatedClusterBatch):
        """Commit a checksummed bulk spool batch without rescanning its raw rows."""
        if (not isinstance(batch, _PrevalidatedClusterBatch)
                or batch.seal is not _SPOOLED_BATCH_SEAL
                or not re.fullmatch(r"[0-9a-f]{64}", batch.spool_sha256)):
            raise ValueError("bulk persistence requires a verified cluster spool")
        return self._commit_cluster_day_items(batch.items, batch.expected_cells)

    def _commit_cluster_day_items(self, batch, expected_cells):
        _validate_cluster_batch_structure(batch, expected_cells)
        if expected_cells[0].cluster_id != self.cluster_id:
            raise ValueError("cluster batch differs from session lock scope")
        self.assert_alive()
        with self.connection.transaction():
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("finpay-cell-current-v1", self.cluster_id),
            )
            for item in batch:
                identity = item["identity"]
                old = self._execute(self._query(
                    "SELECT generation_id FROM {} WHERE account=%s AND cluster_id=%s "
                    "AND cell_code=%s AND report_date=%s", "finpay_cell_current_v1",
                ), (identity["account"], identity["cluster_id"], identity["cell_code"],
                    item["report_date"])).fetchone()
                if old and old[0] != item["generation_id"]:
                    old_source = self._execute(self._query(
                        "SELECT raw_fingerprint,row_count FROM {} WHERE generation_id=%s",
                        "finpay_cell_sources_v1",
                    ), (old[0],)).fetchone()
                    if old_source is None:
                        raise ValueError("current cell source evidence is missing")
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
                    self._copy_new_generation_rows(item)
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
        return [item["calculation_id"] for item in batch]

    def _copy_new_generation_rows(self, item):
        if not item["raw_rows"]:
            return
        with self.connection.cursor() as cursor:
            with cursor.copy(self._query(
                "COPY {} (generation_id,source_position,transaction_id,row_fingerprint,raw_row) "
                "FROM STDIN", "finpay_cell_ledger_v1",
            )) as copy:
                for position, row in enumerate(item["raw_rows"], 1):
                    copy.write_row((
                        item["generation_id"], position, row["Transaction ID"],
                        fingerprint(row), Jsonb(row),
                    ))

    def current_results(self, start: str, end: str) -> list[dict]:
        self.assert_alive()
        rows = self._execute(self._query(
            "SELECT account,cell_code,report_date,generation_id,calculation_id,config_id,result "
            "FROM {} WHERE cluster_id=%s AND report_date BETWEEN %s AND %s "
            "ORDER BY report_date,cell_code", "finpay_cell_current_results_v1",
        ), (self.cluster_id, business_date(start), business_date(end))).fetchall()
        return [dict(zip(("account", "cell_code", "report_date", "generation_id",
                          "calculation_id", "config_id", "result"), row)) for row in rows]

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

    def iter_current_source_rows(self, start, end):
        self.assert_alive()
        statement = sql.SQL(
            "SELECT source.manifest, COALESCE((SELECT jsonb_agg(ledger.raw_row "
            "ORDER BY ledger.source_position) FROM {ledger} AS ledger "
            "WHERE ledger.generation_id=source.generation_id), '[]'::jsonb) "
            "FROM {current} AS selected JOIN {sources} AS source ON selected.generation_id=source.generation_id "
            "WHERE selected.cluster_id=%s AND selected.report_date BETWEEN %s AND %s "
            "ORDER BY selected.report_date,selected.cell_code"
        ).format(**{key: sql.Identifier(self.target.schema, name) for key, name in {
            "current": "finpay_cell_current_v1", "sources": "finpay_cell_sources_v1",
            "ledger": "finpay_cell_ledger_v1",
        }.items()})
        params = (self.cluster_id, business_date(start), business_date(end))
        # A named cursor keeps raw history bounded to one cell/day cohort.
        with self.connection.transaction():
            with self.connection.cursor(name=f"cell_source_history_{uuid.uuid4().hex}") as cursor:
                cursor.execute(statement, params)
                while rows := cursor.fetchmany(1):
                    manifest, raw_rows = rows[0]
                    yield {"manifest": manifest, "raw_rows": raw_rows}

    def current_source_rows(self, start, end):
        return list(self.iter_current_source_rows(start, end))

@contextmanager
def cell_publication_session(
    target: CellDatabaseTarget, approved_targets, cluster_id: str, target_id: str,
    *, password: str, approved_sheet_ids, dry_run: bool = False,
    write_gsheet: bool = True, connect=None,
):
    """Hold transient DB advisory locks for one source or publication phase.

    Dry runs never call a connection factory; publication progress is not stored.
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
