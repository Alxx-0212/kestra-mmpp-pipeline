import psycopg
from finpay_topup_pipeline.schema import ensure_schema

dsn = "host=localhost port=5433 dbname=finpay_topup_test user=finpay password=finpay"
conn = psycopg.connect(dsn, connect_timeout=10)
try:
    ensure_schema(conn)
    print("ensure_schema OK")
except Exception as e:
    print("ensure_schema ERROR:", repr(e))
    try:
        conn.rollback()
    except Exception:
        pass
try:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE finpay_topup_txn, finpay_topup_classification, finpay_cluster_balance RESTART IDENTITY CASCADE")
    conn.commit()
    print("truncate OK")
except Exception as e:
    print("truncate ERROR:", repr(e))
