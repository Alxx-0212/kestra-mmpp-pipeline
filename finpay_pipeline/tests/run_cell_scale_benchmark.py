"""Opt-in synthetic scale benchmark; requires the guarded disposable database.

The month workload covers reruns, byte-only changes, changed rows, and stale
configuration. The year workload ingests 10 million rows in monthly bundles.
Prepared members are synthetic and normalized; no source XLSX or Sheet is used.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import resource
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql

from finpay_pipeline import cell_database, cell_publication_v2
from finpay_pipeline import cell_calculation
from finpay_pipeline.cell_contracts import fingerprint, roster_fingerprint
from finpay_pipeline.cell_format import bulk_pipeline
from finpay_pipeline.cell_format import pipeline as daily
from finpay_pipeline.cell_format.settings import REPORTING_START_DATE, report_config
from finpay_pipeline.tests.scale_database_guard import (
    DATABASE_GUARD, USER_GUARD, validate_scale_test_dsn,
)

CLUSTER_ID = "412110"
SYNTHETIC_PASSWORD = "benchmark-only"
PLAN_PATH = Path(__file__).parents[2] / ".kilo" / "plans" / "1792939058856-finpay-10m-row-scale-plan.md"


class Metrics:
    def __init__(self):
        self.values = {
            "prepare_cell_day_calls": 0,
            "prepare_cell_day_raw_rows": 0,
            "deep_validation_batches": 0,
            "prepared_member_loads": 0,
            "rebind_batches": 0,
            "stale_days_recalculated": 0,
            "_prepare_seconds": 0.0,
            "_validate_seconds": 0.0,
            "_load_seconds": 0.0,
            "preflight_seconds": 0.0,
            "source_facts_seconds": 0.0,
            "rebind_seconds": 0.0,
            "commit_seconds": 0.0,
            "matrix_build_seconds": 0.0,
        }

    def wrap(self, owner, name, *, key=None, calls=None):
        original = getattr(owner, name)

        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = original(*args, **kwargs)
            finally:
                self.values[key or f"{name}_seconds"] += time.perf_counter() - started
                if calls:
                    calls(*args, **kwargs)
            return result

        setattr(owner, name, measured)
        return original

    def instrument(self):
        def count_prepare(identity, entry, raw_rows, *args, **kwargs):
            self.values["prepare_cell_day_calls"] += 1
            self.values["prepare_cell_day_raw_rows"] += len(raw_rows)

        def count_validation(batch, *args, **kwargs):
            self.values["deep_validation_batches"] += len(batch)

        def count_load(*args, **kwargs):
            self.values["prepared_member_loads"] += 1

        def count_rebind(*args, **kwargs):
            self.values["rebind_batches"] += 1

        for owner in (cell_database, cell_calculation, bulk_pipeline, daily):
            if hasattr(owner, "prepare_cell_day"):
                self.wrap(owner, "prepare_cell_day", key="_prepare_seconds", calls=count_prepare)
            if hasattr(owner, "validate_cluster_batch"):
                self.wrap(owner, "validate_cluster_batch", key="_validate_seconds", calls=count_validation)
        self.wrap(bulk_pipeline, "_load_bulk_prepared_member", key="_load_seconds", calls=count_load)
        self.wrap(bulk_pipeline, "_rebind_batch", key="rebind_seconds", calls=count_rebind)
        self.wrap(bulk_pipeline, "_preflight_prepared", key="preflight_seconds")
        self.wrap(bulk_pipeline, "_source_facts_for_cluster", key="source_facts_seconds")
        self.wrap(bulk_pipeline, "build_cell_cluster_matrices_v2", key="matrix_build_seconds")

        store_type = cell_database.CellStore
        self.wrap(store_type, "commit_cluster_day", key="commit_seconds")
        self.wrap(store_type, "_commit_prevalidated_cluster_day", key="commit_seconds")
        original_recalculate = bulk_pipeline.iter_recalculate_stale_history

        def measured_recalculate(*args, **kwargs):
            for cohort in original_recalculate(*args, **kwargs):
                self.values["stale_days_recalculated"] += 1
                yield cohort

        bulk_pipeline.iter_recalculate_stale_history = measured_recalculate


class PeakUsage:
    def __init__(self, root: Path, dsn: str):
        self.root = root
        self.dsn = dsn
        self.stop_event = threading.Event()
        self.peak_scratch_bytes = 0
        self.peak_postgres_temp_bytes = 0
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _scratch_bytes(self):
        total = 0
        for directory, _, files in os.walk(self.root):
            for name in files:
                try:
                    total += (Path(directory) / name).stat().st_size
                except FileNotFoundError:
                    pass
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(descriptor)
                if target.startswith(str(self.root)) and target.endswith(" (deleted)"):
                    total += descriptor.stat().st_size
            except (FileNotFoundError, PermissionError, OSError):
                pass
        return total

    def _sample(self):
        while not self.stop_event.wait(0.5):
            self.peak_scratch_bytes = max(self.peak_scratch_bytes, self._scratch_bytes())
            try:
                with psycopg.connect(self.dsn, hostaddr="127.0.0.1", connect_timeout=2) as connection:
                    size = connection.execute(
                        "SELECT COALESCE(sum(size), 0) FROM pg_ls_tmpdir()",
                    ).fetchone()[0]
                    self.peak_postgres_temp_bytes = max(self.peak_postgres_temp_bytes, size)
            except psycopg.Error:
                pass


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _dsn_and_identity():
    dsn = os.environ.get("FINPAY_SCALE_TEST_DSN", "")
    if not dsn:
        raise ValueError("FINPAY_SCALE_TEST_DSN must target the guarded disposable database")
    settings = validate_scale_test_dsn(dsn)
    with psycopg.connect(dsn, hostaddr="127.0.0.1", connect_timeout=5) as connection:
        database, user, version = connection.execute(
            "SELECT current_database(), current_user, current_setting('server_version_num')::int",
        ).fetchone()
    if (database, user, version) != (DATABASE_GUARD, USER_GUARD, 180004):
        raise ValueError("benchmark requires the guarded disposable PostgreSQL 18.4 database")
    return dsn, settings


def _initialize_schema(dsn: str) -> tuple[str, cell_database.CellDatabaseTarget]:
    schema = "scale_" + uuid.uuid4().hex[:16]
    with psycopg.connect(dsn, hostaddr="127.0.0.1", autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        migration = Path(__file__).parents[1] / "migrations" / "003_finpay_cell_reporting_v1.sql"
        connection.execute(migration.read_text(), prepare=False)
    target = cell_database.CellDatabaseTarget(
        "127.0.0.1", 5432, DATABASE_GUARD, schema, USER_GUARD,
    )
    return schema, target


def _connect_factory(dsn: str):
    def connect(**kwargs):
        return psycopg.connect(dsn, hostaddr="127.0.0.1", **kwargs)
    return connect


def _session_factory(dsn: str):
    connect = _connect_factory(dsn)

    def session(target, approved, cluster_id, target_id, **kwargs):
        return cell_database.cell_publication_session(
            target, approved, cluster_id, target_id, connect=connect, **kwargs,
        )
    return session


def _row(report_date: str, credit: int):
    transaction_id = "000123456789012345"
    return {
        "No": 1,
        "Transaction Date": report_date + " 12:00:00",
        "Transaction ID": transaction_id,
        "base_id": transaction_id,
        "transaction_id_type": "MAIN",
        "Saldo Awal": 1000,
        "Kredit": credit,
        "Debet": 0,
        "Saldo Akhir": 1000 + credit,
        "Transaction Type": "Credit",
        "Transaction": "RECHARGE",
        "Nomor RS": "0812345678",
        "Remarks": "Synthetic scale duplicate",
    }


def _rows(report_date: str, count: int, credit: int):
    template = _row(report_date, credit)
    rows = []
    for position in range(count):
        row = template.copy()
        row["No"] = position + 1
        if position > 1:
            seconds = position % 86_400
            row["Transaction Date"] = (
                report_date
                + f" {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
            )
            row["Transaction ID"] = f"synthetic-{position:012d}"
            row["base_id"] = row["Transaction ID"]
        rows.append(row)
    return rows


def _build_prepared_bundle(path: Path, start: date, end: date, total_rows: int, *,
                           generation: str, credit: int,
                           config_factory=report_config) -> dict:
    days = list(_date_range(start, end))
    cells = report_config(CLUSTER_ID, REPORTING_START_DATE).cells
    cohort_count = len(days) * len(cells)
    base, remainder = divmod(total_rows, cohort_count)
    prepared_entries = []
    prepared_total = 0

    with tempfile.TemporaryDirectory(prefix="finpay-scale-members-") as temporary:
        member_root = Path(temporary)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            ordinal = 0
            for day in days:
                config = config_factory(CLUSTER_ID, day)
                batch = []
                for identity in config.cells:
                    count = base + (ordinal < remainder)
                    ordinal += 1
                    source_sha = hashlib.sha256(
                        f"{generation}:{day}:{identity.cell_code}:{count}:{credit}".encode(),
                    ).hexdigest()
                    entry = {
                        **identity.to_dict(), "report_date": day, "status": "validated",
                        "footer_total": count, "validated_rows": count,
                        "artifact": f"{identity.account}-{identity.cell_code}.xlsx",
                        "source_sha256": source_sha,
                    }
                    raw_rows = _rows(day, count, credit)
                    # Compact report facts isolate raw-ledger persistence and validation cost.
                    calculation = {
                        "report_date": day, "processed": [], "deduplicated": [],
                        "summary_ready": [], "unusual": [], "qris": [],
                        "reversal": [], "summary": [], "raw_row_count": count,
                        "calculation_row_count": 0, "duplicate_row_count": 0,
                    }
                    batch.append(cell_database.prepare_cell_day(
                        identity, entry, raw_rows, calculation, config.to_dict(),
                        config.rules_version,
                    ))
                member = member_root / (day + ".json")
                digest, size = daily._write_json(member, {
                    "schema_version": 1, "report_date": day,
                    "cluster_id": CLUSTER_ID, "account": config.account,
                    "roster_version": config.roster_version,
                    "roster_fingerprint": roster_fingerprint(config.cells),
                    "batch": batch,
                }, daily.MAX_PREPARED_BYTES)
                prepared_total += size
                if prepared_total > bulk_pipeline.MAX_BULK_BYTES:
                    raise ValueError("synthetic prepared bundle exceeds the production bulk bound")
                member_path = bulk_pipeline._daily_prepared_path(CLUSTER_ID, day)
                archive.write(member, arcname=member_path)
                prepared_entries.append({
                    "cluster_id": CLUSTER_ID, "report_date": day,
                    "path": member_path, "sha256": digest,
                    "cells": len(batch), "bytes": size,
                })
            index = {
                "schema_version": 1, "start_date": start.isoformat(),
                "end_date": end.isoformat(), "cluster_id": CLUSTER_ID,
                "clusters": [CLUSTER_ID],
                "roster_version": "",
                "entries": prepared_entries,
            }
            # The workflow index uses the code-owned roster version, not source row metadata.
            from finpay_pipeline.cell_format.settings import ROSTER_VERSION
            index["roster_version"] = ROSTER_VERSION
            archive.writestr("index.json", json.dumps(index, sort_keys=True, separators=(",", ":")))
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "entries": len(prepared_entries),
            "prepared_uncompressed_bytes": prepared_total}


def _database_stats(dsn: str, schema: str):
    with psycopg.connect(dsn, hostaddr="127.0.0.1") as connection:
        rows = connection.execute(
            "SELECT c.relname, pg_total_relation_size(c.oid), pg_relation_size(c.oid), "
            "pg_indexes_size(c.oid), CASE WHEN c.reltoastrelid=0 THEN 0 "
            "ELSE pg_total_relation_size(c.reltoastrelid) END "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname", (schema,),
        ).fetchall()
        tables = {
            name: {"total_bytes": total, "heap_bytes": heap,
                   "index_bytes": indexes, "toast_bytes": toast}
            for name, total, heap, indexes, toast in rows
        }
        counts = {}
        for table in ("finpay_cell_sources_v1", "finpay_cell_ledger_v1",
                      "finpay_cell_calculations_v1", "finpay_cell_current_v1",
                      "finpay_cell_publications_v1", "finpay_cell_publication_dates_v1"):
            counts[table] = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table),
                ),
            ).fetchone()[0]
        no_rows = connection.execute(sql.SQL(
            "SELECT count(*) FROM {}.finpay_cell_ledger_v1 WHERE raw_row ? 'No'",
        ).format(sql.Identifier(schema))).fetchone()[0]
        snapshots = connection.execute(sql.SQL(
            "SELECT epoch, pg_column_size(snapshot), octet_length(snapshot::text) "
            "FROM {}.finpay_cell_publications_v1 ORDER BY epoch",
        ).format(sql.Identifier(schema))).fetchall()
        temp = connection.execute(
            "SELECT temp_files, temp_bytes FROM pg_stat_database WHERE datname=current_database()",
        ).fetchone()
        largest_generation = connection.execute(sql.SQL(
            "SELECT COALESCE(max(row_count), 0) FROM {}.finpay_cell_sources_v1",
        ).format(sql.Identifier(schema))).fetchone()[0]
        reset_dates = connection.execute(sql.SQL(
            "SELECT count(*) FROM {}.finpay_cell_dirty_dates_v1 WHERE reset_required",
        ).format(sql.Identifier(schema))).fetchone()[0]
        changed_fingerprints = connection.execute(sql.SQL("""
            SELECT count(*) FROM (
                SELECT report_date,cell_code
                FROM {}.finpay_cell_sources_v1
                GROUP BY report_date,cell_code
                HAVING count(DISTINCT raw_fingerprint) > 1
            ) AS changed_sources
        """).format(sql.Identifier(schema))).fetchone()[0]
    return {
        "counts": counts, "tables": tables, "ledger_rows_with_source_no": no_rows,
        "largest_source_generation_rows": largest_generation,
        "reset_required_dates": reset_dates,
        "cell_dates_with_changed_raw_fingerprints": changed_fingerprints,
        "snapshot_bytes_by_epoch": [
            {"epoch": epoch, "stored_jsonb_bytes": stored, "expanded_json_bytes": expanded}
            for epoch, stored, expanded in snapshots
        ],
        "postgres_temp_files": temp[0], "postgres_temp_bytes": temp[1],
    }


def _stage_snapshot(dsn: str, target, session_factory, end_date: str, request_id: str,
                    config_factory=report_config):
    with session_factory(
        target, {target}, CLUSTER_ID, "", password=SYNTHETIC_PASSWORD,
        approved_sheet_ids=set(), write_gsheet=False,
    ) as store:
        config = config_factory(CLUSTER_ID, REPORTING_START_DATE)
        facts = daily._facts(
            config, store.current_results(REPORTING_START_DATE, end_date),
        )
        matrices = cell_publication_v2.build_cell_cluster_matrices_v2(
            config, facts, end_date, dummy=True,
        )
        source_calculations = {
            day: fingerprint(sorted(
                item["calculation_id"] for item in facts if item["report_date"] == day
            )) for day in sorted({item["report_date"] for item in facts})
        }
        return store.stage_publication(
            REPORTING_START_DATE, end_date,
            {"schema_version": 1, "refresh_request_id": request_id,
             "matrices": matrices, "source_calculations": source_calculations},
            write_gsheet=False,
        )


def _persist(path: Path, built: dict, output: Path, target, session_factory):
    return bulk_pipeline.persist_cell_range(
        str(path), built["sha256"], database_password=SYNTHETIC_PASSWORD,
        output_path=str(output), target=target, session_factory=session_factory,
    )


def _metric_delta(before, after):
    return {key: round(after[key] - before[key], 3) if isinstance(after[key], float)
            else after[key] - before[key] for key in before}


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported benchmark output value: {type(value).__name__}")


@contextmanager
def _temporary_config(config_factory):
    original = daily._cell_config
    daily._cell_config = config_factory
    try:
        yield
    finally:
        daily._cell_config = original


def _scale_month(root: Path, dsn: str, schema: str, target, metrics: Metrics):
    session_factory = _session_factory(dsn)
    start, end = date(2026, 10, 1), date(2026, 10, 31)
    bundle = root / "month-prepared.zip"
    output = root / "month-handoff.zip"
    traces = []

    def run(label, generation, credit, config_factory=report_config, *, prepared_path=bundle,
            prepared=None, prepared_build_seconds=None, span=(start, end)):
        before_stats = _database_stats(dsn, schema)
        before_metrics = metrics.values.copy()
        if prepared is None:
            build_started = time.perf_counter()
            built = _build_prepared_bundle(
                prepared_path, span[0], span[1], 400_000,
                generation=generation, credit=credit, config_factory=config_factory,
            )
            build_elapsed = time.perf_counter() - build_started
        else:
            built = prepared
            build_elapsed = prepared_build_seconds or 0.0
        started = time.perf_counter()
        if config_factory is report_config:
            _persist(prepared_path, built, output, target, session_factory)
        else:
            with _temporary_config(config_factory):
                _persist(prepared_path, built, output, target, session_factory)
        persist_elapsed = time.perf_counter() - started
        snapshot_id = f"synthetic-{label}-{uuid.uuid4().hex[:8]}"
        snapshot_started = time.perf_counter()
        _stage_snapshot(dsn, target, session_factory, span[1].isoformat(), snapshot_id,
                        config_factory=config_factory)
        snapshot_elapsed = time.perf_counter() - snapshot_started
        after_stats = _database_stats(dsn, schema)
        new_generations = (after_stats["counts"]["finpay_cell_sources_v1"]
                           - before_stats["counts"]["finpay_cell_sources_v1"])
        selected_generation_count = built["entries"] * len(report_config(CLUSTER_ID, span[0].isoformat()).cells)
        traces.append({"stage": label, "build_seconds": round(build_elapsed, 3),
                       "persist_seconds": round(persist_elapsed, 3),
                       "snapshot_seconds": round(snapshot_elapsed, 3),
                       "source_generations_created": new_generations,
                       "source_generations_reused": max(0, selected_generation_count - new_generations),
                       "ledger_rows_inserted": after_stats["counts"]["finpay_cell_ledger_v1"]
                       - before_stats["counts"]["finpay_cell_ledger_v1"],
                       "postgres_storage": after_stats["tables"],
                       "metrics": _metric_delta(before_metrics, metrics.values),
                       "prepared": built})
        return built

    initial = run("first_ingest", "source-a", 100)
    first_stats = _database_stats(dsn, schema)
    # Model an already-published baseline before testing source-only replacement.
    with psycopg.connect(dsn, hostaddr="127.0.0.1", autocommit=True) as connection:
        connection.execute(sql.SQL(
            "UPDATE {}.finpay_cell_dirty_dates_v1 SET reset_required=false "
            "WHERE cluster_id=%s AND report_date BETWEEN %s AND %s",
        ).format(sql.Identifier(schema)), (CLUSTER_ID, start, end))
    run("exact_rerun", "source-a", 100, prepared=initial)
    run("byte_different_same_rows", "source-b", 100)
    after_same = _database_stats(dsn, schema)
    if after_same["counts"]["finpay_cell_ledger_v1"] - first_stats["counts"]["finpay_cell_ledger_v1"] != 400_000:
        raise AssertionError("byte-different source did not retain one new row generation")
    with psycopg.connect(dsn, hostaddr="127.0.0.1") as connection:
        reset_count = connection.execute(sql.SQL(
            "SELECT count(*) FROM {}.finpay_cell_dirty_dates_v1 "
            "WHERE cluster_id=%s AND reset_required",
        ).format(sql.Identifier(schema)), (CLUSTER_ID,)).fetchone()[0]
    if reset_count:
        raise AssertionError("byte-different, normalized-identical sources set correction resets")
    if after_same["cell_dates_with_changed_raw_fingerprints"]:
        raise AssertionError("byte-different source changed normalized raw fingerprints")

    run("changed_rows", "source-c", 200)
    changed_stats = _database_stats(dsn, schema)
    if changed_stats["counts"]["finpay_cell_ledger_v1"] - after_same["counts"]["finpay_cell_ledger_v1"] != 400_000:
        raise AssertionError("changed rows did not create a complete retained generation")
    if changed_stats["reset_required_dates"] != 31:
        raise AssertionError("changed rows did not mark every affected date for correction reset")
    if changed_stats["cell_dates_with_changed_raw_fingerprints"] != 31 * len(
        report_config(CLUSTER_ID, start.isoformat()).cells,
    ):
        raise AssertionError("changed rows did not alter every selected cell/day fingerprint")

    changed_config = lambda cluster, day: replace(
        report_config(cluster, day),
        opening_balances={
            code: balance + (1 if code == "412111" else 0)
            for code, balance in report_config(cluster, day).opening_balances.items()
        },
    )
    per_day_rows = 2 * (400_000 // (31 * 2))
    stale_path = root / "stale-prepared.zip"
    stale_build_started = time.perf_counter()
    stale = _build_prepared_bundle(
        stale_path, end, end, per_day_rows, generation="source-c", credit=200,
        config_factory=changed_config,
    )
    stale_build_elapsed = time.perf_counter() - stale_build_started
    run("stale_config", "source-c", 200, changed_config, prepared_path=stale_path,
        prepared=stale, prepared_build_seconds=stale_build_elapsed, span=(end, end))
    stats = _database_stats(dsn, schema)
    if stats["ledger_rows_with_source_no"]:
        raise AssertionError("source No leaked into the immutable ledger")
    return {"stages": traces, "after_first_ingest": first_stats, "final": stats}


def _scale_year(root: Path, dsn: str, schema: str, target, metrics: Metrics):
    session_factory = _session_factory(dsn)
    start = date.fromisoformat(REPORTING_START_DATE)
    end = start.replace(year=start.year + 1) - timedelta(days=1)
    total_days = (end - start).days + 1
    total_rows = 10_000_000
    stages = []
    allocated = 0
    current = start
    month_index = 0
    while current <= end:
        month_end = date(current.year, current.month,
                         calendar.monthrange(current.year, current.month)[1])
        month_end = min(month_end, end)
        days = (month_end - current).days + 1
        next_allocated = total_rows * ((month_end - start).days + 1) // total_days
        month_rows = next_allocated - allocated
        allocated = next_allocated
        prepared_path = root / f"year-{month_index:02d}-prepared.zip"
        output = root / f"year-{month_index:02d}-handoff.zip"
        before_stats = _database_stats(dsn, schema)
        before_metrics = metrics.values.copy()
        prepared_started = time.perf_counter()
        built = _build_prepared_bundle(
            prepared_path, current, month_end, month_rows, generation="year-source",
            credit=100,
        )
        build_elapsed = time.perf_counter() - prepared_started
        started = time.perf_counter()
        _persist(prepared_path, built, output, target, session_factory)
        persist_elapsed = time.perf_counter() - started
        snapshot_started = time.perf_counter()
        snapshot = _stage_snapshot(
            dsn, target, session_factory, month_end.isoformat(), f"year-{month_index}-{uuid.uuid4().hex[:8]}",
        )
        snapshot_elapsed = time.perf_counter() - snapshot_started
        after_stats = _database_stats(dsn, schema)
        new_generations = (after_stats["counts"]["finpay_cell_sources_v1"]
                           - before_stats["counts"]["finpay_cell_sources_v1"])
        stages.append({
            "month": current.strftime("%Y-%m"), "rows": month_rows, "days": days,
            "build_seconds": round(build_elapsed, 3), "persist_seconds": round(persist_elapsed, 3),
            "snapshot_seconds": round(snapshot_elapsed, 3),
            "publication_epoch": snapshot["epoch"], "prepared": built,
            "source_generations_created": new_generations,
            "source_generations_reused": 0,
            "ledger_rows_inserted": after_stats["counts"]["finpay_cell_ledger_v1"]
            - before_stats["counts"]["finpay_cell_ledger_v1"],
            "postgres_storage": after_stats["tables"],
            "metrics": _metric_delta(before_metrics, metrics.values),
        })
        prepared_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        current = month_end + timedelta(days=1)
        month_index += 1
    if allocated != total_rows:
        raise AssertionError("annual row allocation did not match 10 million")
    stats = _database_stats(dsn, schema)
    if stats["counts"]["finpay_cell_ledger_v1"] != total_rows:
        raise AssertionError("annual ledger row count differs from the 10 million target")
    expected_generations = sum(stage["prepared"]["entries"] for stage in stages) * len(
        report_config(CLUSTER_ID, start.isoformat()).cells,
    )
    if stats["counts"]["finpay_cell_sources_v1"] != expected_generations:
        raise AssertionError("annual source-generation count differs from complete cell/day coverage")
    return {"rows": total_rows, "stages": stages, "final": stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=("month", "year"), required=True)
    args = parser.parse_args()
    dsn, _ = _dsn_and_identity()
    schema, target = _initialize_schema(dsn)
    scratch = Path(tempfile.mkdtemp(prefix="finpay-cell-scale-")).resolve()
    tempfile.tempdir = str(scratch)
    usage = PeakUsage(scratch, dsn)
    metrics = Metrics()
    metrics.instrument()
    usage.start()
    started = time.perf_counter()
    try:
        result = (_scale_month(scratch, dsn, schema, target, metrics)
                  if args.workload == "month"
                  else _scale_year(scratch, dsn, schema, target, metrics))
    finally:
        usage.stop()
    elapsed = time.perf_counter() - started
    measured_stages = [item[key] for item in result["stages"]
                       for key in ("build_seconds", "persist_seconds", "snapshot_seconds")]
    within_six_hour_stage_limit = max(measured_stages, default=0) <= 6 * 60 * 60
    if not within_six_hour_stage_limit:
        raise AssertionError("a measured stage exceeded the six-hour runtime limit")
    plan_sha256 = hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest() if PLAN_PATH.is_file() else None
    print(json.dumps({
        "plan_sha256": plan_sha256,
        "workload": args.workload, "database_guard": DATABASE_GUARD,
        "postgres_version": "18.4", "psycopg_version": psycopg.__version__,
        "schema": schema, "elapsed_seconds": round(elapsed, 3),
        "peak_runner_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "peak_scratch_bytes": usage.peak_scratch_bytes,
        "peak_postgres_temp_bytes": usage.peak_postgres_temp_bytes,
        "within_six_hour_stage_limit": within_six_hour_stage_limit,
        "validation_and_rebind_metrics": metrics.values,
        "result": result,
    }, sort_keys=True, separators=(",", ":"), default=_json_default))


if __name__ == "__main__":
    main()
