from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .audit import start_refresh, update_refresh_status, upsert_cluster_status
from .backfill import backfill_xlsx
from .bucket import BucketTopupSnapshot
from .config import TABLE_BALANCE, TABLE_CLASS, TABLE_REFRESH, TABLE_TXN
from .loader import load_inbox
from .schema import ensure_schema
from .saldo import summary_by_cluster
from .verify import verify_bucket_topup_values


@dataclass(frozen=True)
class RestoreResult:
    legacy: dict
    loaded: dict[str, dict[str, int]]
    refresh_id: int | None
    summary: list[dict]
    status: str


def reset_topup_tables(conn):
    """Clear only the FinPay Top Up prototype tables.

    This intentionally does not touch the older FinPay transaction tables that
    share the same Compose database.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"TRUNCATE {TABLE_TXN}, {TABLE_CLASS}, {TABLE_BALANCE}, {TABLE_REFRESH} "
            f"RESTART IDENTITY CASCADE"
        )
    conn.commit()


def restore_dashboard_data(
    conn,
    *,
    legacy_xlsx,
    legacy_cluster_id="421318",
    legacy_from_date=None,
    legacy_before_date=None,
    opening_as_of_date=None,
    opening_balance=None,
    inbox_dirs=None,
    reset=False,
    refresh_start=None,
    refresh_end=None,
    requested_users=None,
    requested_by="codex",
    trigger_source="local_restore",
    verify_status="VERIFY_SKIPPED",
    bucket_topup_value=None,
    bucket_topup_text=None,
):
    """Restore the finance-facing dashboard dataset from trusted local inputs.

    The common MOROWALI repair path is:
    - seed the legacy workbook as cluster ``421318``;
    - keep only legacy transactions before the live extract start date;
    - load DigiPOS Top Up CSV inboxes from that live start date onward.
    """
    ensure_schema(conn)
    if reset:
        reset_topup_tables(conn)
        ensure_schema(conn)

    if opening_as_of_date is not None or opening_balance is not None:
        if opening_as_of_date is None or opening_balance is None:
            raise ValueError("opening_as_of_date and opening_balance must be provided together")
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
                f"VALUES (%s, %s, %s, %s) "
                f"ON CONFLICT (cluster_id, as_of_date) DO UPDATE "
                f"SET opening_balance = EXCLUDED.opening_balance, note = EXCLUDED.note",
                (
                    legacy_cluster_id,
                    opening_as_of_date,
                    opening_balance,
                    "explicit finance checkpoint for dashboard restore",
                ),
            )
        conn.commit()

    legacy = backfill_xlsx(
        conn,
        legacy_xlsx,
        default_cluster_id=legacy_cluster_id,
        source_file=str(legacy_xlsx),
        from_date=legacy_from_date,
        before_date=legacy_before_date,
        seed_openings=opening_balance is None,
    )

    inbox_dirs = [Path(p) for p in (inbox_dirs or [])]
    loaded: dict[str, dict[str, int]] = {}
    refresh_id = None
    if inbox_dirs:
        requested_users = list(requested_users or [f"{legacy_cluster_id}_A"])
        refresh_id = start_refresh(
            conn,
            refresh_start or _min_refresh_date(inbox_dirs) or date.today().isoformat(),
            refresh_end or _max_refresh_date(inbox_dirs) or date.today().isoformat(),
            requested_users,
            requested_by=requested_by,
            trigger_source=trigger_source,
        )
        update_refresh_status(conn, refresh_id, "DOWNLOADED")
        for inbox_dir in inbox_dirs:
            res = load_inbox(conn, inbox_dir)
            for cluster_id, counts in res.items():
                previous = loaded.get(cluster_id, {"seen": 0, "inserted": 0})
                loaded[cluster_id] = {
                    "seen": previous["seen"] + counts["seen"],
                    "inserted": previous["inserted"] + counts["inserted"],
                }
        for cluster_id, counts in loaded.items():
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

        if bucket_topup_value is not None:
            snapshot = BucketTopupSnapshot(
                username=requested_users[0],
                cluster_id=legacy_cluster_id,
                value=float(bucket_topup_value),
                text=bucket_topup_text or f"BUCKET TOP UP Rp {bucket_topup_value}",
                snapshot_at=datetime.now(timezone.utc),
                url="local://provided-bucket-topup",
            )
            verification = verify_bucket_topup_values(conn, refresh_id, [snapshot])
            verify_status = verification["status"]
        else:
            update_refresh_status(conn, refresh_id, verify_status)
            for cluster_id in loaded:
                upsert_cluster_status(
                    conn,
                    refresh_id,
                    cluster_id,
                    status=verify_status,
                    verification_attempts=0,
                    error_message="BUCKET TOP UP verification not run for local restore",
                )

    summary = summary_by_cluster(conn)
    return RestoreResult(
        legacy=legacy,
        loaded=loaded,
        refresh_id=refresh_id,
        summary=summary,
        status=verify_status if refresh_id is not None else "LEGACY_RESTORED",
    )


def _min_refresh_date(paths):
    dates = [d for path in paths for d in _dates_from_path(path)]
    return min(dates).isoformat() if dates else None


def _max_refresh_date(paths):
    dates = [d for path in paths for d in _dates_from_path(path)]
    return max(dates).isoformat() if dates else None


def _dates_from_path(path):
    import re

    found = []
    for candidate in [Path(path), *Path(path).glob("**/*.csv")]:
        match = re.search(r"\((\d{2}-\d{2}-\d{4})to(\d{2}-\d{2}-\d{4})\)", str(candidate))
        if not match:
            continue
        for value in match.groups():
            found.append(datetime.strptime(value, "%d-%m-%Y").date())
    return found
