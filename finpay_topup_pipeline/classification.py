from .config import TABLE_CLASS, TABLE_TXN

UPSERT_CLASS = None


def _upsert_sql():
    global UPSERT_CLASS
    if UPSERT_CLASS is None:
        UPSERT_CLASS = (
            f"INSERT INTO {TABLE_CLASS} (txn_id, category, note, classified_by) "
            f"VALUES (%s,%s,%s,%s) "
            f"ON CONFLICT (txn_id) DO UPDATE SET category = EXCLUDED.category, "
            f"note = EXCLUDED.note, classified_by = EXCLUDED.classified_by, "
            f"classified_at = now()"
        )
    return UPSERT_CLASS


def upsert_classification(conn, txn_id, category, note=None, by=None):
    sql = _upsert_sql()
    with conn.cursor() as cur:
        cur.execute(sql, (txn_id, category, note, by))
    conn.commit()


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
