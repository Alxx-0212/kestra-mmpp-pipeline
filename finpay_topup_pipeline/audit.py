from __future__ import annotations

from .config import TABLE_REFRESH, TABLE_REFRESH_CLUSTER, TABLE_BUCKET_SNAPSHOT
from .refresh_service import CLUSTER_USERS, DEFAULT_USERS


TERMINAL_REFRESH_STATUSES = {
    "COMMITTED",
    "REJECTED",
    "VERIFY_FAILED",
    "VERIFY_SKIPPED",
    "FAILED",
}


class PendingRefreshConflict(ValueError):
    """Raised when a new refresh overlaps a nonterminal cycle."""


def _scope_keys(users):
    if isinstance(users, str):
        users = [users]
    keys = set()
    for raw in users or []:
        value = str(raw).strip()
        if not value:
            continue
        keys.add(value)
        keys.add(value[:-2] if value.endswith("_A") else value)
    return keys


def start_refresh(
    conn,
    start,
    end,
    users,
    requested_by=None,
    trigger_source="unknown",
    kestra_execution_id=None,
    refresh_mode="FULL",
    checkpoint_dates=None,
):
    if isinstance(users, str):
        users = [users]
    refresh_mode = str(refresh_mode or "FULL").strip().upper()
    if refresh_mode not in {"FULL", "AUTO_CLUSTER", "AUTO_ALL"}:
        raise ValueError(f"unsupported refresh mode: {refresh_mode!r}")
    if refresh_mode == "AUTO_CLUSTER" and (
        len(users or []) != 1 or users[0] not in CLUSTER_USERS.values()
    ):
        raise ValueError("AUTO_CLUSTER requires one configured DigiPOS user")
    if refresh_mode == "AUTO_ALL" and (
        len(users or []) != len(DEFAULT_USERS) or set(users) != set(DEFAULT_USERS)
    ):
        raise ValueError("AUTO_ALL requires all configured DigiPOS users")
    requested_scope = _scope_keys(users)
    checkpoint_dates = dict(checkpoint_dates or {})
    requested_cluster_ids = {
        str(user)[:-2] if str(user).endswith("_A") else str(user)
        for user in users or []
    }
    if refresh_mode in {"AUTO_CLUSTER", "AUTO_ALL"} and set(checkpoint_dates) != requested_cluster_ids:
        raise ValueError("automatic refresh requires one checkpoint date per cluster")
    try:
        with conn.cursor() as cur:
            # Serialize the check so two simultaneous requests cannot both
            # pass when no active row existed at the start of either one.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("finpay_topup_active_refresh_guard",),
            )
            terminal_statuses = tuple(sorted(TERMINAL_REFRESH_STATUSES))
            placeholders = ", ".join("%s" for _ in terminal_statuses)
            cur.execute(
                f"SELECT refresh_id, requested_users, status FROM {TABLE_REFRESH} "
                f"WHERE status NOT IN ({placeholders}) FOR UPDATE",
                terminal_statuses,
            )
            for active_id, active_users, active_status in cur.fetchall():
                overlap = requested_scope & _scope_keys(active_users)
                if overlap:
                    raise PendingRefreshConflict(
                        f"refresh {active_id} is {active_status} for overlapping scope"
                    )
            row = cur.execute(
                f"INSERT INTO {TABLE_REFRESH} "
                f"(requested_start, requested_end, requested_users, requested_by, "
                f"trigger_source, refresh_mode, kestra_execution_id) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING refresh_id",
                (
                    start,
                    end,
                    list(users or []),
                    requested_by,
                    trigger_source,
                    refresh_mode,
                    kestra_execution_id,
                ),
            ).fetchone()
            for user in users or []:
                cluster_id = str(user).strip()
                if cluster_id.endswith("_A"):
                    cluster_id = cluster_id[:-2]
                checkpoint = checkpoint_dates.get(cluster_id)
                if isinstance(checkpoint, dict):
                    checkpoint = checkpoint.get("as_of_date")
                cur.execute(
                    f"INSERT INTO {TABLE_REFRESH_CLUSTER} "
                    "(refresh_id, cluster_id, checkpoint_as_of_date, status) "
                    "VALUES (%s, %s, %s, 'REQUESTED') "
                    "ON CONFLICT (refresh_id, cluster_id) DO UPDATE SET "
                    "checkpoint_as_of_date = EXCLUDED.checkpoint_as_of_date",
                    (row[0], cluster_id, checkpoint),
                )
        conn.commit()
        return row[0]
    except PendingRefreshConflict:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def update_refresh_status(conn, refresh_id, status, error_message=None):
    if not status:
        raise ValueError("status is required")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE_REFRESH} "
            f"SET status = %s, error_message = %s, updated_at = now() "
            f"WHERE refresh_id = %s",
            (status, error_message, refresh_id),
        )
    conn.commit()


