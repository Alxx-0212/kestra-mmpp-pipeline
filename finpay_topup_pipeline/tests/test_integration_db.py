import os
import unittest
from datetime import datetime, timezone

import psycopg

from finpay_topup_pipeline.tests.conftest import assert_disposable_database
from finpay_topup_pipeline.config import (
    dsn_from_env,
    TABLE_TXN,
    TABLE_TXN_STAGING,
    TABLE_CLASS,
    TABLE_BALANCE,
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
    TABLE_BUCKET_SNAPSHOT,
)
from finpay_topup_pipeline.schema import ensure_schema
from finpay_topup_pipeline.loader import load_rows, row_hash, load_inbox
from finpay_topup_pipeline.saldo import current_saldo, summary_by_cluster, update_opening_balance_from_latest
from finpay_topup_pipeline.classification import upsert_classification
from finpay_topup_pipeline.backfill import backfill_xlsx
from finpay_topup_pipeline.reconcile import reconcile_xlsx
from finpay_topup_pipeline.io_xlsx import parse_morowali_xlsx
from finpay_topup_pipeline.audit import start_refresh
from finpay_topup_pipeline.bucket import BucketTopupSnapshot
from finpay_topup_pipeline.verify import verify_bucket_topup_values
from finpay_topup_pipeline.review import approve_refresh


def _dsn():
    return os.environ.get("FINPAY_TOPUP_TEST_DSN", "")


