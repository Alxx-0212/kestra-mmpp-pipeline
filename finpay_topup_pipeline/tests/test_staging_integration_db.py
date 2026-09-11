import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from finpay_topup_pipeline.tests.conftest import assert_disposable_database
from finpay_topup_pipeline.audit import (
    PendingRefreshConflict,
    start_refresh,
    update_refresh_status,
    upsert_cluster_status,
)
from finpay_topup_pipeline.bucket import BucketTopupSnapshot
from finpay_topup_pipeline.classification import (
    active_outlets,
    classify_for_cluster,
    resolve_outlet,
    unclassified_kredit,
    upsert_classification,
)
from finpay_topup_pipeline.config import (
    dsn_from_env,
    TABLE_TXN,
    TABLE_TXN_STAGING,
    TABLE_CLASS,
    TABLE_OUTLET,
    TABLE_BALANCE,
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
    TABLE_BUCKET_SNAPSHOT,
)
from finpay_topup_pipeline.loader import (
    commit_staged_rows,
    load_inbox_staged,
    purge_staged_rows,
    row_hash,
    staged_row_count,
)
from finpay_topup_pipeline.refresh_service import DEFAULT_USERS
from finpay_topup_pipeline.legacy_outlet import outlet_code
from finpay_topup_pipeline.saldo import (
    current_saldo,
    current_saldo_at,
    current_saldo_before_date,
    update_opening_balance_from_latest,
)
from finpay_topup_pipeline.schema import ensure_schema
from finpay_topup_pipeline.verify import verify_bucket_topup_values
from finpay_topup_pipeline.review import approve_refresh, reject_refresh

CSV_HEADER = "No,Transaction Date,Sender,Receiver,Transaction Type,Amount,Currency,Remarks\n"


def _dsn():
    return os.environ.get("FINPAY_TOPUP_TEST_DSN", "")


def _write_inbox(root, cluster_id, rows):
    """rows: list of CSV data lines (without header)."""
    cluster_dir = Path(root) / cluster_id
    cluster_dir.mkdir(parents=True, exist_ok=True)
    csv_path = cluster_dir / "batch.csv"
    csv_path.write_text(CSV_HEADER + "".join(rows), encoding="utf-8")
    return csv_path


