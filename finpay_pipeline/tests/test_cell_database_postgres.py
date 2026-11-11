"""Opt-in PostgreSQL integration tests for COPY and transaction atomicity."""
from __future__ import annotations

import os
import unittest
import uuid

import psycopg
from psycopg import sql

from finpay_pipeline.cell_database import (
    CELL_MIGRATION, CellDatabaseTarget, cell_publication_session, prepare_cell_day,
)
from finpay_pipeline.cell_format.settings import report_config
from finpay_pipeline.tests.scale_database_guard import (
    DATABASE_GUARD, USER_GUARD, validate_scale_test_dsn,
)

DSN_ENV = "FINPAY_CELL_COPY_TEST_DSN"
CLUSTER = "412110"


@unittest.skipUnless(os.environ.get(DSN_ENV), f"requires a dedicated disposable DB in {DSN_ENV}")
class CellDatabaseCopyPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ[DSN_ENV]
        validate_scale_test_dsn(cls.dsn)
        with psycopg.connect(cls.dsn, hostaddr="127.0.0.1") as connection:
            database, user, version = connection.execute(
                "SELECT current_database(), current_user, current_setting('server_version_num')::int",
            ).fetchone()
        if (database, user, version) != (DATABASE_GUARD, USER_GUARD, 180004):
            raise RuntimeError("COPY integration tests require the guarded disposable PostgreSQL 18.4 database")

        cls.schema = "copy_test_" + uuid.uuid4().hex[:16]
        with psycopg.connect(cls.dsn, hostaddr="127.0.0.1", autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(cls.schema)))
            connection.execute(CELL_MIGRATION.read_text(), prepare=False)
        cls.target = CellDatabaseTarget(
            "127.0.0.1", 5432, DATABASE_GUARD, cls.schema, USER_GUARD,
        )

    @classmethod
    def _connect(cls, **kwargs):
        return psycopg.connect(cls.dsn, hostaddr="127.0.0.1", **kwargs)

    def _session(self):
        return cell_publication_session(
            self.target, {self.target}, CLUSTER, "", password="benchmark-only",
            approved_sheet_ids=set(), write_gsheet=False, connect=self._connect,
        )

    def _batch(self, report_date: str, digest: str):
        config = report_config(CLUSTER, report_date)
        batch = []
        for identity in config.cells:
            rows = [{
                "No": 1,
                "Transaction Date": f"{report_date[8:10]}-{report_date[5:7]}-{report_date[:4]} 12:00:00",
                "Transaction ID": "000123456789012345",
                "Saldo Awal": 1000, "Kredit": 100, "Debet": 0, "Saldo Akhir": 1100,
                "Transaction Type": "Credit", "Transaction": "RECHARGE",
                "Nomor RS": "0812345678", "Remarks": "Synthetic duplicate row",
            }, {
                "No": 2,
                "Transaction Date": f"{report_date[8:10]}-{report_date[5:7]}-{report_date[:4]} 12:00:00",
                "Transaction ID": "000123456789012345",
                "Saldo Awal": 1000, "Kredit": 100, "Debet": 0, "Saldo Akhir": 1100,
                "Transaction Type": "Credit", "Transaction": "RECHARGE",
                "Nomor RS": "0812345678", "Remarks": "Synthetic duplicate row",
            }]
            entry = {
                **identity.to_dict(), "report_date": report_date, "status": "validated",
                "footer_total": len(rows), "validated_rows": len(rows),
                "artifact": f"{identity.account}-{identity.cell_code}.xlsx",
                "source_sha256": digest,
            }
            calculation = {
                "report_date": report_date, "summary": [], "unusual": [],
                "reversal": [], "qris": [],
            }
            batch.append(prepare_cell_day(
                identity, entry, rows, calculation, config.to_dict(), config.rules_version,
            ))
        return config, batch

    def test_copy_preserves_duplicate_positions_and_exact_rerun_is_idempotent(self):
        config, batch = self._batch("2026-10-13", "a" * 64)
        copy_calls = []
        with self._session() as store:
            original_copy = store._copy_new_generation_rows

            def count_copy(item):
                copy_calls.append(item["generation_id"])
                return original_copy(item)

            store._copy_new_generation_rows = count_copy
            store.commit_cluster_day(batch, config.cells)
            store.commit_cluster_day(batch, config.cells)
            streamed_sources = list(store.iter_current_source_rows("2026-10-13", "2026-10-13"))

        self.assertEqual(len(copy_calls), len(config.cells))
        self.assertEqual(len(streamed_sources), len(config.cells))
        self.assertTrue(all(len(source["raw_rows"]) == 2 for source in streamed_sources))
        with psycopg.connect(self.dsn, hostaddr="127.0.0.1") as connection:
            rows_by_generation = []
            for item in batch:
                rows_by_generation.append(connection.execute(sql.SQL(
                    "SELECT source_position,transaction_id,row_fingerprint,raw_row "
                    "FROM {}.finpay_cell_ledger_v1 WHERE generation_id=%s ORDER BY source_position",
                ).format(sql.Identifier(self.schema)), (item["generation_id"],)).fetchall())
            raw_rows_with_no = connection.execute(sql.SQL(
                "SELECT count(*) FROM {}.finpay_cell_ledger_v1 WHERE raw_row ? 'No'",
            ).format(sql.Identifier(self.schema))).fetchone()[0]
            source_count = connection.execute(sql.SQL(
                "SELECT count(*) FROM {}.finpay_cell_sources_v1 WHERE report_date=%s",
            ).format(sql.Identifier(self.schema)), ("2026-10-13",)).fetchone()[0]

        self.assertEqual(source_count, len(config.cells))
        self.assertEqual(raw_rows_with_no, 0)
        self.assertEqual(len(rows_by_generation), len(config.cells))
        for rows in rows_by_generation:
            self.assertEqual([row[0] for row in rows], [1, 2])
            self.assertEqual([row[1] for row in rows], ["000123456789012345"] * 2)
            self.assertEqual(rows[0][2], rows[1][2])
            self.assertEqual(rows[0][3], rows[1][3])

    def test_failure_during_copy_rolls_back_source_ledger_calculation_and_current(self):
        report_date = "2026-10-14"
        config, batch = self._batch(report_date, "b" * 64)
        function = sql.SQL("""
            CREATE FUNCTION {}.reject_second_copy_row() RETURNS trigger
            LANGUAGE plpgsql AS $body$
            BEGIN
                IF NEW.cluster_id = '412110' AND NEW.report_date = DATE '2026-10-14'
                   AND NEW.source_position = 2 THEN
                    RAISE EXCEPTION 'synthetic COPY failure';
                END IF;
                RETURN NEW;
            END;
            $body$
        """).format(sql.Identifier(self.schema))
        trigger = sql.SQL(
            "CREATE TRIGGER reject_second_copy_row BEFORE INSERT ON {}.finpay_cell_ledger_v1 "
            "FOR EACH ROW EXECUTE FUNCTION {}.reject_second_copy_row()",
        ).format(sql.Identifier(self.schema), sql.Identifier(self.schema))
        with psycopg.connect(self.dsn, hostaddr="127.0.0.1", autocommit=True) as connection:
            connection.execute(function)
            connection.execute(trigger)

        with self._session() as store:
            with self.assertRaises(psycopg.Error):
                store.commit_cluster_day(batch, config.cells)

        with psycopg.connect(self.dsn, hostaddr="127.0.0.1") as connection:
            counts = connection.execute(sql.SQL("""
                SELECT
                    (SELECT count(*) FROM {}.finpay_cell_sources_v1 WHERE report_date=%s),
                    (SELECT count(*) FROM {}.finpay_cell_ledger_v1 WHERE generation_id = ANY(%s)),
                    (SELECT count(*) FROM {}.finpay_cell_calculations_v1 WHERE generation_id = ANY(%s)),
                    (SELECT count(*) FROM {}.finpay_cell_current_v1 WHERE report_date=%s)
            """).format(*[sql.Identifier(self.schema)] * 4), (
                report_date, [item["generation_id"] for item in batch],
                [item["generation_id"] for item in batch], report_date,
            )).fetchone()
        self.assertEqual(tuple(counts), (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
