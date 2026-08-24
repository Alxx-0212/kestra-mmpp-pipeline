from .config import TABLE_TXN, TABLE_BALANCE, TABLE_CLASS


def _compute_running_saldo(conn, cluster_id, cutoff_date=None):
    """Compute running saldo for a cluster up to cutoff_date (exclusive).

    Returns None if no opening balance and no transactions exist for the cluster.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT opening_balance, as_of_date FROM finpay_cluster_balance "
            "WHERE cluster_id = %s AND as_of_date <= COALESCE(%s, 'infinity'::date) "
            "ORDER BY as_of_date DESC LIMIT 1",
            (cluster_id, cutoff_date),
        )
        row = cur.fetchone()
        opening = row[0] if row else None
        opening_as_of = row[1] if row else None

        query = """
            SELECT transaction_date, transaction_type, amount
            FROM finpay_topup_txn
            WHERE cluster_id = %s
        """
        params = [cluster_id]
        
        # If we have an opening balance checkpoint, only include transactions
        # on or after the checkpoint's as_of_date (the checkpoint represents
        # the balance BEFORE transactions on that date)
        if opening_as_of is not None:
            query += " AND transaction_date::date >= %s"
            params.append(opening_as_of)
        
        if cutoff_date:
            query += " AND transaction_date::date < %s"
            params.append(cutoff_date)
        query += " ORDER BY transaction_date, txn_id"

        cur.execute(query, params)
        rows = cur.fetchall()

        if opening is None and not rows:
            return None

        saldo = opening if opening is not None else 0
        for row in rows:
            if row[1] == "Kredit":
                saldo += row[2]
            elif row[1] == "Debit":
                saldo -= row[2]
        return saldo


def current_saldo(conn, cluster_id):
    return _compute_running_saldo(conn, cluster_id)


def latest_saldo_snapshot(conn, cluster_id):
    """Return the balance and source timestamp used for a live CMS comparison."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT transaction_date, transaction_type, amount
            FROM finpay_topup_txn
            WHERE cluster_id = %s
            ORDER BY transaction_date DESC, txn_id DESC
            LIMIT 1
            """,
            (cluster_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        txn_date, txn_type, amount = row
        running = _compute_running_saldo(conn, cluster_id)
        return {"saldo": running, "transaction_date": txn_date}


def current_saldo_before_date(conn, cluster_id, cutoff_date):
    return _compute_running_saldo(conn, cluster_id, cutoff_date)


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


def update_opening_balance_from_latest(conn):
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
            as_of_date = txn_date.date() + __import__("datetime").timedelta(days=1)
            cur.execute(
                "INSERT INTO finpay_cluster_balance (cluster_id, as_of_date, opening_balance) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (cluster_id, as_of_date) DO UPDATE SET opening_balance = EXCLUDED.opening_balance",
                (cluster_id, as_of_date, saldo),
            )
    conn.commit()


def update_opening_balance_before_date(conn, cutoff_date):
    """Checkpoint the verified closed-day balance as the cutoff date opening."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT cluster_id FROM finpay_topup_txn WHERE transaction_date::date < %s",
            (cutoff_date,),
        )
        for row in cur.fetchall():
            cluster_id = row[0]
            saldo = _compute_running_saldo(conn, cluster_id, cutoff_date)
            cur.execute(
                "INSERT INTO finpay_cluster_balance (cluster_id, as_of_date, opening_balance) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (cluster_id, as_of_date) DO UPDATE SET opening_balance = EXCLUDED.opening_balance",
                (cluster_id, cutoff_date, saldo),
            )
    conn.commit()