@unittest.skipIf(not _dsn(), "FINPAY_TOPUP_TEST_DSN not set")
class IntegrationDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg.connect(_dsn(), connect_timeout=10)
        assert_disposable_database(cls.conn)
        ensure_schema(cls.conn)
        cls._truncate()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    @classmethod
    def _truncate(cls):
        cls.conn.rollback()
        with cls.conn.cursor() as cur:
            cur.execute(
                f"TRUNCATE {TABLE_TXN}, {TABLE_TXN_STAGING}, {TABLE_CLASS}, {TABLE_BALANCE}, {TABLE_REFRESH} "
                f"RESTART IDENTITY CASCADE"
            )
        cls.conn.commit()

    def test_load_and_running_saldo(self):
        self._truncate()
        rows = [
            {"transaction_date": datetime(2026, 8, 1, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Kredit", "amount": 1000, "currency": "IDR", "remarks": "setor"},
            {"transaction_date": datetime(2026, 8, 1, 9, 0, 0), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 500, "currency": "IDR", "remarks": "topup"},
        ]
        res = load_rows(self.conn, rows, "411311", "unit.csv")
        self.assertEqual(res["inserted"], 2)
        self.assertEqual(current_saldo(self.conn, "411311"), 500)
        summ = summary_by_cluster(self.conn)
        self.assertEqual(summ[0]["cluster_id"], "411311")
        self.assertEqual(summ[0]["total_kredit"], 1000)
        self.assertEqual(summ[0]["total_debit"], 500)

    def test_idempotent_reload(self):
        self._truncate()
        rows = [{"transaction_date": datetime(2026, 8, 1, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Kredit", "amount": 100, "currency": "IDR", "remarks": "x"}]
        first = load_rows(self.conn, rows, "411311", "unit.csv")
        second = load_rows(self.conn, rows, "411311", "unit.csv")
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)

    def test_latest_checkpoint_starts_on_next_day(self):
        self._truncate()
        rows = [
            {"transaction_date": datetime(2026, 8, 1, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Kredit", "amount": 1000, "currency": "IDR", "remarks": "opening movement"},
            {"transaction_date": datetime(2026, 8, 1, 9, 0, 0), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 300, "currency": "IDR", "remarks": "topup"},
        ]
        load_rows(self.conn, rows, "411311", "unit.csv")
        update_opening_balance_from_latest(self.conn)
        with self.conn.cursor() as cur:
            checkpoint = cur.execute(
                f"SELECT as_of_date, opening_balance FROM {TABLE_BALANCE} WHERE cluster_id = %s",
                ("411311",),
            ).fetchone()
        self.assertEqual(str(checkpoint[0]), "2026-08-02")
        self.assertEqual(checkpoint[1], 700)

        load_rows(
            self.conn,
            [{"transaction_date": datetime(2026, 8, 2, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 100, "currency": "IDR", "remarks": "topup"}],
            "411311",
            "next-day.csv",
        )
        self.assertEqual(current_saldo(self.conn, "411311"), 600)

    def test_classification_write(self):
        self._truncate()
        rows = [{"transaction_date": datetime(2026, 8, 1, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 10, "currency": "IDR", "remarks": "x"}]
        load_rows(self.conn, rows, "411311", "unit.csv")
        with self.conn.cursor() as cur:
            tid = cur.execute(f"SELECT txn_id FROM {TABLE_TXN} LIMIT 1").fetchone()[0]
        upsert_classification(self.conn, tid, "BAHODOPI", note="agent", by="tester")
        with self.conn.cursor() as cur:
            cat = cur.execute(f"SELECT category FROM {TABLE_CLASS} WHERE txn_id = %s", (tid,)).fetchone()[0]
        self.assertEqual(cat, "BAHODOPI")

    def test_classification_on_transaction(self):
        self._truncate()
        rows = [{"transaction_date": datetime(2026, 8, 1, 8, 0, 0), "sender": "", "receiver": "", "transaction_type": "Debit", "amount": 10, "currency": "IDR", "remarks": "x"}]
        load_rows(self.conn, rows, "411311", "unit.csv")
        with self.conn.cursor() as cur:
            tid = cur.execute(f"SELECT txn_id FROM {TABLE_TXN} LIMIT 1").fetchone()[0]
        upsert_classification(self.conn, tid, "POSO", by="tester")
        with self.conn.cursor() as cur:
            row = cur.execute(
                f"SELECT transaction_type, amount, category FROM {TABLE_TXN} t JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id WHERE t.txn_id = %s", (tid,)
            ).fetchone()
        self.assertEqual(row[2], "POSO")
        self.assertEqual(row[0], "Debit")
        self.assertEqual(row[1], 10)

    def test_snapshot_retry_simulation_reconciles_inflight_topup(self):
        """Model the MOROWALI case where a Debit arrived after first extract."""
        self._truncate()
        cluster_id = "421318"
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                f"VALUES (%s, %s, %s, %s)",
                (cluster_id, "2026-08-11", 68837100, "simulation opening"),
            )
        self.conn.commit()
        refresh_id = start_refresh(
            self.conn,
            "2026-08-11",
            "2026-08-18",
            ["421318_A"],
            requested_by="simulation",
            trigger_source="test",
        )
        first_extract = load_rows(
            self.conn,
            [{
                "transaction_date": datetime(2026, 8, 18, 13, 1, 51),
                "sender": "",
                "receiver": "",
                "transaction_type": "Debit",
                "amount": 3932515,
                "currency": "IDR",
                "remarks": "first extract before late top up",
            }],
            cluster_id,
            "first.csv",
        )
        self.assertEqual(first_extract["inserted"], 1)
        bucket = BucketTopupSnapshot(
            "421318_A",
            cluster_id,
            64604585,
            "Bucket Top Up Rp 64,604,585",
            datetime(2026, 8, 18, 6, 6, 59, tzinfo=timezone.utc),
            "https://digipos-cms.finpay.id/home",
        )

        first = verify_bucket_topup_values(
            self.conn, refresh_id, [bucket], verification_attempt=1
        )
        self.assertEqual(first["status"], "MISMATCH")
        # The first CMS snapshot is before the downloaded transaction timestamp,
        # so timestamp-bounded verification reports the full missing movement.
        self.assertEqual(first["clusters"][cluster_id]["verification_diff"], 4232515.0)

        retry_extract = load_rows(
            self.conn,
            [{
                "transaction_date": datetime(2026, 8, 18, 13, 5, 54),
                "sender": "",
                "receiver": "",
                "transaction_type": "Debit",
                "amount": 300000,
                "currency": "IDR",
                "remarks": "Top Up balance via SF 300000",
            }],
            cluster_id,
            "retry.csv",
        )
        self.assertEqual(retry_extract["inserted"], 1)
        late_bucket = BucketTopupSnapshot(
            bucket.username,
            bucket.cluster_id,
            bucket.value,
            bucket.text,
            datetime(2026, 8, 18, 13, 6, 0, tzinfo=timezone.utc),
            bucket.url,
        )
        second = verify_bucket_topup_values(
            self.conn, refresh_id, [late_bucket], verification_attempt=2
        )
        self.assertEqual(second["status"], "VERIFIED")
        self.assertEqual(second["clusters"][cluster_id]["computed_saldo"], 64604585.0)

        with self.conn.cursor() as cur:
            status = cur.execute(
                f"SELECT r.status AS refresh_status, c.status AS cluster_status, c.verification_attempts, c.computed_saldo, "
                f"c.bucket_topup_value, c.verification_diff FROM {TABLE_REFRESH} r "
                f"JOIN {TABLE_REFRESH_CLUSTER} c ON c.refresh_id = r.refresh_id "
                f"WHERE r.refresh_id = %s",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(status[0], "PENDING_REVIEW")
        self.assertEqual(status[1], "VERIFIED")
        self.assertEqual(status[2], 2)
        self.assertEqual(status[3], 64604585)
        self.assertEqual(status[4], 64604585)
        self.assertEqual(status[5], 0)
        match_hash = second["clusters"][cluster_id]["matching_row_hash"]
        self.assertIsNotNone(match_hash)
        self.assertEqual(approve_refresh(self.conn, refresh_id, "reviewer")["status"], "COMMITTED")
        with self.conn.cursor() as cur:
            matched = cur.execute(
                f"SELECT matching_transaction_id, matching_row_hash "
                f"FROM {TABLE_REFRESH_CLUSTER} WHERE refresh_id = %s AND cluster_id = %s",
                (refresh_id, cluster_id),
            ).fetchone()
            ledger_match = cur.execute(
                f"SELECT txn_id FROM {TABLE_TXN} WHERE row_hash = %s",
                (match_hash,),
            ).fetchone()
        self.assertEqual(matched[1], match_hash)
        self.assertEqual(matched[0], ledger_match[0])

    def test_backfill_and_running_saldo_matches_legacy(self):
        self._truncate()
        path = "data/FINPAY MOROWALI.xlsx"
        if not os.path.exists(path):
            self.skipTest("legacy workbook missing")
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                f"VALUES (%s, %s, %s, %s)",
                ("421318", "2026-08-01", 67967075, "accepted July 31 finance checkpoint"),
            )
        self.conn.commit()
        res = backfill_xlsx(
            self.conn,
            path,
            default_cluster_id="421318",
            from_date="2026-08-01",
            before_date="2026-08-11",
            seed_openings=False,
        )
        self.assertGreater(res["txns"], 0)
        self.assertEqual(res["openings_seeded"], 0)

        parsed = parse_morowali_xlsx(path, "421318")
        legacy_rows = sorted(
            (
                t for t in parsed["txns"]
                if t["cluster_id"] == "421318"
                and t["saldo"] is not None
                and t["transaction_date"].date().isoformat() < "2026-08-11"
                and t["transaction_date"].date().isoformat() >= "2026-08-01"
            ),
            key=lambda t: t["transaction_date"],
        )
        legacy_final = legacy_rows[-1]["saldo"] if legacy_rows else None
        computed_final = current_saldo(self.conn, "421318")
        self.assertIsNotNone(computed_final)
        if legacy_final is not None:
            self.assertLess(abs(float(computed_final) - float(legacy_final)), 1.0,
                            msg=f"computed final saldo {computed_final} != legacy {legacy_final}")

        mismatches = reconcile_xlsx(self.conn, path)
        self.assertIsInstance(mismatches, list)
        print(f"[backfill] txns={res['txns']} openings={res['openings_seeded']} "
              f"computed_final_saldo={computed_final} legacy_final_saldo={legacy_final} "
              f"reconcile_mismatches={len(mismatches)}")

    def test_bucket_snapshot_persisted_for_dashboard(self):
        """The DigiPOS CMS BUCKET TOP UP comparison must be queryable per cluster/date."""
        self._truncate()
        cluster_id = "421318"
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                f"VALUES (%s, %s, %s, %s)",
                (cluster_id, "2026-08-11", 64604585, "simulation opening"),
            )
        self.conn.commit()
        refresh_id = start_refresh(
            self.conn,
            "2026-08-11",
            "2026-08-18",
            ["421318_A"],
            requested_by="simulation",
            trigger_source="test",
        )
        load_rows(
            self.conn,
            [{
                "transaction_date": datetime(2026, 8, 18, 13, 1, 51),
                "sender": "",
                "receiver": "",
                "transaction_type": "Debit",
                "amount": 0,
                "currency": "IDR",
                "remarks": "already balanced",
            }],
            cluster_id,
            "unit.csv",
        )
        bucket = BucketTopupSnapshot(
            "421318_A",
            cluster_id,
            64604585,
            "Bucket Top Up Rp 64,604,585",
            datetime(2026, 8, 18, 6, 6, 59, tzinfo=timezone.utc),
            "https://digipos-cms.finpay.id/home",
        )
        verify_bucket_topup_values(
            self.conn, refresh_id, [bucket], verification_attempt=1, report_date="2026-08-18"
        )
        with self.conn.cursor() as cur:
            row = cur.execute(
                f"SELECT cluster_id, report_date, bucket_topup_value, computed_saldo, "
                f"verification_diff, status FROM {TABLE_BUCKET_SNAPSHOT} "
                f"WHERE cluster_id = %s AND report_date = %s",
                (cluster_id, "2026-08-18"),
            ).fetchone()
        self.assertEqual(row[0], cluster_id)
        self.assertEqual(row[2], 64604585)
        self.assertEqual(row[5], "VERIFIED")

        with self.conn.cursor() as cur:
            # Query the bucket snapshot table directly instead of VIEW_BUCKET_COMPARE
            row = cur.execute(
                f"SELECT cluster_id, bucket_topup_value, status "
                f"FROM {TABLE_BUCKET_SNAPSHOT} WHERE cluster_id = %s",
                (cluster_id,),
            ).fetchone()
        self.assertEqual(row[1], 64604585)
        self.assertEqual(row[2], "VERIFIED")
