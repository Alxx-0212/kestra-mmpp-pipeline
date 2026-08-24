import os


def dsn_from_env(prefix="FINPAY_DB_"):
    host = os.environ.get(prefix + "HOST", "localhost")
    port = os.environ.get(prefix + "PORT", "5432")
    name = os.environ.get(prefix + "NAME", "finpay")
    user = os.environ.get(prefix + "USER", "finpay")
    pw = os.environ.get(prefix + "PASSWORD", "")
    return f"host={host} port={port} dbname={name} user={user} password={pw}"


TABLE_TXN = "finpay_topup_txn"
TABLE_CLASS = "finpay_topup_classification"
TABLE_BALANCE = "finpay_cluster_balance"
TABLE_REFRESH = "finpay_topup_refresh"
TABLE_REFRESH_CLUSTER = "finpay_topup_refresh_cluster"
TABLE_BUCKET_SNAPSHOT = "finpay_bucket_topup_snapshot"