def upsert_cluster_status(
    conn,
    refresh_id,
    cluster_id,
    *,
    status,
    source_row_count=None,
    loaded_seen=None,
    loaded_inserted=None,
    computed_saldo=None,
    bucket_topup_value=None,
    bucket_topup_text=None,
    bucket_reference_date=None,
    bucket_snapshot_at=None,
    verification_diff=None,
    verification_attempts=None,
    source_transaction_min_at=None,
    source_transaction_max_at=None,
    calculation_cutoff_at=None,
    calculated_at=None,
    calculation_row_count=None,
    matching_row_number=None,
    matching_type=None,
    matching_transaction_id=None,
    matching_row_hash=None,
    matching_transaction_at=None,
    matching_running_saldo=None,
    error_message=None,
    checkpoint_as_of_date=None,
):
    verification_attempts = 0 if verification_attempts is None else verification_attempts
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_REFRESH_CLUSTER} "
            f"(refresh_id, cluster_id, checkpoint_as_of_date, source_row_count, loaded_seen, loaded_inserted, "
            f"computed_saldo, bucket_topup_value, bucket_topup_text, bucket_reference_date, "
            f"bucket_snapshot_at, verification_diff, verification_attempts, "
            f"source_transaction_min_at, source_transaction_max_at, calculation_cutoff_at, "
            f"calculated_at, calculation_row_count, matching_row_number, matching_type, "
            f"matching_transaction_id, matching_row_hash, matching_transaction_at, matching_running_saldo, "
            f"status, error_message) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (refresh_id, cluster_id) DO UPDATE SET "
            f"checkpoint_as_of_date = COALESCE(EXCLUDED.checkpoint_as_of_date, {TABLE_REFRESH_CLUSTER}.checkpoint_as_of_date), "
            f"source_row_count = COALESCE(EXCLUDED.source_row_count, {TABLE_REFRESH_CLUSTER}.source_row_count), "
            f"loaded_seen = COALESCE(EXCLUDED.loaded_seen, {TABLE_REFRESH_CLUSTER}.loaded_seen), "
            f"loaded_inserted = COALESCE(EXCLUDED.loaded_inserted, {TABLE_REFRESH_CLUSTER}.loaded_inserted), "
            f"computed_saldo = COALESCE(EXCLUDED.computed_saldo, {TABLE_REFRESH_CLUSTER}.computed_saldo), "
            f"bucket_topup_value = COALESCE(EXCLUDED.bucket_topup_value, {TABLE_REFRESH_CLUSTER}.bucket_topup_value), "
            f"bucket_topup_text = COALESCE(EXCLUDED.bucket_topup_text, {TABLE_REFRESH_CLUSTER}.bucket_topup_text), "
            f"bucket_reference_date = COALESCE(EXCLUDED.bucket_reference_date, {TABLE_REFRESH_CLUSTER}.bucket_reference_date), "
            f"bucket_snapshot_at = COALESCE(EXCLUDED.bucket_snapshot_at, {TABLE_REFRESH_CLUSTER}.bucket_snapshot_at), "
            f"verification_diff = COALESCE(EXCLUDED.verification_diff, {TABLE_REFRESH_CLUSTER}.verification_diff), "
            f"verification_attempts = GREATEST({TABLE_REFRESH_CLUSTER}.verification_attempts, EXCLUDED.verification_attempts), "
            f"source_transaction_min_at = COALESCE(EXCLUDED.source_transaction_min_at, {TABLE_REFRESH_CLUSTER}.source_transaction_min_at), "
            f"source_transaction_max_at = COALESCE(EXCLUDED.source_transaction_max_at, {TABLE_REFRESH_CLUSTER}.source_transaction_max_at), "
            f"calculation_cutoff_at = COALESCE(EXCLUDED.calculation_cutoff_at, {TABLE_REFRESH_CLUSTER}.calculation_cutoff_at), "
            f"calculated_at = COALESCE(EXCLUDED.calculated_at, {TABLE_REFRESH_CLUSTER}.calculated_at), "
            f"calculation_row_count = COALESCE(EXCLUDED.calculation_row_count, {TABLE_REFRESH_CLUSTER}.calculation_row_count), "
            f"matching_row_number = EXCLUDED.matching_row_number, "
            f"matching_type = EXCLUDED.matching_type, "
            f"matching_transaction_id = EXCLUDED.matching_transaction_id, "
            f"matching_row_hash = EXCLUDED.matching_row_hash, "
            f"matching_transaction_at = EXCLUDED.matching_transaction_at, "
            f"matching_running_saldo = EXCLUDED.matching_running_saldo, "
            f"status = EXCLUDED.status, "
            f"error_message = EXCLUDED.error_message, "
            f"updated_at = now()",
            (
                refresh_id,
                cluster_id,
                checkpoint_as_of_date,
                source_row_count,
                loaded_seen,
                loaded_inserted,
                computed_saldo,
                bucket_topup_value,
                bucket_topup_text,
                bucket_reference_date,
                bucket_snapshot_at,
                verification_diff,
                verification_attempts,
                source_transaction_min_at,
                source_transaction_max_at,
                calculation_cutoff_at,
                calculated_at,
                calculation_row_count,
                matching_row_number,
                matching_type,
                matching_transaction_id,
                matching_row_hash,
                matching_transaction_at,
                matching_running_saldo,
                status,
                error_message,
            ),
        )
    conn.commit()


