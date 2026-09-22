"""Operator-only orchestration for correcting a pending Top-Up refresh."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import TABLE_REFRESH, TABLE_REFRESH_CLUSTER
from .correction import (
    approve_manual_adjustment_for_refresh,
    propose_manual_adjustment_for_refresh,
    restage_refresh_cluster,
)
from .extract import run_extract
from .refresh_service import DEFAULT_USERS
from .verify import bucket_cutoff_for_refresh, verify_live_bucket_topup

LOCAL_TZ = ZoneInfo("Asia/Makassar")
OPERATIONS = {"RESTAGE", "PROPOSE_ADJUSTMENT", "APPROVE_ADJUSTMENT"}


def _cluster_user(cluster_id):
    prefix = f"{cluster_id}_"
    try:
        return next(user for user in DEFAULT_USERS if user.startswith(prefix))
    except StopIteration as exc:
        raise ValueError("cluster is not configured") from exc


def apply_operator_correction(
    conn,
    operation,
    refresh_id,
    cluster_id,
    actor,
    reason,
    source_reference,
    source_start=None,
    source_end=None,
    effective_at=None,
    transaction_type=None,
    amount=None,
    adjustment_id=None,
    inbox_dir=None,
    password=None,
    **kwargs,
):
    """Apply one audited correction operation without committing the ledger."""
    operation = str(operation or "").upper()
    if operation not in OPERATIONS:
        raise ValueError(f"unsupported correction operation: {operation}")
    if not actor or not reason or not source_reference:
        raise ValueError("actor, reason, and source reference are required")
    if operation == "RESTAGE":
        if not inbox_dir or not source_start or not source_end:
            raise ValueError("restage requires source dates and an inbox directory")
        extraction = run_extract(
            source_start,
            source_end,
            users=[_cluster_user(cluster_id)],
            output_dir=Path(inbox_dir),
            password=password,
        )
        if extraction.get("returncode") != 0:
            raise RuntimeError(extraction.get("stderr") or "source extraction failed")
        result = restage_refresh_cluster(
            conn,
            refresh_id,
            cluster_id,
            inbox_dir,
            source_start,
            source_end,
            reason,
            source_reference,
            actor,
        )
        return {**result, "ready_for_verification": True}
    if operation == "PROPOSE_ADJUSTMENT":
        if not effective_at or not transaction_type or amount is None:
            raise ValueError("adjustment proposal requires date, type, and amount")
        result = propose_manual_adjustment_for_refresh(
            conn,
            refresh_id,
            cluster_id,
            effective_at,
            transaction_type,
            amount,
            reason,
            source_reference,
            actor,
            **kwargs,
        )
        return {
            "refresh_id": refresh_id,
            "cluster_id": cluster_id,
            "adjustment_id": result.get("adjustment_id") if result else None,
            "status": "PROPOSED" if result else "DUPLICATE",
            "ready_for_verification": False,
        }
    if adjustment_id is None:
        raise ValueError("adjustment_id is required to approve an adjustment")
    result = approve_manual_adjustment_for_refresh(
        conn,
        refresh_id,
        cluster_id,
        adjustment_id,
        actor,
    )
    if result is None:
        raise ValueError("adjustment is missing, belongs to another cluster, or is not proposed")
    return {**result, "ready_for_verification": True}


def verify_operator_correction(conn, refresh_id, cluster_id, password, verification_attempt=3):
    """Re-run CMS verification for one corrected cluster only."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT r.requested_start, r.requested_end, c.checkpoint_as_of_date "
            f"FROM {TABLE_REFRESH} r JOIN {TABLE_REFRESH_CLUSTER} c USING (refresh_id) "
            "WHERE r.refresh_id=%s AND c.cluster_id=%s",
            (refresh_id, cluster_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("refresh or cluster was not found")
    requested_start, requested_end, checkpoint = row
    today = datetime.now(LOCAL_TZ).date()
    result = verify_live_bucket_topup(
        conn,
        refresh_id,
        [_cluster_user(cluster_id)],
        password,
        cutoff_date=bucket_cutoff_for_refresh(requested_end, today),
        verification_attempt=verification_attempt,
        report_date=requested_end,
        include_refresh_id=refresh_id,
    )
    result["requested_start"] = requested_start.isoformat()
    result["requested_end"] = requested_end.isoformat()
    result["checkpoint_as_of_date"] = checkpoint.isoformat() if checkpoint else None
    if cluster_id in result.get("clusters", {}):
        result["clusters"][cluster_id]["checkpoint_as_of_date"] = (
            checkpoint.isoformat() if checkpoint else None
        )
    return result
