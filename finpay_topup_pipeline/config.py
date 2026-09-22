import os
from pathlib import Path


def dsn_from_env(prefix="FINPAY_DB_"):
    host = os.environ.get(prefix + "HOST", "localhost")
    port = os.environ.get(prefix + "PORT", "5432")
    name = os.environ.get(prefix + "NAME", "finpay")
    user = os.environ.get(prefix + "USER", "finpay")
    pw = os.environ.get(prefix + "PASSWORD")
    if pw is None:
        password_file = os.environ.get(prefix + "PASSWORD_FILE")
        pw = Path(password_file).read_text(encoding="utf-8").strip() if password_file else ""
    return f"host={host} port={port} dbname={name} user={user} password={pw}"


TABLE_TXN = "finpay_topup_txn"
TABLE_TXN_STAGING = "finpay_topup_txn_staging"
TABLE_CLASS = "finpay_topup_classification"
TABLE_OUTLET = "finpay_outlet"
TABLE_BALANCE = "finpay_cluster_balance"
TABLE_REFRESH = "finpay_topup_refresh"
TABLE_REFRESH_CLUSTER = "finpay_topup_refresh_cluster"
TABLE_BUCKET_SNAPSHOT = "finpay_bucket_topup_snapshot"
TABLE_CLUSTER_OUTLET = "finpay_cluster_outlet"
TABLE_MANUAL_ADJUSTMENT = "finpay_topup_manual_adjustment"
TABLE_CHECKPOINT_OVERRIDE = "finpay_topup_checkpoint_override"
TABLE_CORRECTION_AUDIT = "finpay_topup_correction_audit"
