import hashlib

from .config import TABLE_TXN
from .io_csv import parse_topup_csv


def row_hash(cluster_id, row):
    key = "|".join([
        cluster_id,
        row["transaction_date"].isoformat() if row["transaction_date"] else "",
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
