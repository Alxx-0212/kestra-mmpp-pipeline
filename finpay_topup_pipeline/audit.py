from __future__ import annotations

from .config import TABLE_REFRESH, TABLE_REFRESH_CLUSTER, TABLE_BUCKET_SNAPSHOT


TERMINAL_REFRESH_STATUSES = {"VERIFIED", "VERIFY_FAILED", "VERIFY_SKIPPED", "FAILED"}


def start_refresh(
    conn,
    start,
    end,
    users,
    requested_by=None,
    trigger_source="unknown",
    kestra_execution_id=None,
):
    with conn.cursor() as cur:
        row = cur.execute(
            f"INSERT INTO {TABLE_REFRESH} "
            f"(requested_start, requested_end, requested_users, requested_by, "
            f"trigger_source, kestra_execution_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s) "
            f"RETURNING refresh_id",
            (start, end, list(users or []), requested_by, trigger_source, kestra_execution_id),
        ).fetchone()
    conn.commit()
    return row[0]


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
    error_message=None,
):
    verification_attempts = 0 if verification_attempts is None else verification_attempts
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_REFRESH_CLUSTER} "
            f"(refresh_id, cluster_id, source_row_count, loaded_seen, loaded_inserted, "
            f"computed_saldo, bucket_topup_value, bucket_topup_text, bucket_reference_date, "
            f"bucket_snapshot_at, verification_diff, verification_attempts, status, error_message) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (refresh_id, cluster_id) DO UPDATE SET "
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
            f"status = EXCLUDED.status, "
            f"error_message = EXCLUDED.error_message, "
            f"updated_at = now()",
            (
                refresh_id,
                cluster_id,
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
            f"(cluster_id, report_date, username, bucket_topup_value, bucket_topup_text, "
            f"source_url, computed_saldo, verification_diff, status) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (cluster_id, report_date) DO UPDATE SET "
            f"captured_at = now(), "
            f"username = EXCLUDED.username, "
            f"bucket_topup_value = EXCLUDED.bucket_topup_value, "
            f"bucket_topup_text = EXCLUDED.bucket_topup_text, "
            f"source_url = EXCLUDED.source_url, "
            f"computed_saldo = EXCLUDED.computed_saldo, "
            f"verification_diff = EXCLUDED.verification_diff, "
            f"status = EXCLUDED.status",
            (cluster_id, report_date, username, value, text, source_url, computed_saldo, verification_diff, status),
        )
    conn.commit()
