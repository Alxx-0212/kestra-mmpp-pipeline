from datetime import date, datetime

from .config import (
    TABLE_CORRECTION_AUDIT,
    TABLE_MANUAL_ADJUSTMENT,
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
)
from .loader import load_inbox_staged
from .manual_adjustment import approve_adjustment, propose_adjustment


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _lock_pending_cluster(conn, refresh_id, cluster_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT r.status, r.requested_end, c.status, c.checkpoint_as_of_date "
            f"FROM {TABLE_REFRESH} r "
            f"JOIN {TABLE_REFRESH_CLUSTER} c ON c.refresh_id = r.refresh_id "
            "WHERE r.refresh_id = %s AND c.cluster_id = %s FOR UPDATE",
            (refresh_id, cluster_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("refresh or cluster was not found")
    refresh_status, requested_end, cluster_status, checkpoint = row
    if refresh_status != "PENDING_REVIEW":
        raise ValueError("refresh must be pending review")
    if cluster_status == "COMMITTED":
        raise ValueError("committed cluster cannot be corrected")
    if checkpoint is None:
        raise ValueError("cluster checkpoint is missing")
    return _as_date(requested_end), _as_date(checkpoint), cluster_status


def _record_audit(
    conn,
    refresh_id,
    cluster_id,
    operation,
    actor,
    reason,
    source_reference=None,
    source_start=None,
    source_end=None,
    adjustment_id=None,
    details=None,
):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_CORRECTION_AUDIT} "
            "(refresh_id, cluster_id, operation, source_start, source_end, adjustment_id, "
            "reason, source_reference, actor, details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING correction_id",
            (
                refresh_id,
                cluster_id,
                operation,
                source_start,
                source_end,
                adjustment_id,
                reason,
                source_reference,
                actor,
                details,
            ),
        )
        return cur.fetchone()[0]


def restage_refresh_cluster(
    conn,
    refresh_id,
    cluster_id,
    inbox_dir,
    source_start,
    source_end,
    reason,
    source_reference,
    actor,
):
    """Replace one pending cluster/date range without touching other staging."""
    if not reason or not source_reference or not actor:
        raise ValueError("restage reason, source reference, and actor are required")
    _require_refresh_reference(refresh_id, source_reference)
    source_start = _as_date(source_start)
    source_end = _as_date(source_end)
    if source_start > source_end:
        raise ValueError("source start must be on or before source end")
    try:
        requested_end, checkpoint, _ = _lock_pending_cluster(conn, refresh_id, cluster_id)
        if source_start < checkpoint or source_end > requested_end:
            raise ValueError("restage range must stay inside the refresh window")
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH} SET status='STAGED', updated_at=now() "
                "WHERE refresh_id=%s",
                (refresh_id,),
            )
            cur.execute(
                f"UPDATE {TABLE_REFRESH_CLUSTER} SET status='STAGED', "
                "source_row_count=NULL, loaded_seen=NULL, loaded_inserted=0, "
                "computed_saldo=NULL, bucket_topup_value=NULL, bucket_topup_text=NULL, "
                "bucket_reference_date=NULL, bucket_snapshot_at=NULL, verification_diff=NULL, "
                "verification_attempts=0, source_transaction_min_at=NULL, "
                "source_transaction_max_at=NULL, calculation_cutoff_at=NULL, calculated_at=NULL, "
                "calculation_row_count=NULL, matching_row_number=NULL, matching_type=NULL, "
                "matching_transaction_id=NULL, matching_row_hash=NULL, "
                "matching_transaction_at=NULL, matching_running_saldo=NULL, error_message=NULL, "
                "updated_at=now() WHERE refresh_id=%s AND cluster_id=%s",
                (refresh_id, cluster_id),
            )
        counts = load_inbox_staged(
            conn,
            inbox_dir,
            refresh_id,
            cluster_ids=[cluster_id],
            checkpoint_dates={cluster_id: checkpoint},
            replace_scope=(source_start, source_end),
            commit=False,
        ).get(cluster_id, {"seen": 0, "staged": 0, "already_committed": 0})
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_REFRESH_CLUSTER} SET source_row_count=%s, loaded_seen=%s, "
                "loaded_inserted=0, status='STAGED', error_message=NULL, updated_at=now() "
                "WHERE refresh_id=%s AND cluster_id=%s",
                (counts["seen"], counts["seen"], refresh_id, cluster_id),
            )
        correction_id = _record_audit(
            conn,
            refresh_id,
            cluster_id,
            "RESTAGE",
            actor,
            reason,
            source_reference,
            source_start,
            source_end,
            details=f"seen={counts['seen']};staged={counts['staged']};already_committed={counts['already_committed']}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "refresh_id": refresh_id,
        "cluster_id": cluster_id,
        "status": "STAGED",
        "source_start": source_start.isoformat(),
        "source_end": source_end.isoformat(),
        "correction_id": correction_id,
        **counts,
    }


