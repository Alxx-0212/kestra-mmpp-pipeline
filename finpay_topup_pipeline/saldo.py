from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import (
    TABLE_BALANCE,
    TABLE_CHECKPOINT_OVERRIDE,
    TABLE_CLASS,
    TABLE_MANUAL_ADJUSTMENT,
    TABLE_TXN,
    TABLE_TXN_STAGING,
)


LOCAL_TZ = ZoneInfo("Asia/Makassar")
TXN_TRACE_COLUMNS = (
    "txn_id",
    "cluster_id",
    "transaction_date",
    "sender",
    "receiver",
    "transaction_type",
    "amount",
    "currency",
    "remarks",
    "source_file",
    "row_hash",
)


def _txn_source_sql(include_refresh_id=None):
    """Project identical transaction fields from committed and optional staging."""
    columns = ", ".join(TXN_TRACE_COLUMNS)
    committed = (
        f"SELECT {columns} "
        f"FROM {TABLE_TXN} WHERE cluster_id = %(cluster_id)s"
    )
    manual = (
        f"SELECT (-adjustment_id)::bigint AS txn_id, cluster_id, effective_at AS transaction_date, "
        f"sender, receiver, transaction_type, amount, currency, remarks, "
        f"'manual_adjustment' AS source_file, adjustment_hash AS row_hash "
        f"FROM {TABLE_MANUAL_ADJUSTMENT} "
        "WHERE cluster_id = %(cluster_id)s AND status = 'APPROVED'"
    )
    if include_refresh_id is None:
        return f"{committed} UNION ALL {manual}"
    staged = (
        f"SELECT {columns} "
        f"FROM {TABLE_TXN_STAGING} "
        f"WHERE cluster_id = %(cluster_id)s AND refresh_id = %(refresh_id)s"
    )
    return f"{committed} UNION ALL {staged} UNION ALL {manual}"


def _checkpoint_source_sql():
    return (
        f"SELECT cluster_id, as_of_date, opening_balance, 0 AS priority, 0::bigint AS override_id "
        f"FROM {TABLE_BALANCE} "
        f"UNION ALL "
        f"SELECT cluster_id, as_of_date, opening_balance, 1 AS priority, override_id "
        f"FROM {TABLE_CHECKPOINT_OVERRIDE} WHERE status = 'APPROVED'"
    )


def running_saldo_trace(
    conn,
    cluster_id,
    cutoff_date=None,
    include_refresh_id=None,
    cutoff_at=None,
    target_value=None,
    tolerance=0.5,
):
    """Return final saldo and the last row matching a target BUCKET value.

    The transaction order is intentionally only ``transaction_date ASC``. The
    selected row is therefore the last matching row encountered in that order.
    """
    if cutoff_date is not None and cutoff_at is not None:
        raise ValueError("cutoff_date and cutoff_at are mutually exclusive")
    opening_cutoff = cutoff_date
    if cutoff_at is not None:
        opening_cutoff = (
            cutoff_at.astimezone(LOCAL_TZ).date()
            if cutoff_at.tzinfo is not None
            else cutoff_at.date()
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT opening_balance, as_of_date FROM ({_checkpoint_source_sql()}) checkpoints "
            "WHERE cluster_id = %s AND as_of_date <= COALESCE(%s::date, 'infinity'::date) "
            "ORDER BY as_of_date DESC, priority DESC, override_id DESC LIMIT 1",
            (cluster_id, opening_cutoff),
        )
        row = cur.fetchone()
        opening = row[0] if row else None
        opening_as_of = row[1] if row else None

        params = {
            "cluster_id": cluster_id,
            "refresh_id": include_refresh_id,
        }

        filters = []
        # If we have an opening balance checkpoint, only include transactions
        # on or after the checkpoint's as_of_date (the checkpoint represents
        # the balance BEFORE transactions on that date)
        if opening_as_of is not None:
            filters.append("(transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %(opening_as_of)s")
            params["opening_as_of"] = opening_as_of
        if cutoff_date:
            filters.append("(transaction_date AT TIME ZONE 'Asia/Makassar')::date < %(cutoff_date)s")
            params["cutoff_date"] = cutoff_date
        if cutoff_at is not None:
            filters.append("transaction_date <= %(cutoff_at)s")
            params["cutoff_at"] = cutoff_at
        filter_sql = (" AND " + " AND ".join(filters)) if filters else ""

        query = (
            f"SELECT {', '.join(TXN_TRACE_COLUMNS)} "
            f"FROM ({_txn_source_sql(include_refresh_id)}) u "
            f"WHERE 1=1{filter_sql} "
            f"ORDER BY transaction_date ASC"
        )

        cur.execute(query, params)
        rows = cur.fetchall()

    result = {
        "computed_saldo": None,
        "calculation_row_count": len(rows),
        "source_transaction_min_at": rows[0][2] if rows else None,
        "source_transaction_max_at": rows[-1][2] if rows else None,
        "matching_row_number": None,
        "matching_type": None,
        "matching_transaction_id": None,
        "matching_transaction_at": None,
        "matching_running_saldo": None,
        "matching_row": None,
    }
    if opening is None and not rows:
        return result

    saldo = opening if opening is not None else 0
    target_decimal = None if target_value is None else Decimal(str(target_value))
    tolerance_decimal = Decimal(str(tolerance))

    def target_matches(value):
        return (
            target_decimal is not None
            and abs(Decimal(str(value)) - target_decimal) <= tolerance_decimal
        )
    if opening is not None and target_matches(saldo):
        result.update({
            "matching_row_number": 0,
            "matching_type": "CHECKPOINT",
            "matching_running_saldo": saldo,
        })
    for row_number, row in enumerate(rows, 1):
        if row[5] == "Kredit":
            saldo += row[6]
        elif row[5] == "Debit":
            saldo -= row[6]
        if target_matches(saldo):
            result.update({
                "matching_row_number": row_number,
                "matching_type": "TRANSACTION",
                "matching_transaction_id": row[0],
                "matching_transaction_at": row[2],
                "matching_running_saldo": saldo,
                "matching_row": dict(zip(TXN_TRACE_COLUMNS, row)),
            })

    result["computed_saldo"] = saldo
    if not target_matches(saldo):
        for key in (
            "matching_row_number",
            "matching_type",
            "matching_transaction_id",
            "matching_transaction_at",
            "matching_running_saldo",
            "matching_row",
        ):
            result[key] = None
    elif result["matching_type"] is None:
        result.update({
            "matching_type": "CUTOFF",
            "matching_running_saldo": saldo,
        })
    return result


