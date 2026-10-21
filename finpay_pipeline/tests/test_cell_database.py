"""Offline SQL/protocol tests with synthetic evidence and fake connections.

These exercise issued SQL and transaction boundaries, not PostgreSQL execution.
"""
import copy
import json
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import Mock

from finpay_pipeline.cell_contracts import configured_cells, fingerprint
from finpay_pipeline.cell_database import (
    CELL_MIGRATION, CellDatabaseTarget, CellStore, cell_publication_session,
    prepare_cell_day, validate_cluster_batch,
)


class Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.lock_allowed = True
        self.database_identity = ("dummy_finpay", "dummy_role", "dummy_schema")
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_ledger = False
        self.state = {"sources": {}, "ledger": [], "current": {}, "publications": {}, "dates": [], "operations": {}, "dirty": {}, "route_targets": []}

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.state)
        try:
            yield self
            self.commits += 1
        except Exception:
            self.state = before
            self.rollbacks += 1
            raise

    def execute(self, query, params=()):
        text = query if isinstance(query, str) else query.as_string()
        self.calls.append((text, params))
        if "current_database()" in text:
            return Result(self.database_identity)
        if "SELECT DISTINCT target_id" in text and "finpay_cell_publications_v1" in text:
            return Result(rows=[(target_id,) for target_id, status in self.state["route_targets"]
                                if status != "DB_ONLY"])
        if "pg_try_advisory_lock" in text:
            return Result((self.lock_allowed,))
        if text.startswith("SELECT operation_id"):
            operation = self.state["operations"].get(params[0])
            return Result((operation["id"],) if operation and operation["status"] != "IDLE" else None)
        if "INSERT INTO" in text and "finpay_cell_target_operations_v1" in text:
            operation = self.state["operations"].get(params[0])
            if operation and operation["status"] != "IDLE":
                return Result()
            self.state["operations"][params[0]] = {"id": params[2], "status": "IN_FLIGHT"}
            return Result((params[2],))
        if "UPDATE" in text and "finpay_cell_target_operations_v1" in text:
            operation = self.state["operations"].get(params[2])
            if not operation or operation["id"] != params[3] or operation["status"] != "IN_FLIGHT":
                return Result()
            operation["status"] = params[0]
            operation["error_class"] = params[1]
            return Result((operation["id"],))
        if text.startswith("SELECT generation_id,calculation_id"):
            selected = self.state["current"].get(params)
            return Result(tuple(selected) if selected else None)
        if "INSERT INTO" in text and "finpay_cell_dirty_dates_v1" in text:
            old = self.state["dirty"].get(params[:2], {})
            self.state["dirty"][params[:2]] = {"digest": params[2], "reset": params[3] or old.get("reset", False)}
        if text.startswith("SELECT report_date") and "finpay_cell_dirty_dates_v1" in text:
            return Result(rows=[(date.fromisoformat(day),) for (cluster, day), item in self.state["dirty"].items()
                                if cluster == params[0] and ("AND reset_required" not in text or item["reset"])])
        if text.startswith("DELETE FROM") and "finpay_cell_dirty_dates_v1" in text:
            if self.state["dirty"].get(params[:2], {}).get("digest") == params[2]:
                del self.state["dirty"][params[:2]]
        if "status IN ('IN_FLIGHT','UNKNOWN')" in text:
            unknown = [epoch for epoch, pub in self.state["publications"].items()
                       if pub["status"] in ("IN_FLIGHT", "UNKNOWN")]
            return Result((max(unknown),) if unknown else None)
        if "INSERT INTO" in text and "finpay_cell_sources_v1" in text:
            key = params[0]
            if key in self.state["sources"]:
                return Result()
            self.state["sources"][key] = {"raw_fingerprint": params[7], "rows": params[8]}
            return Result((key,))
        if text.startswith("SELECT raw_fingerprint"):
            stored = self.state["sources"][params[0]]
            return Result((stored["raw_fingerprint"], stored["rows"]))
        if "INSERT INTO" in text and "finpay_cell_ledger_v1" in text:
            if self.fail_ledger:
                raise RuntimeError("synthetic SQL failure")
            self.state["ledger"].append(params)
        if "INSERT INTO" in text and "finpay_cell_current_v1" in text:
            self.state["current"][params[:4]] = params[4:]
        if text.startswith("SELECT epoch,snapshot,status,snapshot_fingerprint"):
            available = {epoch: pub for epoch, pub in self.state["publications"].items()
                         if "status <> 'DB_ONLY'" not in text or pub["status"] != "DB_ONLY"}
            if not available:
                return Result()
            epoch = max(available)
            pub = available[epoch]
            return Result((epoch, pub["snapshot"], pub["status"], pub["fingerprint"]))
        if text.startswith("SELECT epoch,snapshot_fingerprint,status"):
            if not self.state["publications"]:
                return Result()
            epoch = max(self.state["publications"])
            pub = self.state["publications"][epoch]
            return Result((epoch, pub["fingerprint"], pub["status"]))
        if text.startswith("SELECT epoch,status"):
            if not self.state["publications"]:
                return Result()
            epoch = max(self.state["publications"])
            return Result((epoch, self.state["publications"][epoch]["status"]))
        if "INSERT INTO" in text and "finpay_cell_publications_v1" in text:
            self.state["publications"][params[2]] = {
                "fingerprint": params[5], "snapshot": json.loads(params[6]), "status": params[7],
            }
        if "INSERT INTO" in text and "finpay_cell_publication_dates_v1" in text:
            self.state["dates"].append(params)
        if "UPDATE" in text and "finpay_cell_publications_v1" in text:
            if "status='SUPERSEDED'" in text:
                for pub in self.state["publications"].values():
                    if pub["status"] == "PENDING":
                        pub["status"] = "SUPERSEDED"
            elif "status='IN_FLIGHT'" in text:
                self.state["publications"][params[2]]["status"] = "IN_FLIGHT"
            elif "status='UNKNOWN'" in text:
                self.state["publications"][params[3]]["status"] = "UNKNOWN"
            elif "status='PENDING'" in text:
                self.state["publications"][params[3]].update(status="PENDING", error_class=params[0])
            elif "status='COMPLETE'" in text:
                pub = self.state["publications"][params[2]]
                if pub["fingerprint"] != params[3]:
                    return Result()
                pub["status"] = "COMPLETE"
                return Result((params[2],))
        if text.startswith("SELECT snapshot,status"):
            pub = self.state["publications"].get(params[2])
            return Result((pub["snapshot"], pub["status"]) if pub else None)
        return Result((True,))

    def close(self):
        self.closed = True


