import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import TABLE_TXN, TABLE_TXN_STAGING
from .io_csv import parse_topup_csv

LOCAL_TZ = ZoneInfo("Asia/Makassar")


def row_hash(cluster_id, row):
    transaction_date = row["transaction_date"]
    if transaction_date is not None and getattr(transaction_date, "tzinfo", None) is not None:
        transaction_date = transaction_date.astimezone(LOCAL_TZ).replace(tzinfo=None)
    key = "|".join([
        cluster_id,
        transaction_date.isoformat() if transaction_date else "",
        row["sender"] or "",
        row["receiver"] or "",
        row["transaction_type"],
        str(row["amount"]),
        row.get("currency") or "",
        row["remarks"] or "",
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


INSERT_TXN = None  # initialized in _ensure_ddl


def _txn_insert_sql():
    global INSERT_TXN
    if INSERT_TXN is None:
        INSERT_TXN = (
            f"INSERT INTO {TABLE_TXN} (cluster_id, transaction_date, sender, receiver, "
            f"transaction_type, amount, currency, remarks, source_file, row_hash) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"ON CONFLICT (row_hash) DO NOTHING"
        )
    return INSERT_TXN


def load_rows(conn, rows, cluster_id, source_file):
    sql = _txn_insert_sql()
    inserted = 0
    seen = 0
    with conn.cursor() as cur:
        for r in rows:
            if r.get("transaction_type") not in {"Kredit", "Debit"}:
                raise ValueError(f"Unsupported transaction type: {r.get('transaction_type')!r}")
            if r.get("transaction_date") is None or r.get("amount") is None:
                raise ValueError("Top Up rows require transaction_date and amount")
            seen += 1
            h = row_hash(cluster_id, r)
            cur.execute(sql, (
                cluster_id,
                r["transaction_date"],
                r["sender"],
                r["receiver"],
                r["transaction_type"],
                r["amount"],
                r["currency"],
                r["remarks"],
                source_file,
                h,
            ))
            if cur.rowcount:
                inserted += 1
    conn.commit()
    return {"inserted": inserted, "seen": seen}


def load_csv(conn, path, cluster_id, source_file=None):
    source_file = source_file or str(path)
    rows = parse_topup_csv(path)
    return load_rows(conn, rows, cluster_id, source_file)


def load_inbox(conn, inbox_dir):
    from pathlib import Path
    inbox = Path(inbox_dir)
    totals = {}
    for cluster_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        cluster_id = cluster_dir.name
        for csv_file in sorted(cluster_dir.glob("*.csv")):
            res = load_csv(conn, csv_file, cluster_id)
            prev = totals.get(cluster_id, {"inserted": 0, "seen": 0})
            totals[cluster_id] = {
                "inserted": prev["inserted"] + res["inserted"],
                "seen": prev["seen"] + res["seen"],
            }
    return totals


STAGE_INSERT_SQL = (
    f"INSERT INTO {TABLE_TXN_STAGING} "
    f"(refresh_id, cluster_id, transaction_date, sender, receiver, "
    f"transaction_type, amount, currency, remarks, source_file, row_hash) "
    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    f"ON CONFLICT (refresh_id, row_hash) DO NOTHING"
)


def _as_date(value):
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).date() if value.tzinfo is not None else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _validated_pending_rows(cluster_id, csv_files, checkpoint_as_of_date=None):
    """Parse CSVs into a hash-keyed batch, keeping the first row per hash."""
    pending = {}
    seen = 0
    for path in csv_files:
        for r in parse_topup_csv(path):
            if r.get("transaction_type") not in {"Kredit", "Debit"}:
                raise ValueError(f"Unsupported transaction type: {r.get('transaction_type')!r}")
            if r.get("transaction_date") is None or r.get("amount") is None:
                raise ValueError("Top Up rows require transaction_date and amount")
            seen += 1
            if (
                checkpoint_as_of_date is not None
                and _as_date(r["transaction_date"]) < checkpoint_as_of_date
            ):
                continue
            h = row_hash(cluster_id, r)
            pending.setdefault(h, (r, str(path)))
    return pending, seen


def load_inbox_staged(
    conn,
    inbox_dir,
    refresh_id,
    cluster_ids=None,
    checkpoint_dates=None,
    replace_scope=None,
    commit=True,
):
    """Stage inbox CSV rows for one refresh cycle without committing to the ledger.

    Any rows previously staged for this refresh_id are replaced so a retry
    re-extract restages cleanly. Hashes already present in the committed ledger
    are dropped, and duplicate hashes inside the batch keep only their first
    occurrence. Staging stays isolated per refresh_id, so concurrent cycles do
    not interfere.

    Returns per-cluster counts: ``{cluster_id: {"seen", "staged", "already_committed"}}``.
    """
    from pathlib import Path
    inbox = Path(inbox_dir)
    allowed_clusters = (
        None if cluster_ids is None else {str(cluster_id) for cluster_id in cluster_ids}
    )
    checkpoints = {
        str(cluster_id): _as_date(value.get("as_of_date") if isinstance(value, dict) else value)
        for cluster_id, value in (checkpoint_dates or {}).items()
    }
    if allowed_clusters is not None and checkpoint_dates is not None:
        missing = allowed_clusters - checkpoints.keys()
        if missing:
            raise ValueError(f"missing checkpoint dates for clusters: {sorted(missing)}")
    totals = {}
    with conn.cursor() as cur:
        if replace_scope is None:
            cur.execute(f"DELETE FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s", (refresh_id,))
        else:
            if not allowed_clusters:
                raise ValueError("replace_scope requires selected clusters")
            replace_start, replace_end = (_as_date(value) for value in replace_scope)
            if replace_start > replace_end:
                raise ValueError("replace scope start must be on or before end")
            cur.execute(
                f"DELETE FROM {TABLE_TXN_STAGING} "
                "WHERE refresh_id = %s AND cluster_id = ANY(%s) "
                "AND (transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %s "
                "AND (transaction_date AT TIME ZONE 'Asia/Makassar')::date <= %s",
                (refresh_id, sorted(allowed_clusters), replace_start, replace_end),
            )
    for cluster_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        cluster_id = cluster_dir.name
        if allowed_clusters is not None and cluster_id not in allowed_clusters:
            continue
        pending, seen = _validated_pending_rows(
            cluster_id,
            sorted(cluster_dir.glob("*.csv")),
            checkpoints.get(cluster_id),
        )
        already_committed = 0
        staged = 0
        if pending:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT row_hash FROM {TABLE_TXN} WHERE row_hash = ANY(%s)",
                    (list(pending.keys()),),
                )
                existing = {row[0] for row in cur.fetchall()}
            with conn.cursor() as cur:
                for h, (r, source_file) in pending.items():
                    if h in existing:
                        already_committed += 1
                        continue
                    cur.execute(STAGE_INSERT_SQL, (
                        refresh_id,
                        cluster_id,
                        r["transaction_date"],
                        r["sender"],
                        r["receiver"],
                        r["transaction_type"],
                        r["amount"],
                        r["currency"],
                        r["remarks"],
                        source_file,
                        h,
                    ))
                    if cur.rowcount:
                        staged += 1
        totals[cluster_id] = {
            "seen": seen,
            "staged": staged,
            "already_committed": already_committed,
        }
    if commit:
        conn.commit()
    return totals


