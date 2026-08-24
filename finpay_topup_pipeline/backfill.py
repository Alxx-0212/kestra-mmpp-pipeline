from .config import TABLE_TXN, TABLE_BALANCE
from .io_xlsx import parse_morowali_xlsx
from .loader import row_hash

INSERT_TXN = None
UPSERT_BALANCE = None


def _txn_sql():
    global INSERT_TXN
    if INSERT_TXN is None:
        INSERT_TXN = (
            f"INSERT INTO {TABLE_TXN} (cluster_id, transaction_date, sender, receiver, "
            f"transaction_type, amount, currency, remarks, source_file, row_hash) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"ON CONFLICT (row_hash) DO NOTHING"
        )
    return INSERT_TXN


def _balance_sql():
    global UPSERT_BALANCE
    if UPSERT_BALANCE is None:
        UPSERT_BALANCE = (
            f"INSERT INTO {TABLE_BALANCE} (cluster_id, as_of_date, opening_balance, note) "
            f"VALUES (%s,%s,%s,%s) "
            f"ON CONFLICT (cluster_id, as_of_date) DO UPDATE "
            f"SET opening_balance = EXCLUDED.opening_balance, note = EXCLUDED.note"
        )
    return UPSERT_BALANCE


def backfill_xlsx(
    conn,
    path,
    default_cluster_id="MOROWALI",
    source_file=None,
    before_date=None,
    from_date=None,
    seed_openings=True,
):
    parsed = parse_morowali_xlsx(path, default_cluster_id)
    src = source_file or str(path)
    if before_date is not None and not hasattr(before_date, "isoformat"):
        from datetime import date
        before_date = date.fromisoformat(str(before_date))
    if from_date is not None and not hasattr(from_date, "isoformat"):
        from datetime import date
        from_date = date.fromisoformat(str(from_date))
    txn_inserted = 0
    txns_seen = 0
    with conn.cursor() as cur:
        for t in parsed["txns"]:
            if from_date is not None and t["transaction_date"].date() < from_date:
                continue
            if before_date is not None and t["transaction_date"].date() >= before_date:
                continue
            txns_seen += 1
            h = row_hash(t["cluster_id"], t)
            cur.execute(
                _txn_sql(),
                (
                    t["cluster_id"],
                    t["transaction_date"],
                    t["sender"],
                    t["receiver"],
                    t["transaction_type"],
                    t["amount"],
                    t["currency"],
                    t["remarks"],
                    src,
                    h,
                ),
            )
            if cur.rowcount:
                txn_inserted += 1
        openings = {}
        if seed_openings:
            for o in parsed["openings"]:
                # Preserve dated balance checkpoints; later checkpoints are useful
                # when the legacy workbook contains more than one opening balance.
                openings[(o["cluster_id"], o["as_of_date"])] = o
            for o in openings.values():
                cur.execute(
                    _balance_sql(),
                    (o["cluster_id"], o["as_of_date"], o["opening_balance"], o["note"]),
                )
    conn.commit()
    return {
        "txns": txns_seen,
        "txn_inserted": txn_inserted,
        "openings_seeded": len(openings),
    }