class CellDatabaseTests(unittest.TestCase):
    def test_cluster_publication_route_cannot_fork_across_workbooks(self):
        target = CellDatabaseTarget("dummy.test", 5432, "dummy_finpay", "dummy_schema", "dummy_role")
        connection = FakeConnection()
        store = CellStore(connection, target, "412110", "new-workbook")
        store.locked = store.cluster_locked = True

        connection.state["route_targets"] = [("old-workbook", "COMPLETE")]
        with self.assertRaisesRegex(ValueError, "another workbook"):
            store.assert_cluster_target("new-workbook")
        with self.assertRaisesRegex(ValueError, "second workbook"):
            store.assert_cluster_target(None)

        connection.state["route_targets"] = [("new-workbook", "COMPLETE")]
        store.assert_cluster_target("new-workbook")
        connection.state["route_targets"] = [("database-only:412110", "DB_ONLY")]
        store.assert_cluster_target(None)

    def setUp(self):
        self.target = CellDatabaseTarget("dummy.test", 5432, "dummy_finpay", "dummy_schema", "dummy_role")
        self.cells = configured_cells({"412110_B": ("412111", "412112")})
        self.database = FakeConnection()
        self.factory = Mock(return_value=self.database)
        self.config = {"opening_date": "2026-10-13", "balances": {"412111": 1000, "412112": 2000}}

    def session(self, **kwargs):
        return cell_publication_session(
            self.target, {self.target}, "412110", "dummy-sheet-only",
            password="synthetic", approved_sheet_ids={"dummy-sheet-only"},
            connect=self.factory, **kwargs,
        )

    def prepared(self, identity, *, rows=2, digest="a" * 64):
        raw = [{"No": 99, "Transaction ID": "000123", "Transaction Date": "13-10-2026 12:00:00",
                "Saldo Awal": 1000, "Kredit": 100, "Debet": 0, "Saldo Akhir": 1100,
                "Remarks": "=literal source", "Transaction": "RECHARGE",
                "Transaction Type": "Credit", "Nomor RS": "0812345678"}] * rows
        source = {**identity.to_dict(), "report_date": "2026-10-13",
                  "validated_rows": rows, "footer_total": rows,
                  "status": "validated" if rows else "empty",
                  "artifact": "fixture.xlsx" if rows else None, "source_sha256": digest}
        return prepare_cell_day(identity, source, raw, {"summary": {"credit": 100 * rows}},
                                self.config, "dummy-rules-v1")

    def test_target_fail_closed_before_connection_and_no_legacy_defaults(self):
        for target, allowed in ((self.target, set()),
                                (CellDatabaseTarget("", 5432, "x", "public", "u"), {self.target})):
            with self.assertRaises(ValueError):
                with cell_publication_session(target, allowed, "412110", "dummy-sheet-only",
                                              password="synthetic", approved_sheet_ids=set(),
                                              connect=self.factory):
                    pass
        self.factory.assert_not_called()

    def test_dry_run_never_connects_even_without_live_config(self):
        with cell_publication_session(None, set(), "", "", password="",
                                      approved_sheet_ids=set(), dry_run=True,
                                      connect=self.factory) as store:
            self.assertIsNone(store)
        self.factory.assert_not_called()

    def test_lock_spans_commit_and_publication_until_context_exit(self):
        with self.session() as store:
            batch = [self.prepared(cell) for cell in self.cells]
            store.commit_cluster_day(batch, self.cells)
            staged = store.stage_publication("2026-10-13", "2026-10-14", {"dummy": True}, write_gsheet=True)
            store.record_manual_reset(staged["epoch"], "412111", "2026-10-13", 25, "=default")
            store.begin_sheet_write(staged["epoch"])
            store.confirm_chunk(staged["epoch"], "matrix-0")
            store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])
            self.assertFalse(any("pg_advisory_unlock" in q for q, _ in self.database.calls))
        self.assertTrue(self.database.closed)
        self.assertTrue(any("pg_advisory_unlock" in q for q, _ in self.database.calls))
        self.assertEqual(len(self.database.state["dates"]), 2)
        self.assertEqual(self.database.state["publications"][1]["status"], "COMPLETE")

    def test_duplicate_source_rows_retained_and_exact_retry_adds_no_ledger_rows(self):
        batch = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            store.commit_cluster_day(batch, self.cells)
            store.commit_cluster_day(batch, self.cells)
        self.assertEqual(len(self.database.state["sources"]), 2)
        self.assertEqual(len(self.database.state["ledger"]), 4)
        for row in self.database.state["ledger"]:
            self.assertEqual(row[2], "000123")
            self.assertNotIn("No", json.loads(row[4]))
        self.assertEqual(self.database.commits, 2)

    def test_correction_empty_replaces_only_its_cell_and_retains_old_generations(self):
        first = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            store.commit_cluster_day(first, self.cells)
            correction = [self.prepared(self.cells[0], rows=0, digest="b" * 64), first[1]]
            store.commit_cluster_day(correction, self.cells)
        current = self.database.state["current"]
        self.assertEqual(current[("412110_B", "412110", "412111", "2026-10-13")][0],
                         correction[0]["generation_id"])
        self.assertEqual(current[("412110_B", "412110", "412112", "2026-10-13")][0],
                         first[1]["generation_id"])
        self.assertEqual(len(self.database.state["sources"]), 3)
        self.assertEqual(len(self.database.state["ledger"]), 4)

    def test_incomplete_or_modified_batch_fails_before_sql(self):
        valid = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            before = len(self.database.calls)
            for batch in (valid[:1], [valid[0]] * 2):
                with self.assertRaises(ValueError):
                    store.commit_cluster_day(batch, self.cells)
            modified = copy.deepcopy(valid)
            modified[0]["calculation"]["summary"]["credit"] = 999
            with self.assertRaises(ValueError):
                validate_cluster_batch(modified, self.cells)
            self.assertEqual(len(self.database.calls), before)

    def test_transaction_rolls_back_raw_current_and_source_on_failure(self):
        self.database.fail_ledger = True
        with self.session() as store:
            with self.assertRaises(RuntimeError):
                store.commit_cluster_day([self.prepared(c) for c in self.cells], self.cells)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.state["sources"], {})
        self.assertEqual(self.database.state["current"], {})

    def test_render_failure_rolls_back_source_selection_and_publication_together(self):
        def fail_render(store):
            raise ValueError("synthetic render validation failure")

        with self.session() as store:
            with self.assertRaises(ValueError):
                store.commit_and_stage(
                    [self.prepared(cell) for cell in self.cells], self.cells,
                    fail_render, write_gsheet=True,
                )
        self.assertEqual(self.database.state["sources"], {})
        self.assertEqual(self.database.state["current"], {})
        self.assertEqual(self.database.state["publications"], {})

    def test_synthetic_target_never_uses_default_live_connection(self):
        with self.assertRaisesRegex(ValueError, "synthetic database targets"):
            with cell_publication_session(
                self.target, {self.target}, "412110", "dummy-sheet-only",
                password="", approved_sheet_ids={"dummy-sheet-only"},
            ):
                pass

    def test_unknown_prepublication_operation_blocks_target(self):
        with self.session() as store:
            store.begin_target_operation("synthetic-operation")
            store.finish_target_operation("synthetic-operation", unknown_error="TimeoutError")
        self.database.closed = False
        with self.assertRaisesRegex(RuntimeError, "setup/write is ambiguous"):
            with self.session():
                pass

    def test_same_checksum_with_conflicting_raw_evidence_is_rejected(self):
        first = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            store.commit_cluster_day(first, self.cells)
            different = [self.prepared(self.cells[0], rows=1), first[1]]
            with self.assertRaisesRegex(ValueError, "conflicting immutable"):
                store.commit_cluster_day(different, self.cells)
        self.assertEqual(len(self.database.state["ledger"]), 4)

    def test_balance_only_change_changes_calculation_but_not_source_generation(self):
        original = self.prepared(self.cells[0])
        self.config = {**self.config, "balances": {"412111": 1500, "412112": 2000}}
        corrected = self.prepared(self.cells[0])
        self.assertEqual(original["generation_id"], corrected["generation_id"])
        self.assertNotEqual(original["calculation_id"], corrected["calculation_id"])

    def test_config_only_dirty_dates_do_not_require_manual_resets(self):
        batch = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            store.commit_cluster_day(batch, self.cells)
            self.database.state["dirty"].clear()  # Simulate prior confirmed publication.
            self.config = {**self.config, "balances": {"412111": 1500, "412112": 2000}}
            corrected = [self.prepared(cell) for cell in self.cells]
            store.commit_cluster_day(corrected, self.cells)
            self.assertEqual(store.dirty_dates(), ["2026-10-13"])
            self.assertEqual(store.dirty_dates(reset_only=True), [])

    def test_database_only_dirty_date_survives_retry_until_matching_publication(self):
        batch = [self.prepared(cell) for cell in self.cells]
        with self.session() as store:
            store.commit_cluster_day(batch, self.cells)
            store.stage_publication("2026-10-13", "2026-10-13", {}, write_gsheet=False)
            self.assertEqual(store.dirty_dates(), ["2026-10-13"])
            store.commit_cluster_day(batch, self.cells)
            self.assertEqual(store.dirty_dates(), ["2026-10-13"])
            digest = fingerprint(sorted(item["calculation_id"] for item in batch))
            pub = store.stage_publication("2026-10-13", "2026-10-13",
                                          {"source_calculations": {"2026-10-13": digest}}, write_gsheet=True)
            store.begin_sheet_write(pub["epoch"])
            store.complete(pub["epoch"], verified_fingerprint=pub["fingerprint"])
            self.assertEqual(store.dirty_dates(), [])
            store.commit_cluster_day(batch, self.cells)
            self.assertEqual(store.dirty_dates(), [])

    def test_publication_idempotency_scope_and_supersession(self):
        with self.session() as store:
            one = store.stage_publication("2026-10-13", "2026-10-13", {"dummy": 1}, write_gsheet=True)
            same = store.stage_publication("2026-10-13", "2026-10-13", {"dummy": 1}, write_gsheet=True)
            self.assertEqual(one, same)
            two = store.stage_publication("2026-10-13", "2026-10-14", {"dummy": 1}, write_gsheet=True)
            self.assertNotEqual(one["fingerprint"], two["fingerprint"])
            self.assertEqual(two["epoch"], 2)
            with self.assertRaisesRegex(ValueError, "superseded"):
                store.begin_sheet_write(1)

    def test_acknowledged_validation_failure_retains_pending_error_not_unknown(self):
        with self.session() as store:
            store.begin_target_operation("known-validation")
            staged = store.stage_publication("2026-10-13", "2026-10-13", {}, write_gsheet=True)
            store.begin_sheet_write(staged["epoch"])
            store.mark_pending(staged["epoch"], "ValueError")
            store.finish_target_operation("known-validation", known_error="ValueError")
            self.assertEqual(self.database.state["publications"][staged["epoch"]]["status"], "PENDING")
            self.assertEqual(self.database.state["publications"][staged["epoch"]]["error_class"], "ValueError")
            operation = self.database.state["operations"]["dummy-sheet-only"]
            self.assertEqual((operation["status"], operation["error_class"]), ("IDLE", "ValueError"))
            with self.assertRaises(ValueError):
                store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])

    def test_unknown_write_blocks_new_session_and_never_marks_complete(self):
        with self.session() as store:
            staged = store.stage_publication("2026-10-13", "2026-10-13", {}, write_gsheet=True)
            store.begin_sheet_write(staged["epoch"])
            store.mark_unknown(staged["epoch"], "TimeoutError")
            with self.assertRaises(ValueError):
                store.complete(staged["epoch"], verified_fingerprint=staged["fingerprint"])
        self.database.closed = False
        with self.assertRaisesRegex(RuntimeError, "ambiguous publication"):
            with self.session():
                pass

    def test_lost_connection_stops_next_request(self):
        with self.session() as store:
            self.database.closed = True
            before = len(self.database.calls)
            with self.assertRaises(RuntimeError):
                store.begin_sheet_write(1)
            self.assertEqual(len(self.database.calls), before)

    def test_missing_sheet_target_does_not_block_explicit_database_only_session(self):
        with cell_publication_session(
            self.target, {self.target}, "412110", "", password="synthetic",
            approved_sheet_ids=set(), write_gsheet=False, connect=self.factory,
        ) as store:
            publication = store.stage_publication("2026-10-13", "2026-10-13", {}, write_gsheet=False)
            self.assertEqual(store.target_id, "database-only:412110")
            self.assertEqual(publication["status"], "DB_ONLY")
            with self.assertRaises(ValueError):
                store.begin_sheet_write(publication["epoch"])

    def test_wrong_server_identity_and_busy_lock_close_session(self):
        for identity, lock in ((('other', 'dummy_role', 'dummy_schema'), True),
                               (self.database.database_identity, False)):
            database = FakeConnection()
            database.database_identity = identity
            database.lock_allowed = lock
            with self.assertRaises((RuntimeError, ValueError)):
                with cell_publication_session(
                    self.target, {self.target}, "412110", "dummy-sheet-only", password="synthetic",
                    approved_sheet_ids={"dummy-sheet-only"}, connect=lambda **kw: database,
                ):
                    pass
            self.assertTrue(database.closed)

    def test_migration_is_additive_and_not_applied_by_runtime(self):
        ddl = CELL_MIGRATION.read_text()
        self.assertIn("PRIMARY KEY (account, cluster_id, cell_code, report_date)", ddl)
        self.assertIn("source_position", ddl)
        self.assertIn("IN_FLIGHT", ddl)
        self.assertIn("UNKNOWN", ddl)
        self.assertNotIn("ALTER TABLE finpay_", ddl)
        self.assertNotIn("DROP TABLE", ddl)
        self.assertNotIn("DELETE FROM", ddl)
        self.assertNotIn("DEFAULT TRUE", ddl)


if __name__ == "__main__":
    unittest.main()
