from .config import TABLE_TXN, TABLE_BALANCE
from .io_xlsx import parse_morowali_xlsx
from .saldo import _compute_running_saldo


def reconcile_xlsx(conn, path, default_cluster_id="MOROWALI", tolerance=0.5):
    parsed = parse_morowali_xlsx(path, default_cluster_id)
    mismatches = []
    clusters = sorted({t["cluster_id"] for t in parsed["txns"]})
    with conn.cursor() as cur:
        for cluster_id in clusters:
            legacy = [t for t in parsed["txns"] if t["cluster_id"] == cluster_id]
            computed_saldos = []
            # Get opening balance for this cluster
            cur.execute(
                "SELECT opening_balance FROM finpay_cluster_balance "
                "WHERE cluster_id = %s ORDER BY as_of_date DESC LIMIT 1",
                (cluster_id,),
            )
            row = cur.fetchone()
            opening = row[0] if row else 0

            # Get all transactions for this cluster in order
            cur.execute(
                "SELECT transaction_date, transaction_type, amount "
                "FROM finpay_topup_txn "
                "WHERE cluster_id = %s "
                "ORDER BY transaction_date, txn_id",
                (cluster_id,),
            )
            saldo = opening
            for row in cur.fetchall():
                txn_date, txn_type, amount = row
                if txn_type == "Kredit":
                    saldo += amount
                elif txn_type == "Debit":
                    saldo -= amount
                computed_saldos.append(saldo)

            for i, t in enumerate(legacy):
                if i >= len(computed_saldos):
                    mismatches.append({
                        "cluster_id": cluster_id,
                        "index": i,
                        "date": t["transaction_date"].isoformat() if t["transaction_date"] else None,
                        "legacy": float(t.get("saldo") or 0),
                        "computed": None,
                        "diff": None,
                    })
                    continue
                c = float(computed_saldos[i])
                lg = float(t.get("saldo") or 0)
                if abs(c - lg) > tolerance:
                    mismatches.append({
                        "cluster_id": cluster_id,
                        "index": i,
                        "date": t["transaction_date"].isoformat() if t["transaction_date"] else None,
                        "legacy": lg,
                        "computed": c,
                        "diff": round(c - lg, 2),
                    })
    return mismatches