def _require_refresh_reference(refresh_id, source_reference):
    if not source_reference or f"refresh:{refresh_id}" not in source_reference.lower():
        raise ValueError("source reference must include refresh:<id>")


def propose_manual_adjustment_for_refresh(
    conn,
    refresh_id,
    cluster_id,
    effective_at,
    transaction_type,
    amount,
    reason,
    source_reference,
    created_by,
    **kwargs,
):
    """Create a PROPOSED adjustment tied to a pending refresh and audit it."""
    if not reason or not created_by:
        raise ValueError("adjustment reason and creator are required")
    _require_refresh_reference(refresh_id, source_reference)
    try:
        requested_end, checkpoint, _ = _lock_pending_cluster(conn, refresh_id, cluster_id)
        effective_date = _as_date(effective_at)
        if effective_date < checkpoint or effective_date > requested_end:
            raise ValueError("adjustment date must stay inside the refresh window")
        result = propose_adjustment(
            conn,
            cluster_id,
            effective_at,
            transaction_type,
            amount,
            reason,
            created_by,
            source_reference=source_reference,
            commit=False,
            **kwargs,
        )
        if result is None:
            conn.commit()
            return None
        correction_id = _record_audit(
            conn,
            refresh_id,
            cluster_id,
            "PROPOSE_ADJUSTMENT",
            created_by,
            reason,
            source_reference,
            _as_date(effective_at),
            _as_date(effective_at),
            result["adjustment_id"],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {**result, "refresh_id": refresh_id, "cluster_id": cluster_id, "correction_id": correction_id}


def approve_manual_adjustment_for_refresh(
    conn,
    refresh_id,
    cluster_id,
    adjustment_id,
    approved_by,
):
    """Approve one proposed adjustment only when it belongs to the refresh."""
    if not approved_by:
        raise ValueError("approver is required")
    try:
        _lock_pending_cluster(conn, refresh_id, cluster_id)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT cluster_id, effective_at, reason, source_reference, status "
                f"FROM {TABLE_MANUAL_ADJUSTMENT} WHERE adjustment_id=%s FOR UPDATE",
                (adjustment_id,),
            )
            row = cur.fetchone()
        if row is None or str(row[0]) != str(cluster_id) or row[4] != "PROPOSED":
            conn.commit()
            return None
        _require_refresh_reference(refresh_id, row[3])
        result = approve_adjustment(conn, adjustment_id, approved_by, commit=False)
        correction_id = _record_audit(
            conn,
            refresh_id,
            cluster_id,
            "APPROVE_ADJUSTMENT",
            approved_by,
            row[2],
            row[3],
            _as_date(row[1]),
            _as_date(row[1]),
            adjustment_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "refresh_id": refresh_id,
        "cluster_id": cluster_id,
        "adjustment_id": result[0],
        "status": result[1],
        "correction_id": correction_id,
    }
