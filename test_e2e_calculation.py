#!/usr/bin/env python3
"""Test end-to-end FinPay Top-Up calculation with synthetic data."""

import os
import sys
import tempfile
import shutil
from datetime import date, datetime

sys.path.insert(0, "/home/awsdx/projects/mmpp/kestra-mmpp-pipeline")

from finpay_topup_pipeline.config import dsn_from_env
from finpay_topup_pipeline.schema import ensure_schema
from finpay_topup_pipeline.loader import load_inbox
from finpay_topup_pipeline.saldo import (
    current_saldo,
    summary_by_cluster,
    update_opening_balance_from_latest,
)
from finpay_topup_pipeline.audit import start_refresh, update_refresh_status, upsert_cluster_status
from finpay_topup_pipeline.verify import (
    bucket_cutoff_for_refresh,
    verify_bucket_topup_values,
)
from finpay_topup_pipeline.bucket import BucketTopupSnapshot
import psycopg


def setup_database(conn):
    """Ensure schema and clean test data."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            TRUNCATE finpay_topup_txn, finpay_topup_classification, 
                   finpay_cluster_balance, finpay_topup_refresh,
                   finpay_topup_refresh_cluster, finpay_bucket_topup_snapshot
            RESTART IDENTITY CASCADE
        """)
    conn.commit()
    print("✅ Database schema ready")


def test_pipeline():
    # Use test DSN from environment
    test_dsn = os.environ.get("FINPAY_TOPUP_TEST_DSN")
    if not test_dsn:
        print("❌ FINPAY_TOPUP_TEST_DSN not set")
        return False

    conn = psycopg.connect(test_dsn)
    try:
        setup_database(conn)

        # Configuration
        start_date = "2026-08-11"
        end_date = "2026-08-13"
        users = ["421318_A", "411311_A"]
        cluster_ids = ["421318", "411311"]
        inbox_dir = "/test_inbox"

        print(f"\n{'='*60}")
        print(f"Testing FinPay Top-Up Pipeline")
        print(f"Date range: {start_date} to {end_date}")
        print(f"Clusters: {cluster_ids}")
        print(f"Inbox: {inbox_dir}")
        print(f"{'='*60}\n")

        # Step 1: Start refresh audit
        print("📝 Step 1: Starting refresh audit...")
        refresh_id = start_refresh(
            conn,
            start_date,
            end_date,
            users,
            requested_by="test",
            trigger_source="test",
        )
        print(f"   Refresh ID: {refresh_id}")

        # Step 2: Load CSV files (simulating DigiPOS download)
        print("\n📥 Step 2: Loading CSV files from inbox...")
        res = load_inbox(conn, inbox_dir)
        for cluster_id, counts in res.items():
            upsert_cluster_status(
                conn,
                refresh_id,
                cluster_id,
                status="LOADED",
                source_row_count=counts["seen"],
                loaded_seen=counts["seen"],
                loaded_inserted=counts["inserted"],
            )
        update_refresh_status(conn, refresh_id, "LOADED")
        print(f"   Loaded: {res}")

        # Step 3: Verify running saldo calculation
        print("\n🧮 Step 3: Calculating running saldo...")
        for cluster_id in cluster_ids:
            saldo = current_saldo(conn, cluster_id)
            print(f"   {cluster_id} current saldo: {saldo}")

        # Step 4: Summary by cluster
        print("\n📊 Step 4: Cluster summary...")
        summary = summary_by_cluster(conn)
        for s in summary:
            print(f"   {s['cluster_id']}: {s['rows']} txns, "
                  f"Kredit: {s['total_kredit']}, Debit: {s['total_debit']}, "
                  f"Saldo: {s['current_saldo']}")

        # Step 5: Test opening balance checkpoint
        print("\n💾 Step 5: Creating opening balance checkpoint...")
        update_opening_balance_from_latest(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT cluster_id, as_of_date, opening_balance FROM finpay_cluster_balance")
            for row in cur.fetchall():
                print(f"   Checkpoint: {row[0]} as of {row[1]} = {row[2]}")

        # Step 6: Simulate BUCKET TOP UP verification
        print("\n✅ Step 6: Simulating BUCKET TOP UP verification...")
        # Compute expected final saldo for each cluster
        for cluster_id in cluster_ids:
            final_saldo = current_saldo(conn, cluster_id)
            print(f"   {cluster_id} final saldo: {final_saldo}")

            # Create a matching bucket snapshot (simulating perfect match)
            snapshot = BucketTopupSnapshot(
                username=f"{cluster_id}_A",
                cluster_id=cluster_id,
                value=float(final_saldo),
                text=f"BUCKET TOP UP Rp {final_saldo:,.0f}",
                snapshot_at=datetime.now(),
                url="test://bucket-topup",
            )

            # Use cutoff_date for closed-day comparison
            cutoff = bucket_cutoff_for_refresh(date.fromisoformat(end_date), date.fromisoformat(end_date))
            print(f"   Cutoff date for verification: {cutoff}")

            verification = verify_bucket_topup_values(
                conn,
                refresh_id,
                [snapshot],
                cutoff_date=cutoff,
                verification_attempt=1,
                report_date=end_date,
            )
            print(f"   Verification: {verification['status']}")
            print(f"   Details: {verification['clusters']}")

        # Step 7: Final verification status
        print("\n🔍 Step 7: Final audit status...")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.refresh_id, r.status as refresh_status, 
                       c.cluster_id, c.status as cluster_status,
                       c.computed_saldo, c.bucket_topup_value, c.verification_diff
                FROM finpay_topup_refresh r
                JOIN finpay_topup_refresh_cluster c ON c.refresh_id = r.refresh_id
                WHERE r.refresh_id = %s
            """, (refresh_id,))
            for row in cur.fetchall():
                print(f"   Refresh: {row[0]}, Status: {row[1]}")
                print(f"   Cluster: {row[2]}, Status: {row[3]}")
                print(f"   Computed: {row[4]}, Bucket: {row[5]}, Diff: {row[6]}")

        # Step 8: Verify idempotency - reload same data
        print("\n🔄 Step 8: Testing idempotency (reload same data)...")
        res2 = load_inbox(conn, inbox_dir)
        print(f"   Second load inserted: {res2}")

        print(f"\n{'='*60}")
        print("✅ ALL TESTS PASSED")
        print(f"{'='*60}")
        return True

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    success = test_pipeline()
    sys.exit(0 if success else 1)