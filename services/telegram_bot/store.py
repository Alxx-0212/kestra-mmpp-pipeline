"""Synchronous PostgreSQL access for the classification bot.

Thin wrappers over finpay_topup_pipeline so the bot stays a consumer of the
FinPay domain contracts and never re-implements business SQL.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from finpay_topup_pipeline.classification import (
    active_outlets,
    classify_for_cluster as _classify_for_cluster,
    kredit_report_page,
    resolve_outlet,
    unclassified_kredit,
    unclassified_kredit_counts,
)
from finpay_topup_pipeline.config import (
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
    TABLE_TXN_STAGING,
)
from finpay_topup_pipeline.excel_export import build_finance_workbook
from finpay_topup_pipeline.refresh_service import CLUSTER_USERS
from finpay_topup_pipeline.review import approve_refresh as approve_pending_refresh
from finpay_topup_pipeline.review import reject_refresh as reject_pending_refresh
from finpay_topup_pipeline.saldo import latest_checkpoints

WALKTHROUGH_LIMIT = 50
LOCAL_TZ = ZoneInfo("Asia/Makassar")


class RefreshScopeError(ValueError):
    """Raised when an automatic refresh has no safe checkpoint scope."""

    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


def _scope_keys(values):
    if isinstance(values, str):
        values = [values]
    keys = set()
    for value in values or []:
        value = str(value).strip()
        if value:
            keys.add(value)
            if value.endswith("_A"):
                keys.add(value[:-2])
    return keys


def resolve_refresh_scope(conn, cluster_ids, today=None):
    """Resolve checkpoints and reject pending overlap for an automatic run."""
    if isinstance(cluster_ids, str):
        cluster_ids = [cluster_ids]
    cluster_ids = list(dict.fromkeys(str(cluster_id) for cluster_id in cluster_ids))
    if not cluster_ids or any(cluster_id not in CLUSTER_USERS for cluster_id in cluster_ids):
        raise RefreshScopeError("unknown refresh cluster")
    today = today or datetime.now(LOCAL_TZ).date()
    if isinstance(today, datetime):
        today = today.date()
    elif not isinstance(today, date):
        today = date.fromisoformat(str(today))
    checkpoints = latest_checkpoints(conn, cluster_ids)
    if len(checkpoints) != len(cluster_ids):
        raise RefreshScopeError("a refresh checkpoint is missing")
    for cluster_id in cluster_ids:
        as_of_date = checkpoints[cluster_id]["as_of_date"]
        if isinstance(as_of_date, datetime):
            as_of_date = as_of_date.date()
        elif not isinstance(as_of_date, date):
            as_of_date = date.fromisoformat(str(as_of_date))
        checkpoints[cluster_id]["as_of_date"] = as_of_date
        if as_of_date > today:
            raise RefreshScopeError("a refresh checkpoint is in the future")

    requested_users = [CLUSTER_USERS[cluster_id] for cluster_id in cluster_ids]
    requested_scope = _scope_keys([*cluster_ids, *requested_users])
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT refresh_id, requested_users FROM {TABLE_REFRESH} "
            "WHERE status = 'PENDING_REVIEW'"
        )
        for refresh_id, pending_users in cur.fetchall():
            if requested_scope & _scope_keys(pending_users):
                raise RefreshScopeError(
                    f"refresh {refresh_id} is awaiting review for overlapping scope",
                    code="pending",
                )

    checkpoint_dates = {
        cluster_id: checkpoints[cluster_id]["as_of_date"].isoformat()
        for cluster_id in cluster_ids
    }
    return {
        "cluster_ids": cluster_ids,
        "users": requested_users,
        "start": min(checkpoint_dates.values()),
        "end": today.isoformat(),
        "today": today.isoformat(),
        "checkpoint_dates": checkpoint_dates,
        "checkpoints": checkpoints,
    }


def list_active_outlets(conn, cluster_id=None):
    return active_outlets(conn, cluster_id=cluster_id)


def find_outlet(conn, code, cluster_id=None):
    return resolve_outlet(conn, code, cluster_id=cluster_id)


def list_unclassified(
    conn,
    start_date=None,
    end_date=None,
    limit=WALKTHROUGH_LIMIT,
    cluster_id=None,
):
    return unclassified_kredit(
        conn,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        cluster_id=cluster_id,
    )


def list_kredit_page(conn, start_date=None, end_date=None, cluster_id=None, page=0):
    return kredit_report_page(
        conn,
        start_date=start_date,
        end_date=end_date,
        cluster_id=cluster_id,
        offset=max(int(page), 0) * 20,
    )


def build_topup_excel(conn, start_date, end_date, cluster_ids):
    return build_finance_workbook(conn, start_date, end_date, cluster_ids)


def counts(conn, cluster_id=None):
    return unclassified_kredit_counts(conn, cluster_id=cluster_id)


def is_classified(conn, txn_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM finpay_topup_classification WHERE txn_id = %s",
            (txn_id,),
        )
        return cur.fetchone() is not None


def classify(conn, txn_id, cluster_id, outlet_code, classified_by):
    """Keep the legacy store entry point on the same guarded write path."""
    return _classify_for_cluster(conn, txn_id, cluster_id, outlet_code, classified_by)


def classify_for_cluster(conn, txn_id, cluster_id, outlet_code, classified_by):
    return _classify_for_cluster(conn, txn_id, cluster_id, outlet_code, classified_by)


classify_for_requested_cluster = classify_for_cluster


def approve_refresh(conn, refresh_id, reviewed_by):
    return approve_pending_refresh(conn, refresh_id, reviewed_by)


def reject_refresh(conn, refresh_id, reviewed_by):
    return reject_pending_refresh(conn, refresh_id, reviewed_by)


def latest_refresh_for_cluster(conn, cluster_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT r.refresh_id, r.status, r.requested_start, r.requested_end, "
            f"r.requested_users, r.requested_by, r.trigger_source, r.refresh_mode, "
            f"r.kestra_execution_id, c.checkpoint_as_of_date, c.status, c.computed_saldo, "
            f"c.bucket_topup_value, c.verification_diff, c.bucket_snapshot_at, "
            f"c.calculation_cutoff_at, c.calculated_at, "
            f"c.source_transaction_min_at, c.source_transaction_max_at, "
            f"CASE WHEN r.status = 'PENDING_REVIEW' THEN (SELECT COUNT(*) "
            f"FROM {TABLE_TXN_STAGING} s WHERE s.refresh_id = r.refresh_id "
            f"AND s.cluster_id = c.cluster_id) ELSE 0 END "
            f"FROM {TABLE_REFRESH} r "
            f"JOIN {TABLE_REFRESH_CLUSTER} c ON c.refresh_id = r.refresh_id "
            f"WHERE c.cluster_id = %s "
            f"ORDER BY r.refresh_id DESC LIMIT 1",
            (cluster_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "refresh_id": row[0],
        "status": row[1],
        "lifecycle_status": row[1],
        "requested_start": str(row[2]),
        "requested_end": str(row[3]),
        "requested_users": row[4],
        "requested_by": row[5],
        "trigger_source": row[6],
        "refresh_mode": row[7],
        "kestra_execution_id": row[8],
        "checkpoint_as_of_date": str(row[9]) if row[9] is not None else None,
        "verification_status": row[10],
        "cluster_status": row[10],
        "computed_saldo": row[11],
        "bucket_topup_value": row[12],
        "verification_diff": row[13],
        "bucket_snapshot_at": row[14],
        "calculation_cutoff_at": row[15],
        "calculated_at": row[16],
        "source_transaction_min_at": row[17],
        "source_transaction_max_at": row[18],
        "staged_count": int(row[19] or 0),
    }


def refresh_details(conn, refresh_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT r.refresh_id, r.status, r.requested_start, r.requested_end, "
            f"r.requested_users, r.refresh_mode, r.kestra_execution_id, "
            f"c.cluster_id, c.checkpoint_as_of_date, c.status, c.source_row_count, "
            f"c.loaded_seen, c.loaded_inserted, c.computed_saldo, c.bucket_topup_value, "
            f"c.verification_diff, c.bucket_snapshot_at, c.calculation_cutoff_at, "
            f"c.calculated_at, c.error_message, c.source_transaction_min_at, "
            f"c.source_transaction_max_at, c.calculation_row_count, c.matching_row_number, "
            f"c.matching_type, c.matching_transaction_id, c.matching_row_hash, "
            f"c.matching_transaction_at, "
            f"c.matching_running_saldo, r.requested_by, r.trigger_source, "
            f"r.reviewed_by, r.reviewed_at "
            f"FROM {TABLE_REFRESH} r "
            f"LEFT JOIN {TABLE_REFRESH_CLUSTER} c ON c.refresh_id = r.refresh_id "
            "WHERE r.refresh_id = %s ORDER BY c.cluster_id",
            (refresh_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    first = rows[0]
    clusters = []
    for row in rows:
        if row[7] is None:
            continue
        clusters.append({
            "cluster_id": row[7],
            "checkpoint_as_of_date": str(row[8]) if row[8] is not None else None,
            "status": row[9],
            "source_row_count": row[10],
            "loaded_seen": row[11],
            "loaded_inserted": row[12],
            "computed_saldo": row[13],
            "bucket_topup_value": row[14],
            "verification_diff": row[15],
            "bucket_snapshot_at": row[16],
            "calculation_cutoff_at": row[17],
            "calculated_at": row[18],
            "error_message": row[19],
            "source_transaction_min_at": row[20],
            "source_transaction_max_at": row[21],
            "calculation_row_count": row[22],
            "matching_row_number": row[23],
            "matching_type": row[24],
            "matching_transaction_id": row[25],
            "matching_row_hash": row[26],
            "matching_transaction_at": row[27],
            "matching_running_saldo": row[28],
        })
    return {
        "refresh_id": first[0],
        "status": first[1],
        "requested_start": str(first[2]),
        "requested_end": str(first[3]),
        "requested_users": first[4],
        "refresh_mode": first[5],
        "kestra_execution_id": first[6],
        "requested_by": first[29],
        "trigger_source": first[30],
        "reviewed_by": first[31],
        "reviewed_at": first[32],
        "clusters": clusters,
    }


def latest_refresh(conn, cluster_id=None):
    if cluster_id is not None:
        return latest_refresh_for_cluster(conn, cluster_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refresh_id, status, requested_start, requested_end "
            "FROM finpay_topup_refresh ORDER BY refresh_id DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "refresh_id": row[0],
        "status": row[1],
        "requested_start": str(row[2]),
        "requested_end": str(row[3]),
    }
