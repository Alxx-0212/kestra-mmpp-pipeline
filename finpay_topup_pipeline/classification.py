from .config import TABLE_CLASS, TABLE_CLUSTER_OUTLET, TABLE_OUTLET, TABLE_TXN

UPSERT_CLASS = None


def _upsert_sql():
    global UPSERT_CLASS
    if UPSERT_CLASS is None:
        UPSERT_CLASS = (
            f"INSERT INTO {TABLE_CLASS} (txn_id, category, note, classified_by, outlet_code) "
            f"VALUES (%s,%s,%s,%s,%s) "
            f"ON CONFLICT (txn_id) DO UPDATE SET category = EXCLUDED.category, "
            f"note = EXCLUDED.note, classified_by = EXCLUDED.classified_by, "
            f"outlet_code = COALESCE(EXCLUDED.outlet_code, {TABLE_CLASS}.outlet_code), "
            f"classified_at = now()"
        )
    return UPSERT_CLASS


def upsert_classification(conn, txn_id, category, note=None, by=None, outlet_code=None):
    sql = _upsert_sql()
    with conn.cursor() as cur:
        cur.execute(sql, (txn_id, category, note, by, outlet_code))
    conn.commit()


def classify_for_cluster(conn, txn_id, cluster_id, outlet_code, by=None):
    """Classify one displayed Kredit only when its scope is still valid.

    The transaction row lock makes the membership, type, classification, and
    active-outlet checks one atomic decision for callback retries/races.
    A missing cluster is intentionally a non-match; an unscoped callback must
    never turn into an unrestricted transaction lookup.
    """
    scope_sql = " AND t.cluster_id = %s"
    params = [outlet_code, txn_id, cluster_id]
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT t.txn_id, o.code, o.label "
                f"FROM {TABLE_TXN} t "
                f"JOIN {TABLE_OUTLET} o ON o.code = %s AND o.active "
                f"LEFT JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id "
                f"WHERE t.txn_id = %s{scope_sql} "
                f"AND t.transaction_type = 'Kredit' AND c.txn_id IS NULL "
                f"AND ("
                f"(EXISTS (SELECT 1 FROM {TABLE_CLUSTER_OUTLET} scope WHERE scope.cluster_id = t.cluster_id AND scope.active) "
                f"AND EXISTS (SELECT 1 FROM {TABLE_CLUSTER_OUTLET} mapped WHERE mapped.cluster_id = t.cluster_id "
                f"AND mapped.outlet_code = o.code AND mapped.active)) "
                f"OR NOT EXISTS (SELECT 1 FROM {TABLE_CLUSTER_OUTLET} scope WHERE scope.cluster_id = t.cluster_id AND scope.active)"
                f") "
                f"FOR UPDATE OF t, o",
                params,
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return None
            cur.execute(
                f"INSERT INTO {TABLE_CLASS} "
                f"(txn_id, category, note, classified_by, outlet_code) "
                f"VALUES (%s, NULL, NULL, %s, %s) "
                f"ON CONFLICT (txn_id) DO NOTHING "
                f"RETURNING txn_id",
                (txn_id, by, outlet_code),
            )
            if cur.fetchone() is None:
                conn.commit()
                return None
        conn.commit()
        return {"txn_id": row[0], "code": row[1], "label": row[2]}
    except Exception:
        conn.rollback()
        raise


def active_outlets(conn, cluster_id=None):
    """Return the active outlet enum as ``[{"code", "label"}]`` ordered by code.

    The bot renders one inline-keyboard button per row; swapping placeholder
    outlets for the real list is a data change in finpay_outlet only.
    """
    with conn.cursor() as cur:
        if cluster_id is not None:
            cur.execute(
                f"SELECT o.code, o.label FROM {TABLE_CLUSTER_OUTLET} m "
                f"JOIN {TABLE_OUTLET} o ON o.code = m.outlet_code "
                "WHERE m.cluster_id = %s AND m.active AND o.active ORDER BY o.code",
                (cluster_id,),
            )
            rows = cur.fetchall()
            if rows:
                return [{"code": row[0], "label": row[1]} for row in rows]
            cur.execute(
                f"SELECT EXISTS (SELECT 1 FROM {TABLE_CLUSTER_OUTLET} WHERE active)"
            )
            if cur.fetchone()[0]:
                return []
        cur.execute(
            f"SELECT code, label FROM {TABLE_OUTLET} WHERE active ORDER BY code"
        )
        return [{"code": row[0], "label": row[1]} for row in cur.fetchall()]


