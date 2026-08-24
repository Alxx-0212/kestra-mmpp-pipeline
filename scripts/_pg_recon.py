import psycopg
from datetime import datetime
from finpay_topup_pipeline.io_xlsx import parse_morowali_xlsx

dsn = "host=localhost port=5433 dbname=finpay_topup_test user=finpay password=finpay"
conn = psycopg.connect(dsn)
cur = conn.cursor()
cur.execute(
    "SELECT count(*), sum(CASE transaction_type WHEN 'Kredit' THEN amount ELSE -amount END), "
    "min(transaction_date), max(transaction_date) FROM finpay_topup_txn WHERE cluster_id='MOROWALI'"
)
cnt, net, mn, mx = cur.fetchone()
print("MOROWALI txns=", cnt, "db_net=", net, "min=", mn, "max=", mx)
cur.execute("SELECT opening_balance FROM finpay_cluster_balance WHERE cluster_id='MOROWALI'")
print("opening rows=", cur.fetchall())
cur.execute(
    "SELECT running_saldo FROM finpay_topup_saldo WHERE cluster_id='MOROWALI' "
    "ORDER BY transaction_date DESC, txn_id DESC LIMIT 1"
)
print("computed final=", cur.fetchone())
conn.close()

parsed = parse_morowali_xlsx("data/FINPAY MOROWALI.xlsx", "MOROWALI")
mor = [t for t in parsed["txns"] if t["cluster_id"] == "MOROWALI"]
last = max(mor, key=lambda t: (t["transaction_date"] or datetime.min))
print("legacy last saldo=", last["saldo"], "at", last["transaction_date"])
pnet = sum(t["amount"] if t["transaction_type"] == "Kredit" else -t["amount"] for t in mor)
print("parsed python net=", pnet, "=> opening+net=", 9998750 + pnet)
print("distinct transaction_types=", set(t["transaction_type"] for t in mor))
