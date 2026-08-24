from .config import (
    TABLE_TXN,
    TABLE_CLASS,
    TABLE_BALANCE,
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
    TABLE_BUCKET_SNAPSHOT,
)

DDL_TXN = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TXN} (
    txn_id BIGSERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    transaction_date TIMESTAMPTZ NOT NULL,
    sender TEXT,
    receiver TEXT,
    transaction_type TEXT NOT NULL,
    amount NUMERIC(18,2) NOT NULL,
    currency TEXT,
    remarks TEXT,
    source_file TEXT,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_hash TEXT NOT NULL UNIQUE
);
"""

DDL_CLASS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_CLASS} (
    txn_id BIGINT PRIMARY KEY REFERENCES {TABLE_TXN}(txn_id),
    category TEXT,
    note TEXT,
    classified_by TEXT,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DDL_BALANCE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BALANCE} (
    cluster_id TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    opening_balance NUMERIC(18,2) NOT NULL,
    note TEXT,
    PRIMARY KEY (cluster_id, as_of_date)
);
"""

DDL_REFRESH = f"""
CREATE TABLE IF NOT EXISTS {TABLE_REFRESH} (
    refresh_id BIGSERIAL PRIMARY KEY,
    requested_start DATE NOT NULL,
    requested_end DATE NOT NULL,
    requested_users TEXT[] NOT NULL,
    requested_by TEXT,
    trigger_source TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'REQUESTED',
    error_message TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kestra_execution_id TEXT
);
"""

DDL_REFRESH_CLUSTER = f"""
CREATE TABLE IF NOT EXISTS {TABLE_REFRESH_CLUSTER} (
    refresh_id BIGINT NOT NULL REFERENCES {TABLE_REFRESH}(refresh_id) ON DELETE CASCADE,
    cluster_id TEXT NOT NULL,
    source_row_count INTEGER,
    loaded_seen INTEGER,
    loaded_inserted INTEGER,
    computed_saldo NUMERIC(18,2),
    bucket_topup_value NUMERIC(18,2),
    bucket_topup_text TEXT,
    bucket_reference_date DATE,
    bucket_snapshot_at TIMESTAMPTZ,
    verification_diff NUMERIC(18,2),
    verification_attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REQUESTED',
    error_message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (refresh_id, cluster_id)
);
"""

DDL_REFRESH_ALTERS = f"""
ALTER TABLE {TABLE_REFRESH_CLUSTER}
    ADD COLUMN IF NOT EXISTS bucket_reference_date DATE,
    ADD COLUMN IF NOT EXISTS verification_attempts INTEGER NOT NULL DEFAULT 0;
"""

DDL_BUCKET_SNAPSHOT = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BUCKET_SNAPSHOT} (
    cluster_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    username TEXT,
    bucket_topup_value NUMERIC(18,2),
    bucket_topup_text TEXT,
    source_url TEXT,
    computed_saldo NUMERIC(18,2),
    verification_diff NUMERIC(18,2),
    status TEXT,
    PRIMARY KEY (cluster_id, report_date)
);
"""

DDL_REFRESH_ALTERS = f"""
ALTER TABLE {TABLE_REFRESH_CLUSTER}
    ADD COLUMN IF NOT EXISTS bucket_reference_date DATE,
    ADD COLUMN IF NOT EXISTS verification_attempts INTEGER NOT NULL DEFAULT 0;
"""

DDL_INDEXES = f"""
CREATE INDEX IF NOT EXISTS finpay_topup_txn_cluster_date_idx
    ON {TABLE_TXN} (cluster_id, transaction_date, txn_id);
CREATE INDEX IF NOT EXISTS finpay_topup_txn_date_idx
    ON {TABLE_TXN} (transaction_date);
CREATE INDEX IF NOT EXISTS finpay_topup_classification_category_idx
    ON {TABLE_CLASS} (category);
CREATE INDEX IF NOT EXISTS finpay_topup_refresh_status_idx
    ON {TABLE_REFRESH} (status, requested_at);
CREATE INDEX IF NOT EXISTS finpay_topup_refresh_cluster_status_idx
    ON {TABLE_REFRESH_CLUSTER} (cluster_id, status);
"""


def ensure_schema(conn):
    import psycopg
    if isinstance(conn, psycopg.Connection):
        pass
    with conn.cursor() as cur:
        cur.execute(DDL_TXN)
        cur.execute(DDL_CLASS)
        cur.execute(DDL_BALANCE)
        cur.execute(DDL_REFRESH)
        cur.execute(DDL_REFRESH_CLUSTER)
        cur.execute(DDL_REFRESH_ALTERS)
        cur.execute(DDL_INDEXES)
        cur.execute(DDL_BUCKET_SNAPSHOT)
    conn.commit()