def resolve_outlet(conn, code, cluster_id=None):
    for outlet in active_outlets(conn, cluster_id=cluster_id):
        if outlet["code"] == code:
            return outlet
    return None


UNCLASSIFIED_KREDIT_SQL = (
    f"SELECT t.txn_id, t.cluster_id, t.transaction_date, t.amount, t.remarks "
    f"FROM {TABLE_TXN} t LEFT JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id "
    f"WHERE t.transaction_type = 'Kredit' AND c.txn_id IS NULL"
)


def unclassified_kredit(
    conn,
    start_date=None,
    end_date=None,
    limit=50,
    cluster_id=None,
):
    """List unclassified Kredit transactions oldest-first for the bot walkthrough."""
    sql = UNCLASSIFIED_KREDIT_SQL
    params = []
    if cluster_id is not None:
        sql += " AND t.cluster_id = %s"
        params.append(cluster_id)
    if start_date is not None:
        sql += " AND (t.transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %s"
        params.append(start_date)
    if end_date is not None:
        sql += " AND (t.transaction_date AT TIME ZONE 'Asia/Makassar')::date <= %s"
        params.append(end_date)
    sql += " ORDER BY t.transaction_date, t.txn_id LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["transaction_date"] = r["transaction_date"].isoformat() if r["transaction_date"] else None
        r["amount"] = float(r["amount"]) if r["amount"] is not None else None
    return rows


def kredit_report_page(
    conn,
    start_date=None,
    end_date=None,
    cluster_id=None,
    limit=20,
    offset=0,
):
    """Return one page of all Kredit rows with their outlet classification."""
    sql = (
        f"SELECT t.txn_id, t.cluster_id, t.transaction_date, t.amount, "
        f"c.outlet_code, COALESCE(o.label, c.outlet_code) AS outlet_label "
        f"FROM {TABLE_TXN} t "
        f"LEFT JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id "
        f"LEFT JOIN {TABLE_OUTLET} o ON o.code = c.outlet_code "
        "WHERE t.transaction_type = 'Kredit'"
    )
    params = []
    if cluster_id is not None:
        sql += " AND t.cluster_id = %s"
        params.append(cluster_id)
    if start_date is not None:
        sql += " AND (t.transaction_date AT TIME ZONE 'Asia/Makassar')::date >= %s"
        params.append(start_date)
    if end_date is not None:
        sql += " AND (t.transaction_date AT TIME ZONE 'Asia/Makassar')::date <= %s"
        params.append(end_date)
    sql += " ORDER BY t.transaction_date, t.txn_id LIMIT %s OFFSET %s"
    params.extend([limit + 1, offset])
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["transaction_date"] = row["transaction_date"].isoformat() if row["transaction_date"] else None
        row["amount"] = float(row["amount"]) if row["amount"] is not None else None
    return {"rows": rows, "has_more": has_more}


def unclassified_kredit_counts(conn, cluster_id=None):
    """Return (total, last-7-days) counts of unclassified Kredit transactions."""
    cluster_sql = ""
    params = []
    if cluster_id is not None:
        cluster_sql = " AND t.cluster_id = %s"
        params.append(cluster_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE_TXN} t LEFT JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id "
            f"WHERE t.transaction_type = 'Kredit' AND c.txn_id IS NULL{cluster_sql}",
            params,
        )
        total = cur.fetchone()[0]
        recent_params = list(params)
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE_TXN} t LEFT JOIN {TABLE_CLASS} c ON c.txn_id = t.txn_id "
            f"WHERE t.transaction_type = 'Kredit' AND c.txn_id IS NULL "
            f"AND (t.transaction_date AT TIME ZONE 'Asia/Makassar')::date >= "
            f"(now() AT TIME ZONE 'Asia/Makassar')::date - INTERVAL '6 days'"
            f"{cluster_sql}",
            recent_params,
        )
        recent = cur.fetchone()[0]
    return int(total), int(recent)


def bulk_classify(conn, sql_filter, params, category, by=None):
    sql = (
        f"INSERT INTO {TABLE_CLASS} (txn_id, category, note, classified_by) "
        f"SELECT txn_id, %s, NULL, %s FROM {TABLE_TXN} WHERE {sql_filter} "
        f"ON CONFLICT (txn_id) DO UPDATE SET category = EXCLUDED.category, "
        f"classified_by = EXCLUDED.classified_by, classified_at = now()"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (category, by, *params))
    conn.commit()