def commit_staged_rows(conn, refresh_id):
    """Promote staged rows into the immutable ledger and clear the staging area.

    Committed-ledger conflicts (row_hash) are skipped silently so an overlapping
    historical window cannot double-write. Returns total inserted rows, the
    staged count observed before promotion, and per-cluster inserted counts.
    """
    per_cluster = {}
    staged_total = 0
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT cluster_id, COUNT(*) FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s GROUP BY cluster_id",
            (refresh_id,),
        )
        staged_by_cluster = dict(cur.fetchall())
        staged_total = sum(staged_by_cluster.values())
        for cluster_id in sorted(staged_by_cluster):
            cur.execute(
                f"INSERT INTO {TABLE_TXN} "
                f"(cluster_id, transaction_date, sender, receiver, transaction_type, "
                f"amount, currency, remarks, source_file, row_hash) "
                f"SELECT cluster_id, transaction_date, sender, receiver, transaction_type, "
                f"amount, currency, remarks, source_file, row_hash "
                f"FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s AND cluster_id = %s "
                f"ON CONFLICT (row_hash) DO NOTHING",
                (refresh_id, cluster_id),
            )
            per_cluster[cluster_id] = cur.rowcount
        cur.execute(f"DELETE FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s", (refresh_id,))
    conn.commit()
    return {"inserted": sum(per_cluster.values()), "staged": staged_total, "per_cluster": per_cluster}


def purge_staged_rows(conn, refresh_id):
    """Remove all staged rows for one refresh cycle (bad data is never accepted)."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s", (refresh_id,))
        purged = cur.rowcount
    conn.commit()
    return purged


def staged_row_count(conn, refresh_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {TABLE_TXN_STAGING} WHERE refresh_id = %s", (refresh_id,))
        return cur.fetchone()[0]