def record_bucket_snapshot(
    conn,
    cluster_id,
    report_date,
    *,
    value=None,
    text=None,
    username=None,
    source_url=None,
    captured_at=None,
    computed_saldo=None,
    verification_diff=None,
    status=None,
):
    """Persist one DigiPOS CMS BUCKET TOP UP comparison per cluster per report date.

    This is the dashboard-facing time series of the legacy SALDO vs DigiPOS CMS
    BUCKET TOP UP comparison that finance performs manually every day. A later
    refresh on the same report date overwrites the snapshot so the view shows the
    latest verified evidence for that day.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_BUCKET_SNAPSHOT} "
            f"(cluster_id, report_date, captured_at, username, bucket_topup_value, bucket_topup_text, "
            f"source_url, computed_saldo, verification_diff, status) "
            f"VALUES (%s, %s, COALESCE(%s, now()), %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (cluster_id, report_date) DO UPDATE SET "
            f"captured_at = EXCLUDED.captured_at, "
            f"username = EXCLUDED.username, "
            f"bucket_topup_value = EXCLUDED.bucket_topup_value, "
            f"bucket_topup_text = EXCLUDED.bucket_topup_text, "
            f"source_url = EXCLUDED.source_url, "
            f"computed_saldo = EXCLUDED.computed_saldo, "
            f"verification_diff = EXCLUDED.verification_diff, "
            f"status = EXCLUDED.status",
            (
                cluster_id,
                report_date,
                captured_at,
                username,
                value,
                text,
                source_url,
                computed_saldo,
                verification_diff,
                status,
            ),
        )
    conn.commit()