def _compute_running_saldo(
    conn,
    cluster_id,
    cutoff_date=None,
    include_refresh_id=None,
    cutoff_at=None,
):
    """Compute saldo through the same ordered trace used by verification."""
    return running_saldo_trace(
        conn,
        cluster_id,
        cutoff_date=cutoff_date,
        include_refresh_id=include_refresh_id,
        cutoff_at=cutoff_at,
    )["computed_saldo"]


def current_saldo(conn, cluster_id, include_refresh_id=None):
    return _compute_running_saldo(conn, cluster_id, include_refresh_id=include_refresh_id)


def latest_checkpoints(conn, cluster_ids):
    """Return the newest persisted opening checkpoint for each cluster."""
    if isinstance(cluster_ids, str):
        cluster_ids = [cluster_ids]
    cluster_ids = list(dict.fromkeys(str(cluster_id) for cluster_id in cluster_ids))
    if not cluster_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(cluster_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT ON (cluster_id) cluster_id, as_of_date, opening_balance "
            f"FROM ({_checkpoint_source_sql()}) checkpoints WHERE cluster_id IN ({placeholders}) "
            "ORDER BY cluster_id, as_of_date DESC, priority DESC, override_id DESC",
            cluster_ids,
        )
        return {
            row[0]: {"as_of_date": row[1], "opening_balance": row[2]}
            for row in cur.fetchall()
        }


def current_saldo_at(conn, cluster_id, cutoff_at, include_refresh_id=None):
    """Compute saldo through an inclusive source timestamp."""
    if cutoff_at is None:
        raise ValueError("cutoff_at is required")
    if cutoff_at.tzinfo is None:
        raise ValueError("cutoff_at must be timezone-aware")
    return _compute_running_saldo(
        conn,
        cluster_id,
        include_refresh_id=include_refresh_id,
        cutoff_at=cutoff_at,
    )


def latest_saldo_snapshot(conn, cluster_id, include_refresh_id=None):
    """Return the balance and source timestamp used for a live CMS comparison."""
    with conn.cursor() as cur:
        query = (
            "SELECT transaction_date, transaction_type, amount "
            f"FROM ({_txn_source_sql(include_refresh_id)}) u "
            "ORDER BY transaction_date DESC, txn_id DESC "
            "LIMIT 1"
        )
        cur.execute(query, {"cluster_id": cluster_id, "refresh_id": include_refresh_id})
        row = cur.fetchone()
        if row is None:
            return None

        txn_date, txn_type, amount = row
        running = _compute_running_saldo(conn, cluster_id, include_refresh_id=include_refresh_id)
        return {"saldo": running, "transaction_date": txn_date}


def current_saldo_before_date(conn, cluster_id, cutoff_date, include_refresh_id=None):
    return _compute_running_saldo(conn, cluster_id, cutoff_date, include_refresh_id=include_refresh_id)


