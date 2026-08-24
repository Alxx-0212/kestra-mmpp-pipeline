from __future__ import annotations

from .audit import update_refresh_status, upsert_cluster_status, record_bucket_snapshot
from .bucket import fetch_bucket_topup
from .saldo import current_saldo_before_date, latest_saldo_snapshot


DEFAULT_TOLERANCE = 0.5


def bucket_cutoff_for_refresh(end_date, today):
    """Return the saldo cutoff date for BUCKET TOP UP verification.

    The DigiPOS home BUCKET TOP UP value is treated as a closed-day balance.
    When the refresh window ends today, compare it to the computed saldo before
    today's transactions. Historical closed windows can compare to the latest
    downloaded saldo for that window.
    """
    if end_date >= today:
        return today
    return None


def verify_bucket_topup_values(
    conn,
    refresh_id,
    snapshots,
    tolerance=DEFAULT_TOLERANCE,
    cutoff_date=None,
    verification_attempt=1,
    report_date=None,
):
    """Compare computed saldo with DigiPOS BUCKET TOP UP snapshots.

    Mismatches intentionally do not roll back loaded rows. They mark the
    refresh as VERIFY_FAILED so finance can review the loaded evidence in
    Superset while the trusted opening-balance checkpoint stays unchanged.

    ``cutoff_date`` is used when the CMS bucket represents a closed-day
    balance, including the daily refresh path where today's CMS home page still
    shows yesterday's closing BUCKET TOP UP.
    """
    failures = []
    results = {}
    for snapshot in snapshots:
        reference_date = cutoff_date
        if cutoff_date is not None:
            computed = current_saldo_before_date(conn, snapshot.cluster_id, cutoff_date)
        else:
            latest = latest_saldo_snapshot(conn, snapshot.cluster_id)
            computed = latest["saldo"] if latest else None
            reference_date = latest["transaction_date"].date() if latest else None
        if computed is None:
            diff = None
            status = "VERIFY_FAILED"
            error = "No computed saldo for cluster"
        else:
            diff = float(computed) - float(snapshot.value)
            status = "VERIFIED" if abs(diff) <= tolerance else "VERIFY_FAILED"
            error = None if status == "VERIFIED" else "Computed saldo differs from BUCKET TOP UP"
        upsert_cluster_status(
            conn,
            refresh_id,
            snapshot.cluster_id,
            status=status,
            computed_saldo=computed,
            bucket_topup_value=snapshot.value,
            bucket_topup_text=snapshot.text,
            bucket_reference_date=reference_date,
            bucket_snapshot_at=snapshot.snapshot_at,
            verification_diff=diff,
            verification_attempts=verification_attempt,
            error_message=error,
        )
        if report_date is not None:
            record_bucket_snapshot(
                conn,
                snapshot.cluster_id,
                report_date,
                value=float(snapshot.value),
                text=snapshot.text,
                username=snapshot.username,
                source_url=snapshot.url,
                computed_saldo=computed,
                verification_diff=diff,
                status=status,
            )
        results[snapshot.cluster_id] = {
            "status": status,
            "computed_saldo": None if computed is None else float(computed),
            "bucket_topup_value": float(snapshot.value),
            "verification_diff": diff,
            "bucket_topup_text": snapshot.text,
            "bucket_reference_date": str(reference_date) if reference_date is not None else None,
            "verification_attempts": verification_attempt,
        }
        if status != "VERIFIED":
            failures.append(snapshot.cluster_id)

    overall = "VERIFIED" if not failures else "VERIFY_FAILED"
    err = None if not failures else f"BUCKET TOP UP mismatch for clusters: {', '.join(failures)}"
    update_refresh_status(conn, refresh_id, overall, err)
    return {"status": overall, "clusters": results}


def verify_live_bucket_topup(
    conn,
    refresh_id,
    users,
    password,
    tolerance=DEFAULT_TOLERANCE,
    cutoff_date=None,
    verification_attempt=1,
    report_date=None,
):
    snapshots = []
    scrape_failures = []
    for username in users:
        cluster_id = username[:-2] if username.endswith("_A") else username
        try:
            snapshots.append(fetch_bucket_topup(username, password))
        except Exception as exc:  # noqa: BLE001 - preserve evidence and continue.
            scrape_failures.append(cluster_id)
            upsert_cluster_status(
                conn,
                refresh_id,
                cluster_id,
                status="VERIFY_FAILED",
                verification_attempts=verification_attempt,
                error_message=f"Could not read BUCKET TOP UP: {exc}",
            )
            if report_date is not None:
                record_bucket_snapshot(
                    conn,
                    cluster_id,
                    report_date,
                    username=username,
                    source_url="https://digipos-cms.finpay.id/home",
                    status="VERIFY_FAILED",
                )

    result = verify_bucket_topup_values(
        conn,
        refresh_id,
        snapshots,
        tolerance,
        cutoff_date,
        verification_attempt,
        report_date,
    ) if snapshots else {
        "status": "VERIFY_FAILED",
        "clusters": {},
    }
    if scrape_failures:
        result["status"] = "VERIFY_FAILED"
        result["scrape_failures"] = scrape_failures
        update_refresh_status(
            conn,
            refresh_id,
            "VERIFY_FAILED",
            f"Could not read BUCKET TOP UP for clusters: {', '.join(scrape_failures)}",
        )
    return result
