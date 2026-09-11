"""Transactional finance decisions for staged FinPay top-up refreshes."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import TABLE_REFRESH, TABLE_REFRESH_CLUSTER, TABLE_TXN, TABLE_TXN_STAGING
from .saldo import update_opening_balance_before_date


TZ = ZoneInfo("Asia/Makassar")


def _as_date(value):
    return value.date() if isinstance(value, datetime) else (
        value if isinstance(value, date) else date.fromisoformat(str(value))
    )


def _existing_decision(refresh_id, status, requested_action):
    decision = {
        "COMMITTED": "approved",
        "REJECTED": "rejected",
    }.get(status)
    if decision:
        return {
            "refresh_id": refresh_id,
            "status": status,
            "decision": decision,
            "requested_action": requested_action,
            "idempotent": True,
        }
    return {
        "refresh_id": refresh_id,
        "status": status,
        "decision": "not_reviewable",
        "requested_action": requested_action,
        "idempotent": True,
    }


def _lock_refresh(conn, refresh_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status, requested_end, requested_at, requested_users FROM {TABLE_REFRESH} "
            "WHERE refresh_id = %s FOR UPDATE",
            (refresh_id,),
        )
        return cur.fetchone()


def _request_local_date(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(TZ).date()
    return _as_date(value)


def approve_refresh(conn, refresh_id, reviewed_by):
    """Approve verified clusters and retain incomplete clusters in staging."""
    try:
        row = _lock_refresh(conn, refresh_id)
        if row is None:
            conn.commit()
            return {"refresh_id": refresh_id, "status": "NOT_FOUND", "idempotent": True}
        status, requested_end, requested_at, requested_users = row
        if status != "PENDING_REVIEW":
            result = _existing_decision(refresh_id, status, "approve")
            conn.commit()
            return result

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cluster_id, status FROM {TABLE_REFRESH_CLUSTER} "
                "WHERE refresh_id = %s FOR UPDATE",
                (refresh_id,),
            )
            cluster_rows = cur.fetchall()
            requested_cluster_ids = {
                str(user)[:-2] if str(user).endswith("_A") else str(user)
                for user in (requested_users or [])
            }
            cluster_statuses = {str(cluster_id): cluster_status for cluster_id, cluster_status in cluster_rows}
            verified_cluster_ids = sorted(
                cluster_id
                for cluster_id, cluster_status in cluster_statuses.items()
                if cluster_id in requested_cluster_ids and cluster_status == "VERIFIED"
            )
            held_cluster_ids = sorted(
                cluster_id
                for cluster_id in requested_cluster_ids
                if cluster_statuses.get(cluster_id) not in {"VERIFIED", "COMMITTED"}
            )
            if not cluster_rows:
                held_cluster_ids = sorted(requested_cluster_ids)
            if not verified_cluster_ids:
                conn.commit()
                return {
                    "refresh_id": refresh_id,
                    "status": "PENDING_REVIEW",
                    "decision": "held_for_review",
                    "requested_action": "approve",
                    "idempotent": True,
                    "partial": False,
                    "committed_clusters": [],
                    "held_clusters": held_cluster_ids,
                    "staged": 0,
                    "inserted": 0,
                    "per_cluster": {},
                }
            cur.execute(
                f"SELECT COUNT(*) FROM {TABLE_TXN_STAGING} "
                "WHERE refresh_id = %s AND cluster_id = ANY(%s)",
                (refresh_id, verified_cluster_ids),
            )
            staged_count = cur.fetchone()[0]
            cur.execute(
                f"SELECT cluster_id, matching_row_hash FROM {TABLE_REFRESH_CLUSTER} "
                "WHERE refresh_id = %s AND cluster_id = ANY(%s) "
                "AND matching_row_hash IS NOT NULL",
                (refresh_id, verified_cluster_ids),
            )
            matching_hashes = dict(cur.fetchall())
            cur.execute(
                f"INSERT INTO {TABLE_TXN} "
                "(cluster_id, transaction_date, sender, receiver, transaction_type, "
                "amount, currency, remarks, source_file, row_hash) "
                f"SELECT cluster_id, transaction_date, sender, receiver, transaction_type, "
                f"amount, currency, remarks, source_file, row_hash "
                f"FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s "
                "AND cluster_id = ANY(%s) "
                "ON CONFLICT (row_hash) DO NOTHING RETURNING cluster_id, row_hash, txn_id",
                (refresh_id, verified_cluster_ids),
            )
            inserted_rows = cur.fetchall()
            inserted_by_cluster = Counter(row[0] for row in inserted_rows)
            inserted = sum(inserted_by_cluster.values())
            for cluster_id in verified_cluster_ids:
                cur.execute(
                    f"UPDATE {TABLE_REFRESH_CLUSTER} "
                    "SET status = 'COMMITTED', loaded_inserted = %s, updated_at = now() "
                    "WHERE refresh_id = %s AND cluster_id = %s",
                    (inserted_by_cluster.get(cluster_id, 0), refresh_id, cluster_id),
                )
            for cluster_id, row_hash in matching_hashes.items():
                cur.execute(
                    f"SELECT txn_id FROM {TABLE_TXN} WHERE row_hash = %s",
                    (row_hash,),
                )
                matched = cur.fetchone()
                cur.execute(
                    f"UPDATE {TABLE_REFRESH_CLUSTER} SET matching_transaction_id = %s, "
                    "updated_at = now() WHERE refresh_id = %s AND cluster_id = %s",
                    (matched[0] if matched else None, refresh_id, cluster_id),
                )
            requested_end = _as_date(requested_end)
            checkpoint_date = requested_end
            if requested_end < _request_local_date(requested_at):
                checkpoint_date += timedelta(days=1)
            update_opening_balance_before_date(
                conn,
                checkpoint_date,
                cluster_ids=verified_cluster_ids,
                commit=False,
            )
            partial = bool(held_cluster_ids)
            next_status = "PENDING_REVIEW" if partial else "COMMITTED"
            cur.execute(
                f"UPDATE {TABLE_REFRESH} SET status = %s, "
                "reviewed_by = %s, reviewed_at = now(), "
                "error_message = CASE WHEN %s = 'COMMITTED' THEN NULL ELSE error_message END, "
                "updated_at = now() "
                "WHERE refresh_id = %s RETURNING reviewed_at",
                (next_status, reviewed_by, next_status, refresh_id),
            )
            reviewed_at = cur.fetchone()[0]
            cur.execute(
                f"DELETE FROM {TABLE_TXN_STAGING} "
                "WHERE refresh_id = %s AND cluster_id = ANY(%s)",
                (refresh_id, verified_cluster_ids),
            )
        conn.commit()
        return {
            "refresh_id": refresh_id,
            "status": next_status,
            "decision": "partially_approved" if partial else "approved",
            "requested_action": "approve",
            "idempotent": False,
            "partial": partial,
            "staged": staged_count,
            "inserted": inserted,
            "per_cluster": dict(inserted_by_cluster),
            "committed_clusters": verified_cluster_ids,
            "held_clusters": held_cluster_ids,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        }
    except Exception:
        conn.rollback()
        raise


def reject_refresh(conn, refresh_id, reviewed_by):
    """Reject one pending refresh and purge only its staging atomically."""
    try:
        row = _lock_refresh(conn, refresh_id)
        if row is None:
            conn.commit()
            return {"refresh_id": refresh_id, "status": "NOT_FOUND", "idempotent": True}
        status = row[0]
        if status != "PENDING_REVIEW":
            result = _existing_decision(refresh_id, status, "reject")
            conn.commit()
            return result

        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s",
                (refresh_id,),
            )
            purged = cur.rowcount
            cur.execute(
                f"UPDATE {TABLE_REFRESH} SET status = 'REJECTED', "
                "reviewed_by = %s, reviewed_at = now(), updated_at = now() "
                "WHERE refresh_id = %s RETURNING reviewed_at",
                (reviewed_by, refresh_id),
            )
            reviewed_at = cur.fetchone()[0]
        conn.commit()
        return {
            "refresh_id": refresh_id,
            "status": "REJECTED",
            "decision": "rejected",
            "requested_action": "reject",
            "idempotent": False,
            "purged": purged,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        }
    except Exception:
        conn.rollback()
        raise


# Explicit names make the bot/store contract readable without duplicating logic.
approve_pending_refresh = approve_refresh
reject_pending_refresh = reject_refresh