@unittest.skipIf(not _dsn(), "FINPAY_TOPUP_TEST_DSN not set")
class StagingIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg.connect(_dsn(), connect_timeout=10)
        assert_disposable_database(cls.conn)
        ensure_schema(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self._truncate()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    @classmethod
    def _truncate(cls):
        cls.conn.rollback()
        with cls.conn.cursor() as cur:
            cur.execute(
                f"TRUNCATE {TABLE_TXN}, {TABLE_TXN_STAGING}, {TABLE_CLASS}, {TABLE_BALANCE}, "
                f"{TABLE_REFRESH}, {TABLE_BUCKET_SNAPSHOT} RESTART IDENTITY CASCADE"
            )
        cls.conn.commit()

    def _seed_opening(self, cluster_id, as_of_date, opening):
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                f"VALUES (%s, %s, %s, %s)",
                (cluster_id, as_of_date, opening, "test opening"),
            )
        self.conn.commit()

    def _snapshot(self, username, cluster_id, value):
        return BucketTopupSnapshot(
            username,
            cluster_id,
            value,
            f"Bucket Top Up Rp {value:,}",
            datetime(2026, 8, 12, 6, 6, 59, tzinfo=timezone.utc),
            "https://digipos-cms.finpay.id/home",
        )

    def _stage_default_batch(self, refresh_id, extra_row=None):
        rows = [
            f'1,2026-08-11 09:00:00,,,Kredit,50000,IDR,setor kasir\n',
            f'2,2026-08-11 10:00:00,,,Debit,20000,IDR,top up sf\n',
        ]
        if extra_row:
            rows.append(extra_row)
        _write_inbox(self._tmp.name, "411311", rows)
        return load_inbox_staged(self.conn, self._tmp.name, refresh_id)

    def test_stage_first_match_commits_and_checkpoints(self):
        self._seed_opening("411311", "2026-08-10", 100000)
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        res = self._stage_default_batch(refresh_id)
        self.assertEqual(res["411311"], {"seen": 2, "staged": 2, "already_committed": 0})
        self.assertEqual(staged_row_count(self.conn, refresh_id), 2)

        # Computed saldo over committed+staged must be visible before commit...
        self.assertEqual(current_saldo(self.conn, "411311"), 100000)
        staged_saldo = current_saldo(self.conn, "411311", include_refresh_id=refresh_id)
        self.assertEqual(staged_saldo, 130000)

        verification = verify_bucket_topup_values(
            self.conn,
            refresh_id,
            [self._snapshot("411311_A", "411311", 130000)],
            cutoff_date=date(2026, 8, 12),
            include_refresh_id=refresh_id,
        )
        self.assertEqual(verification["status"], "VERIFIED")
        with self.conn.cursor() as cur:
            self.assertEqual(
                cur.execute(
                    f"SELECT status FROM {TABLE_REFRESH} WHERE refresh_id = %s",
                    (refresh_id,),
                ).fetchone()[0],
                "PENDING_REVIEW",
            )

        decision = approve_refresh(self.conn, refresh_id, "finance-reviewer")
        self.assertEqual(decision["status"], "COMMITTED")
        self.assertEqual(decision["inserted"], 2)
        with self.conn.cursor() as cur:
            reviewed = cur.execute(
                f"SELECT status, reviewed_by, reviewed_at FROM {TABLE_REFRESH} WHERE refresh_id = %s",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(reviewed[0], "COMMITTED")
        self.assertEqual(reviewed[1], "finance-reviewer")
        self.assertIsNotNone(reviewed[2])
        self.assertEqual(staged_row_count(self.conn, refresh_id), 0)
        self.assertEqual(current_saldo(self.conn, "411311"), 130000)

        # Historical closed window advances the next-day checkpoint.
        update_opening_balance_from_latest(self.conn)
        with self.conn.cursor() as cur:
            row = cur.execute(
                f"SELECT as_of_date, opening_balance FROM {TABLE_BALANCE} "
                f"WHERE cluster_id = %s AND as_of_date > '2026-08-10'",
                ("411311",),
            ).fetchone()
        self.assertEqual(str(row[0]), "2026-08-12")
        self.assertEqual(row[1], 130000)

    def test_forced_mismatch_retains_staging_until_rejection(self):
        self._seed_opening("411311", "2026-08-10", 100000)
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        self._stage_default_batch(refresh_id)

        verification = verify_bucket_topup_values(
            self.conn,
            refresh_id,
            [self._snapshot("411311_A", "411311", 140000)],
            cutoff_date=date(2026, 8, 12),
            report_date="2026-08-11",
            include_refresh_id=refresh_id,
        )
        self.assertEqual(verification["status"], "MISMATCH")
        self.assertEqual(staged_row_count(self.conn, refresh_id), 2)
        with self.conn.cursor() as cur:
            ledger_rows = cur.execute(f"SELECT COUNT(*) FROM {TABLE_TXN}").fetchone()[0]
            snapshot = cur.execute(
                f"SELECT status, computed_saldo, bucket_topup_value FROM {TABLE_BUCKET_SNAPSHOT} "
                f"WHERE cluster_id = '411311' AND report_date = '2026-08-11'"
            ).fetchone()
            refresh_status = cur.execute(
                f"SELECT status FROM {TABLE_REFRESH} WHERE refresh_id = %s", (refresh_id,)
            ).fetchone()[0]
        self.assertEqual(ledger_rows, 0)
        self.assertEqual(refresh_status, "PENDING_REVIEW")
        self.assertEqual(snapshot[0], "MISMATCH")
        # Opening 100000 + Kredit 50000 - Debit 20000 vs bucket 140000.
        self.assertEqual(snapshot[1], 130000)
        self.assertEqual(snapshot[2], 140000)
        self.assertEqual(verification["clusters"]["411311"]["verification_diff"], -10000.0)

        decision = reject_refresh(self.conn, refresh_id, "finance-reviewer")
        self.assertEqual(decision["status"], "REJECTED")
        self.assertEqual(decision["purged"], 2)
        with self.conn.cursor() as cur:
            reviewed = cur.execute(
                f"SELECT status, reviewed_by, reviewed_at FROM {TABLE_REFRESH} WHERE refresh_id = %s",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(reviewed[0], "REJECTED")
        self.assertEqual(reviewed[1], "finance-reviewer")
        self.assertIsNotNone(reviewed[2])
        self.assertEqual(staged_row_count(self.conn, refresh_id), 0)

    def test_all_cluster_approval_commits_verified_clusters_only(self):
        cluster_ids = [user[:-2] for user in DEFAULT_USERS]
        for cluster_id in cluster_ids:
            self._seed_opening(cluster_id, "2026-08-10", 100000)
        refresh_id = start_refresh(
            self.conn,
            "2026-08-11",
            "2026-08-11",
            list(DEFAULT_USERS),
            requested_by="plan-test",
            trigger_source="unittest",
            refresh_mode="AUTO_ALL",
            checkpoint_dates={cluster_id: "2026-08-10" for cluster_id in cluster_ids},
        )
        for index, cluster_id in enumerate(cluster_ids, start=1):
            _write_inbox(
                self._tmp.name,
                cluster_id,
                [f"1,2026-08-11 09:00:00,,,Kredit,{index * 100},IDR,setor kasir\n"],
            )
        load_inbox_staged(
            self.conn,
            self._tmp.name,
            refresh_id,
            cluster_ids=cluster_ids,
            checkpoint_dates={cluster_id: "2026-08-10" for cluster_id in cluster_ids},
        )
        for cluster_id in cluster_ids:
            upsert_cluster_status(
                self.conn,
                refresh_id,
                cluster_id,
                status="MISMATCH" if cluster_id == "411311" else "VERIFIED",
            )
        update_refresh_status(self.conn, refresh_id, "PENDING_REVIEW")

        decision = approve_refresh(self.conn, refresh_id, "finance-reviewer")

        self.assertEqual(decision["status"], "PENDING_REVIEW")
        self.assertTrue(decision["partial"])
        self.assertEqual(decision["committed_clusters"], cluster_ids[1:])
        self.assertEqual(decision["held_clusters"], ["411311"])
        self.assertEqual(decision["inserted"], 5)
        self.assertEqual(staged_row_count(self.conn, refresh_id), 1)
        with self.conn.cursor() as cur:
            ledger_rows = cur.execute(f"SELECT COUNT(*) FROM {TABLE_TXN}").fetchone()[0]
            statuses = dict(cur.execute(
                f"SELECT cluster_id, status FROM {TABLE_REFRESH_CLUSTER} WHERE refresh_id = %s",
                (refresh_id,),
            ).fetchall())
            checkpoints = dict(cur.execute(
                f"SELECT cluster_id, max(as_of_date) FROM {TABLE_BALANCE} "
                "GROUP BY cluster_id",
            ).fetchall())
        self.assertEqual(ledger_rows, 5)
        self.assertEqual(
            statuses,
            {cluster_id: "MISMATCH" if cluster_id == "411311" else "COMMITTED" for cluster_id in cluster_ids},
        )
        self.assertNotEqual(str(checkpoints["411311"]), "2026-08-11")
        for cluster_id in cluster_ids[1:]:
            self.assertEqual(str(checkpoints[cluster_id]), "2026-08-12")

        repeated = approve_refresh(self.conn, refresh_id, "another-reviewer")

        self.assertEqual(repeated["status"], "PENDING_REVIEW")
        self.assertEqual(repeated["decision"], "held_for_review")
        self.assertEqual(staged_row_count(self.conn, refresh_id), 1)

        rejected = reject_refresh(self.conn, refresh_id, "finance-reviewer")

        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["purged"], 1)
        self.assertEqual(staged_row_count(self.conn, refresh_id), 0)
        with self.conn.cursor() as cur:
            self.assertEqual(cur.execute(f"SELECT COUNT(*) FROM {TABLE_TXN}").fetchone()[0], 5)

    def test_current_day_approval_advances_same_day_opening_boundary(self):
        self._seed_opening("411311", "2026-08-10", 100000)
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH} SET requested_at = %s WHERE refresh_id = %s",
                (datetime(2026, 8, 11, 10, tzinfo=timezone.utc), refresh_id),
            )
        self.conn.commit()
        self._stage_default_batch(refresh_id)
        update_refresh_status(self.conn, refresh_id, "PENDING_REVIEW")
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH_CLUSTER} SET status = 'VERIFIED' WHERE refresh_id = %s",
                (refresh_id,),
            )
        self.conn.commit()

        decision = approve_refresh(self.conn, refresh_id, "finance-reviewer")

        self.assertEqual(decision["status"], "COMMITTED")
        with self.conn.cursor() as cur:
            checkpoint = cur.execute(
                f"SELECT as_of_date, opening_balance FROM {TABLE_BALANCE} "
                "WHERE cluster_id = %s ORDER BY as_of_date DESC LIMIT 1",
                ("411311",),
            ).fetchone()
        self.assertEqual(str(checkpoint[0]), "2026-08-11")
        self.assertEqual(checkpoint[1], 100000)

    def test_review_decisions_are_idempotent(self):
        self._seed_opening("411311", "2026-08-10", 100000)
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        self._stage_default_batch(refresh_id)
        update_refresh_status(self.conn, refresh_id, "PENDING_REVIEW")
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH_CLUSTER} SET status = 'VERIFIED' WHERE refresh_id = %s",
                (refresh_id,),
            )
        self.conn.commit()

        approved = approve_refresh(self.conn, refresh_id, "finance-reviewer")
        repeated = approve_refresh(self.conn, refresh_id, "another-reviewer")
        self.assertEqual(approved["status"], "COMMITTED")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["decision"], "approved")
        self.assertEqual(staged_row_count(self.conn, refresh_id), 0)

    def test_pending_refresh_scope_blocks_cluster_or_user_overlap(self):
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            trigger_source="unittest",
        )
        update_refresh_status(self.conn, refresh_id, "PENDING_REVIEW")
        with self.assertRaises(PendingRefreshConflict):
            start_refresh(
                self.conn, "2026-08-12", "2026-08-12", ["411311"],
                trigger_source="unittest",
            )
        other = start_refresh(
            self.conn, "2026-08-12", "2026-08-12", ["421306_A"],
            trigger_source="unittest",
        )
        self.assertNotEqual(other, refresh_id)
        update_refresh_status(self.conn, refresh_id, "FAILED")
        retry = start_refresh(
            self.conn, "2026-08-12", "2026-08-12", ["411311_A"],
            trigger_source="unittest",
        )
        self.assertNotEqual(retry, refresh_id)

    def test_nonterminal_refresh_scope_blocks_before_review(self):
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            trigger_source="unittest",
        )
        with self.assertRaises(PendingRefreshConflict):
            start_refresh(
                self.conn, "2026-08-12", "2026-08-12", ["411311"],
                trigger_source="unittest",
            )
        other = start_refresh(
            self.conn, "2026-08-12", "2026-08-12", ["421306_A"],
            trigger_source="unittest",
        )
        self.assertNotEqual(other, refresh_id)

    def test_restage_replaces_previous_batch_and_dedupes(self):
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        first = self._stage_default_batch(refresh_id)
        self.assertEqual(first["411311"]["staged"], 2)

        # Same batch restaged after a retry-extract: replace semantics keep 2.
        second = self._stage_default_batch(refresh_id)
        self.assertEqual(second["411311"]["staged"], 2)
        self.assertEqual(staged_row_count(self.conn, refresh_id), 2)

        # Late retry adds one more row to the same window.
        third = self._stage_default_batch(
            refresh_id, extra_row='3,2026-08-11 13:05:54,,,Debit,300000,IDR,top up balance via SF\n'
        )
        self.assertEqual(third["411311"]["staged"], 3)
        staged_saldo = current_saldo(self.conn, "411311", include_refresh_id=refresh_id)
        self.assertEqual(staged_saldo, -270000)

    def test_restage_dedupes_within_batch_and_against_ledger(self):
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        dup_row = '1,2026-08-11 09:00:00,,,Kredit,50000,IDR,setor kasir\n'
        _write_inbox(self._tmp.name, "411311", [dup_row, dup_row])
        res = load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        self.assertEqual(res["411311"]["staged"], 1)
        self.assertEqual(res["411311"]["seen"], 2)

        commit_staged_rows(self.conn, refresh_id)
        # Re-stage the identical file: hash now lives in the committed ledger.
        res2 = load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        self.assertEqual(res2["411311"]["already_committed"], 1)
        self.assertEqual(res2["411311"]["staged"], 0)

    def test_saldo_over_staged_respects_checkpoint_boundary(self):
        self._seed_opening("411311", "2026-08-11", 1000)
        committed_row = {
            "transaction_date": datetime(2026, 8, 11, 8, 0, 0),
            "sender": "",
            "receiver": "",
            "transaction_type": "Kredit",
            "amount": 100,
            "currency": "IDR",
            "remarks": "before staging",
        }
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_TXN} (cluster_id, transaction_date, sender, receiver, "
                f"transaction_type, amount, currency, remarks, source_file, row_hash) "
                f"VALUES ('411311', %s, '', '', 'Kredit', 100, 'IDR', 'before staging', "
                f"'unit.csv', %s)",
                ("2026-08-11 08:00:00", row_hash("411311", committed_row)),
            )
        self.conn.commit()
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            requested_by="plan-test", trigger_source="unittest",
        )
        _write_inbox(self._tmp.name, "411311", [
            '1,2026-08-11 08:00:00,,,Kredit,100,IDR,before staging\n',
            '2,2026-08-10 09:00:00,,,Kredit,999,IDR,pre-checkpoint noise\n',
            '3,2026-08-11 09:00:00,,,Kredit,5,IDR,staged movement\n',
        ])
        res = load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        # The CSV row duplicating the committed ledger row is dropped by hash.
        self.assertEqual(res["411311"]["already_committed"], 1)
        self.assertEqual(res["411311"]["staged"], 2)

        live = current_saldo(self.conn, "411311")
        self.assertEqual(live, 1100)
        over_staged = current_saldo(self.conn, "411311", include_refresh_id=refresh_id)
        self.assertEqual(over_staged, 1105)
        closed_day = current_saldo_before_date(
            self.conn, "411311", date(2026, 8, 11), include_refresh_id=refresh_id
        )
        self.assertEqual(closed_day, 1000)

    def test_timestamp_cutoff_includes_rows_at_cms_capture(self):
        self._seed_opening("411311", "2026-08-11", 1000)
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_TXN} "
                "(cluster_id, transaction_date, transaction_type, amount, row_hash) "
                "VALUES "
                "('411311', '2026-08-11 09:00:00+00', 'Kredit', 100, 'timestamp-a'), "
                "('411311', '2026-08-11 10:00:00+00', 'Debit', 25, 'timestamp-b')"
            )
        self.conn.commit()
        self.assertEqual(
            current_saldo_at(
                self.conn,
                "411311",
                datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
            ),
            1075,
        )

    def test_current_snapshot_verification_includes_staged_rows_at_cutoff(self):
        self._seed_opening("411311", "2026-08-11", 1000)
        refresh_id = start_refresh(
            self.conn, "2026-08-11", "2026-08-11", ["411311_A"],
            trigger_source="unittest",
        )
        _write_inbox(self._tmp.name, "411311", [
            "1,2026-08-11 10:00:00,,,Kredit,100,IDR,cms boundary\n",
        ])
        load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        snapshot_at = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        verification = verify_bucket_topup_values(
            self.conn,
            refresh_id,
            [BucketTopupSnapshot(
                "411311_A",
                "411311",
                1100,
                "Bucket Top Up Rp 1,100",
                snapshot_at,
                "https://digipos-cms.finpay.id/home",
            )],
            include_refresh_id=refresh_id,
        )
        self.assertEqual(verification["status"], "VERIFIED")
        self.assertEqual(staged_row_count(self.conn, refresh_id), 1)
        self.assertEqual(verification["clusters"]["411311"]["calculation_cutoff_at"], snapshot_at.isoformat())
        self.assertEqual(verification["clusters"]["411311"]["calculation_row_count"], 1)
        self.assertEqual(verification["clusters"]["411311"]["matching_row_number"], 1)
        self.assertEqual(verification["clusters"]["411311"]["matching_type"], "TRANSACTION")
        self.assertEqual(
            verification["clusters"]["411311"]["matching_row"]["remarks"],
            "cms boundary",
        )
        with self.conn.cursor() as cur:
            evidence = cur.execute(
                f"SELECT calculation_row_count, matching_row_number, matching_type, "
                f"matching_transaction_id, matching_transaction_at, matching_running_saldo "
                f"FROM {TABLE_REFRESH_CLUSTER} WHERE refresh_id = %s AND cluster_id = %s",
                (refresh_id, "411311"),
            ).fetchone()
        self.assertEqual(evidence[0:3], (1, 1, "TRANSACTION"))
        self.assertIsNotNone(evidence[3])
        self.assertEqual(evidence[4].astimezone(timezone.utc), datetime(2026, 8, 11, 2, tzinfo=timezone.utc))
        self.assertEqual(evidence[5], 1100)

    def test_historical_cutoff_persists_matching_transaction_evidence(self):
        self._seed_opening("411311", "2026-08-11", 1000)
        refresh_id = start_refresh(
            self.conn,
            "2026-08-11",
            "2026-08-11",
            ["411311_A"],
            trigger_source="unittest",
        )
        _write_inbox(self._tmp.name, "411311", [
            "1,2026-08-11 10:00:00,sender,receiver,Kredit,100,IDR,historical match\n",
        ])
        load_inbox_staged(self.conn, self._tmp.name, refresh_id)

        verification = verify_bucket_topup_values(
            self.conn,
            refresh_id,
            [self._snapshot("411311_A", "411311", 1100)],
            cutoff_date=date(2026, 8, 12),
            include_refresh_id=refresh_id,
        )

        cluster = verification["clusters"]["411311"]
        self.assertEqual(cluster["matching_type"], "TRANSACTION")
        self.assertEqual(cluster["matching_row_number"], 1)
        self.assertEqual(
            datetime.fromisoformat(cluster["matching_transaction_at"]).astimezone(timezone.utc),
            datetime(2026, 8, 11, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(cluster["calculation_cutoff_at"], "2026-08-11T16:00:00+00:00")

    def test_purge_is_isolated_per_refresh_id(self):
        r1 = start_refresh(self.conn, "2026-08-11", "2026-08-11", ["411311_A"], trigger_source="unittest")
        r2 = start_refresh(self.conn, "2026-08-11", "2026-08-11", ["421306_A"], trigger_source="unittest")
        with tempfile.TemporaryDirectory() as inbox_a:
            _write_inbox(inbox_a, "411311", ['1,2026-08-11 09:00:00,,,Kredit,10,IDR,a\n'])
            load_inbox_staged(self.conn, inbox_a, r1)
        with tempfile.TemporaryDirectory() as inbox_b:
            _write_inbox(inbox_b, "421306", ['1,2026-08-11 09:00:00,,,Kredit,20,IDR,b\n'])
            load_inbox_staged(self.conn, inbox_b, r2)
        purge_staged_rows(self.conn, r1)
        self.assertEqual(staged_row_count(self.conn, r1), 0)
        self.assertEqual(staged_row_count(self.conn, r2), 1)

    def test_outlet_enum_seeded_and_classification_upsert_idempotent(self):
        outlets = active_outlets(self.conn)
        self.assertEqual(len(outlets), 19)
        pky = outlet_code("411311", "PALANGKARAYA")
        gunung_mas = outlet_code("411311", "GUNUNG MAS")
        self.assertEqual(
            [o["label"] for o in active_outlets(self.conn, "411311")],
            ["GUNUNG MAS", "KATINGAN", "PALANGKARAYA"],
        )
        self.assertIsNotNone(resolve_outlet(self.conn, pky, cluster_id="411311"))
        self.assertIsNone(resolve_outlet(self.conn, "NOPE"))

        _write_inbox(self._tmp.name, "411311", ['1,2026-08-11 09:00:00,,,Kredit,10,IDR,x\n'])
        refresh_id = start_refresh(self.conn, "2026-08-11", "2026-08-11", ["411311_A"], trigger_source="unittest")
        load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        commit_staged_rows(self.conn, refresh_id)
        with self.conn.cursor() as cur:
            txn_id = cur.execute(f"SELECT txn_id FROM {TABLE_TXN} LIMIT 1").fetchone()[0]

        upsert_classification(self.conn, txn_id, category=None, by="alice", outlet_code=pky)
        upsert_classification(self.conn, txn_id, category=None, by="bob", outlet_code=pky)
        upsert_classification(self.conn, txn_id, category=None, by="bob", outlet_code=gunung_mas)
        with self.conn.cursor() as cur:
            rows = cur.execute(
                f"SELECT classified_by, outlet_code FROM {TABLE_CLASS} WHERE txn_id = %s",
                (txn_id,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "bob")
        self.assertEqual(rows[0][1], gunung_mas)

    def test_unclassified_kredit_listing_rules(self):
        _write_inbox(self._tmp.name, "411311", [
            '1,2026-08-01 09:00:00,,,Kredit,100,IDR,old kredit\n',
            '2,2026-08-20 09:00:00,,,Kredit,200,IDR,new kredit\n',
            '3,2026-08-21 09:00:00,,,Debit,50,IDR,debit ignored\n',
            '4,2026-08-22 09:00:00,,,Kredit,300,IDR,classify me\n',
        ])
        refresh_id = start_refresh(self.conn, "2026-08-25", "2026-08-25", ["411311_A"], trigger_source="unittest")
        load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        commit_staged_rows(self.conn, refresh_id)
        with self.conn.cursor() as cur:
            txn_ids = dict(
                cur.execute(f"SELECT remarks, txn_id FROM {TABLE_TXN}").fetchall()
            )
        upsert_classification(
            self.conn,
            txn_ids["classify me"],
            category=None,
            by="finance",
            outlet_code=outlet_code("411311", "PALANGKARAYA"),
        )
        listed = unclassified_kredit(self.conn, start_date="2026-07-01", end_date="2026-08-31")
        self.assertEqual([r["remarks"] for r in listed], ["old kredit", "new kredit"])
        recent = unclassified_kredit(self.conn, start_date="2026-08-15", end_date="2026-08-31")
        self.assertEqual([r["remarks"] for r in recent], ["new kredit"])
        limited = unclassified_kredit(self.conn, limit=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["amount"], 100.0)

    def test_cluster_filter_and_guarded_classification_are_isolated(self):
        _write_inbox(self._tmp.name, "411311", [
            '1,2026-08-22 09:00:00,,,Kredit,100,IDR,other cluster credit\n',
            '2,2026-08-22 10:00:00,,,Debit,50,IDR,other cluster debit\n',
        ])
        _write_inbox(self._tmp.name, "421318", [
            '1,2026-08-22 11:00:00,,,Kredit,200,IDR,target credit\n',
            '2,2026-08-22 12:00:00,,,Kredit,300,IDR,target second credit\n',
        ])
        refresh_id = start_refresh(
            self.conn,
            "2026-08-22",
            "2026-08-22",
            ["411311_A", "421318_A"],
            trigger_source="unittest",
        )
        load_inbox_staged(self.conn, self._tmp.name, refresh_id)
        commit_staged_rows(self.conn, refresh_id)

        target_rows = unclassified_kredit(self.conn, cluster_id="421318")
        self.assertEqual([row["remarks"] for row in target_rows], ["target credit", "target second credit"])
        target_id = target_rows[0]["txn_id"]
        second_id = target_rows[1]["txn_id"]

        bungku = outlet_code("421318", "BUNGKU")
        pky = outlet_code("411311", "PALANGKARAYA")
        assigned = classify_for_cluster(self.conn, target_id, "421318", bungku, "finance")
        self.assertEqual(assigned["code"], bungku)
        self.assertIsNone(
            classify_for_cluster(self.conn, target_id, "411311", pky, "finance")
        )
        self.assertIsNone(
            classify_for_cluster(self.conn, second_id, "421318", "NOT_AN_OUTLET", "finance")
        )
        with self.conn.cursor() as cur:
            debit_id = cur.execute(
                f"SELECT txn_id FROM {TABLE_TXN} WHERE cluster_id = '411311' AND transaction_type = 'Debit'"
            ).fetchone()[0]
        self.assertIsNone(
            classify_for_cluster(self.conn, debit_id, "411311", pky, "finance")
        )
        self.assertEqual(
            unclassified_kredit(self.conn, cluster_id="421318")[0]["remarks"],
            "target second credit",
        )

    def test_scoped_staging_ignores_other_cluster_directories(self):
        _write_inbox(self._tmp.name, "411311", [
            '1,2026-08-22 09:00:00,,,Kredit,100,IDR,other\n',
        ])
        _write_inbox(self._tmp.name, "421318", [
            '1,2026-08-22 09:00:00,,,Kredit,200,IDR,target\n',
        ])
        refresh_id = start_refresh(
            self.conn,
            "2026-08-22",
            "2026-08-22",
            ["421318_A"],
            refresh_mode="AUTO_CLUSTER",
            trigger_source="unittest",
            checkpoint_dates={"421318": "2026-08-22"},
        )

        result = load_inbox_staged(
            self.conn,
            self._tmp.name,
            refresh_id,
            cluster_ids=["421318"],
        )

        self.assertEqual(set(result), {"421318"})
        self.assertEqual(staged_row_count(self.conn, refresh_id), 1)

    def test_checkpoint_filter_excludes_rows_before_each_cluster_boundary(self):
        _write_inbox(self._tmp.name, "411311", [
            '1,2026-08-10 23:00:00,,,Kredit,100,IDR,before checkpoint\n',
            '2,2026-08-11 00:00:00,,,Debit,50,IDR,at checkpoint\n',
        ])
        refresh_id = start_refresh(
            self.conn,
            "2026-08-10",
            "2026-08-11",
            ["411311_A"],
            refresh_mode="AUTO_CLUSTER",
            checkpoint_dates={"411311": "2026-08-11"},
        )

        result = load_inbox_staged(
            self.conn,
            self._tmp.name,
            refresh_id,
            cluster_ids=["411311"],
            checkpoint_dates={"411311": "2026-08-11"},
        )

        self.assertEqual(result["411311"]["seen"], 2)
        self.assertEqual(result["411311"]["staged"], 1)
        self.assertEqual(staged_row_count(self.conn, refresh_id), 1)

    def test_selective_approval_does_not_advance_other_checkpoints(self):
        self._seed_opening("411311", "2026-08-10", 100000)
        self._seed_opening("421318", "2026-08-10", 200000)
        _write_inbox(self._tmp.name, "421318", [
            '1,2026-08-11 09:00:00,,,Kredit,500,IDR,target\n',
        ])
        refresh_id = start_refresh(
            self.conn,
            "2026-08-11",
            "2026-08-11",
            ["421318_A"],
            refresh_mode="AUTO_CLUSTER",
            checkpoint_dates={"421318": "2026-08-11"},
        )
        load_inbox_staged(
            self.conn,
            self._tmp.name,
            refresh_id,
            cluster_ids=["421318"],
            checkpoint_dates={"421318": "2026-08-11"},
        )
        update_refresh_status(self.conn, refresh_id, "PENDING_REVIEW")
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH_CLUSTER} SET status = 'VERIFIED' WHERE refresh_id = %s",
                (refresh_id,),
            )
        self.conn.commit()
        self.assertEqual(approve_refresh(self.conn, refresh_id, "reviewer")["status"], "COMMITTED")

        with self.conn.cursor() as cur:
            checkpoints = dict(cur.execute(
                f"SELECT cluster_id, max(as_of_date) FROM {TABLE_BALANCE} "
                "GROUP BY cluster_id"
            ).fetchall())
        self.assertEqual(str(checkpoints["411311"]), "2026-08-10")
        self.assertEqual(str(checkpoints["421318"]), "2026-08-12")


if __name__ == "__main__":
    unittest.main()
