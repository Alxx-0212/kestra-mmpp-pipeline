from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .audit import update_refresh_status, upsert_cluster_status, record_bucket_snapshot
from .bucket import fetch_bucket_topup
from .saldo import running_saldo_trace


DEFAULT_TOLERANCE = 0.5
LOCAL_TZ = ZoneInfo("Asia/Makassar")


def bucket_cutoff_for_refresh(end_date, today):
    """Return an exclusive date cutoff for a closed historical refresh.

    A current-day refresh returns ``None`` so each CMS snapshot timestamp is
    used as its inclusive calculation cutoff instead.
    """
    if not isinstance(end_date, date):
        end_date = date.fromisoformat(str(end_date))
    if not isinstance(today, date):
        today = date.fromisoformat(str(today))
    return None if end_date >= today else end_date + timedelta(days=1)


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _jsonable_row(row):
    if row is None:
        return None
    return {
        key: (
            value.isoformat()
            if isinstance(value, (date, datetime))
            else str(value)
            if isinstance(value, Decimal)
            else value
        )
        for key, value in row.items()
    }


def verify_bucket_topup_values(
    conn,
    refresh_id,
    snapshots,
    tolerance=DEFAULT_TOLERANCE,
    cutoff_date=None,
    verification_attempt=1,
    report_date=None,
    include_refresh_id=None,
    external_failures=None,
):
    """Compare computed saldo with DigiPOS BUCKET TOP UP snapshots.

    When ``include_refresh_id`` is set, rows staged for that refresh cycle are
    included in the computed saldo. Numeric results are recorded as ``VERIFIED``
    or ``MISMATCH`` and leave the refresh in ``PENDING_REVIEW``; the review
    action owns the stage-first commit/purge decision.

    ``cutoff_date`` is used when the CMS bucket represents a closed-day
    balance. Without it, each snapshot's capture timestamp is the inclusive
    saldo cutoff for current-day CMS values.
    """
    failures = []
    mismatches = []
    results = {}
    external_failures = list(external_failures or [])
    if not snapshots:
        update_refresh_status(
            conn,
            refresh_id,
            "VERIFY_FAILED",
            "No BUCKET TOP UP snapshots available",
        )
        return {
            "status": "VERIFY_FAILED",
            "refresh_status": "VERIFY_FAILED",
            "clusters": {},
        }
    for snapshot in snapshots:
        calculated_at = datetime.now(timezone.utc)
        cutoff_at = None
        if cutoff_date is not None:
            effective_cutoff_date = _as_date(cutoff_date)
            calculation_cutoff_at = datetime.combine(
                effective_cutoff_date, time.min, tzinfo=LOCAL_TZ
            ).astimezone(timezone.utc)
        else:
            effective_cutoff_date = None
            cutoff_at = _as_utc(snapshot.snapshot_at)
            calculation_cutoff_at = cutoff_at

        trace = running_saldo_trace(
            conn,
            snapshot.cluster_id,
            include_refresh_id=include_refresh_id,
            cutoff_date=effective_cutoff_date,
            cutoff_at=cutoff_at,
            target_value=snapshot.value,
            tolerance=tolerance,
        )
        computed = trace["computed_saldo"]
        source_min_at = trace["source_transaction_min_at"]
        source_max_at = trace["source_transaction_max_at"]
        reference_date = _as_date(report_date) or (
            cutoff_at.astimezone(ZoneInfo("Asia/Makassar")).date()
            if cutoff_at is not None
            else effective_cutoff_date
        )
        bucket_value = None if snapshot.value is None else Decimal(str(snapshot.value))
        if computed is None or snapshot.value is None:
            diff = None
            status = "VERIFY_FAILED"
            error = "No computed saldo for cluster"
            failures.append(snapshot.cluster_id)
        else:
            diff = Decimal(str(computed)) - bucket_value
            status = "VERIFIED" if abs(diff) <= Decimal(str(tolerance)) else "MISMATCH"
            error = None if status == "VERIFIED" else "Computed saldo differs from BUCKET TOP UP"
            if status == "MISMATCH":
                mismatches.append(snapshot.cluster_id)
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
            source_transaction_min_at=source_min_at,
            source_transaction_max_at=source_max_at,
            calculation_cutoff_at=calculation_cutoff_at,
            calculated_at=calculated_at,
            calculation_row_count=trace["calculation_row_count"],
            matching_row_number=trace["matching_row_number"],
            matching_type=trace["matching_type"],
            matching_transaction_id=trace["matching_transaction_id"],
            matching_row_hash=(trace["matching_row"] or {}).get("row_hash"),
            matching_transaction_at=trace["matching_transaction_at"],
            matching_running_saldo=trace["matching_running_saldo"],
            error_message=error,
        )
        if report_date is not None:
            record_bucket_snapshot(
                conn,
                snapshot.cluster_id,
                report_date,
                value=bucket_value,
                text=snapshot.text,
                username=snapshot.username,
                source_url=snapshot.url,
                captured_at=snapshot.snapshot_at,
                computed_saldo=computed,
                verification_diff=diff,
                status=status,
            )
        results[snapshot.cluster_id] = {
            "status": status,
            "computed_saldo": computed,
            "bucket_topup_value": bucket_value,
            "verification_diff": diff,
            "bucket_topup_text": snapshot.text,
            "bucket_snapshot_at": _iso(snapshot.snapshot_at),
            "bucket_reference_date": str(reference_date) if reference_date is not None else None,
            "verification_attempts": verification_attempt,
            "source_transaction_min_at": _iso(source_min_at),
            "source_transaction_max_at": _iso(source_max_at),
            "calculation_cutoff_at": _iso(calculation_cutoff_at),
            "calculated_at": _iso(calculated_at),
            "calculation_row_count": trace["calculation_row_count"],
            "matching_row_number": trace["matching_row_number"],
            "matching_type": trace["matching_type"],
            "matching_transaction_id": trace["matching_transaction_id"],
            "matching_row_hash": (trace["matching_row"] or {}).get("row_hash"),
            "matching_transaction_at": _iso(trace["matching_transaction_at"]),
            "matching_running_saldo": trace["matching_running_saldo"],
            "matching_row": _jsonable_row(trace["matching_row"]),
        }

    all_failures = sorted(set(failures).union(external_failures))
    if all_failures:
        overall = "VERIFY_FAILED"
        err = f"Could not calculate BUCKET TOP UP saldo for clusters: {', '.join(all_failures)}"
        update_refresh_status(conn, refresh_id, overall, err)
    elif mismatches:
        overall = "MISMATCH"
        err = f"BUCKET TOP UP mismatch for clusters: {', '.join(mismatches)}"
        update_refresh_status(conn, refresh_id, "PENDING_REVIEW", err)
    else:
        overall = "VERIFIED"
        update_refresh_status(conn, refresh_id, "PENDING_REVIEW", None)
    return {
        "status": overall,
        "refresh_status": "VERIFY_FAILED" if failures else "PENDING_REVIEW",
        "clusters": results,
    }


def verify_live_bucket_topup(
    conn,
    refresh_id,
    users,
    password,
    tolerance=DEFAULT_TOLERANCE,
    cutoff_date=None,
    verification_attempt=1,
    report_date=None,
    include_refresh_id=None,
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
                    captured_at=datetime.now(timezone.utc),
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
        include_refresh_id,
        external_failures=scrape_failures,
    )
    if scrape_failures:
        result["status"] = "VERIFY_FAILED"
        result["refresh_status"] = "VERIFY_FAILED"
        result["scrape_failures"] = scrape_failures
        update_refresh_status(
            conn,
            refresh_id,
            "VERIFY_FAILED",
            f"Could not read BUCKET TOP UP for clusters: {', '.join(scrape_failures)}",
        )
    return result