def source_transaction_range(
    conn,
    cluster_id,
    include_refresh_id=None,
    cutoff_date=None,
    cutoff_at=None,
):
    """Return the min/max transaction timestamp included by a saldo calculation."""
    if cutoff_date is not None and cutoff_at is not None:
        raise ValueError("cutoff_date and cutoff_at are mutually exclusive")
    opening_cutoff = cutoff_date
    if cutoff_at is not None:
        opening_cutoff = (
            cutoff_at.astimezone(LOCAL_TZ).date()
            if cutoff_at.tzinfo is not None
            else cutoff_at.date()
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT as_of_date FROM ({_checkpoint_source_sql()}) checkpoints "
            "WHERE cluster_id = %s AND as_of_date <= COALESCE(%s::date, 'infinity'::date) "
            "ORDER BY as_of_date DESC, priority DESC, override_id DESC LIMIT 1",
            (cluster_id, opening_cutoff),
        )
        opening = cur.fetchone()
        params = {"cluster_id": cluster_id, "refresh_id": include_refresh_id}
        filters = []
        if opening:
            filters.append("(transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %(opening_as_of)s")
            params["opening_as_of"] = opening[0]
        if cutoff_date is not None:
            filters.append("(transaction_date AT TIME ZONE 'Asia/Makassar')::date < %(cutoff_date)s")
            params["cutoff_date"] = cutoff_date
        if cutoff_at is not None:
            filters.append("transaction_date <= %(cutoff_at)s")
            params["cutoff_at"] = cutoff_at
        source = _txn_source_sql(include_refresh_id)
        where = " AND ".join(filters)
        if where:
            where = " AND " + where
        cur.execute(
            f"SELECT MIN(transaction_date), MAX(transaction_date) "
            f"FROM ({source}) u WHERE 1=1{where}",
            params,
        )
        row = cur.fetchone()
    return row if row else (None, None)


def summary_by_cluster(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cluster_id,
                COUNT(*) AS transaction_count,
                SUM(CASE WHEN transaction_type = 'Kredit' THEN amount ELSE 0 END) AS total_kredit,
                SUM(CASE WHEN transaction_type = 'Debit' THEN amount ELSE 0 END) AS total_debit,
                MIN(transaction_date) AS first_transaction,
                MAX(transaction_date) AS last_transaction,
                COUNT(*) FILTER (WHERE txn_id NOT IN (SELECT txn_id FROM finpay_topup_classification)) AS unclassified_count,
                MAX(loaded_at) AS last_loaded_at
            FROM finpay_topup_txn
            GROUP BY cluster_id
            ORDER BY cluster_id
            """
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        for r in rows:
            r["current_saldo"] = _compute_running_saldo(conn, r["cluster_id"])
            r["rows"] = r.pop("transaction_count")
            r["first_tx"] = r.pop("first_transaction")
            r["last_tx"] = r.pop("last_transaction")
        return rows


def update_opening_balance_from_latest(conn, *, commit=True):
    """Checkpoint the final balance for the next calendar day.

    ``as_of_date`` represents the opening balance before transactions on that
    date.  Writing the checkpoint on the following day keeps the current day's
    transactions in the running-saldo calculation and avoids double counting.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (cluster_id) cluster_id, transaction_date
            FROM finpay_topup_txn
            ORDER BY cluster_id, transaction_date DESC, txn_id DESC
            """
        )
        for row in cur.fetchall():
            cluster_id = row[0]
            txn_date = row[1]
            saldo = _compute_running_saldo(conn, cluster_id)
            local_date = (
                txn_date.astimezone(LOCAL_TZ).date()
                if txn_date.tzinfo is not None
                else txn_date.date()
            )
            as_of_date = local_date + timedelta(days=1)
            cur.execute(
                "INSERT INTO finpay_cluster_balance (cluster_id, as_of_date, opening_balance) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (cluster_id, as_of_date) DO UPDATE SET opening_balance = EXCLUDED.opening_balance",
                (cluster_id, as_of_date, saldo),
            )
    if commit:
        conn.commit()


def update_opening_balance_before_date(conn, cutoff_date, *, cluster_ids=None, commit=True):
    """Checkpoint an accepted refresh without moving past a newer boundary."""
    cluster_ids = list(dict.fromkeys(str(cluster_id) for cluster_id in (cluster_ids or [])))
    with conn.cursor() as cur:
        if cluster_ids:
            target_clusters = cluster_ids
        else:
            cur.execute(
                "SELECT DISTINCT cluster_id FROM finpay_topup_txn "
                "WHERE (transaction_date AT TIME ZONE 'Asia/Makassar')::date < %s",
                (cutoff_date,),
            )
            target_clusters = [row[0] for row in cur.fetchall()]
        for cluster_id in target_clusters:
            cur.execute(
                f"SELECT as_of_date FROM ({_checkpoint_source_sql()}) checkpoints "
                "WHERE cluster_id = %s ORDER BY as_of_date DESC, priority DESC, override_id DESC LIMIT 1",
                (cluster_id,),
            )
            latest = cur.fetchone()
            if latest and latest[0] > cutoff_date:
                continue
            saldo = _compute_running_saldo(conn, cluster_id, cutoff_date)
            cur.execute(
                "INSERT INTO finpay_cluster_balance (cluster_id, as_of_date, opening_balance) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (cluster_id, as_of_date) DO UPDATE SET opening_balance = EXCLUDED.opening_balance",
                (cluster_id, cutoff_date, saldo),
            )
    if commit:
        conn.commit()
