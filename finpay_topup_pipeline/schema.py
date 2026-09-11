from .config import (
    TABLE_TXN,
    TABLE_TXN_STAGING,
    TABLE_CLASS,
    TABLE_OUTLET,
    TABLE_BALANCE,
    TABLE_REFRESH,
    TABLE_REFRESH_CLUSTER,
    TABLE_BUCKET_SNAPSHOT,
    TABLE_CLUSTER_OUTLET,
    TABLE_MANUAL_ADJUSTMENT,
    TABLE_CHECKPOINT_OVERRIDE,
)
from .legacy_outlet import LEGACY_OUTLETS_BY_CLUSTER, outlet_code

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

DDL_TXN_STAGING = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TXN_STAGING} (
    txn_id BIGSERIAL PRIMARY KEY,
    refresh_id BIGINT NOT NULL,
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
    row_hash TEXT NOT NULL
);
"""

DDL_OUTLET = f"""
CREATE TABLE IF NOT EXISTS {TABLE_OUTLET} (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true
);
"""

DDL_CLUSTER_OUTLET = f"""
CREATE TABLE IF NOT EXISTS {TABLE_CLUSTER_OUTLET} (
    cluster_id TEXT NOT NULL,
    outlet_code TEXT NOT NULL REFERENCES {TABLE_OUTLET}(code),
    source_label TEXT NOT NULL,
    source_file TEXT,
    confidence TEXT NOT NULL DEFAULT 'exact',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, outlet_code)
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

DDL_CLASS_ALTERS = f"""
ALTER TABLE {TABLE_CLASS}
    ADD COLUMN IF NOT EXISTS outlet_code TEXT REFERENCES {TABLE_OUTLET}(code),
    ADD COLUMN IF NOT EXISTS source_label TEXT,
    ADD COLUMN IF NOT EXISTS classification_source TEXT,
    ADD COLUMN IF NOT EXISTS classification_confidence TEXT;
"""

DDL_MANUAL_ADJUSTMENT = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MANUAL_ADJUSTMENT} (
    adjustment_id BIGSERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('Kredit', 'Debit')),
    amount NUMERIC(18,2) NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'IDR',
    sender TEXT,
    receiver TEXT,
    remarks TEXT,
    reason TEXT NOT NULL,
    source_reference TEXT,
    adjustment_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'VOID')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    voided_by TEXT,
    voided_at TIMESTAMPTZ
);
"""

DDL_CHECKPOINT_OVERRIDE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_CHECKPOINT_OVERRIDE} (
    override_id BIGSERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    opening_balance NUMERIC(18,2) NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'VOID')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by TEXT,
    approved_at TIMESTAMPTZ
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
    refresh_mode TEXT NOT NULL DEFAULT 'FULL',
    status TEXT NOT NULL DEFAULT 'REQUESTED',
    error_message TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kestra_execution_id TEXT,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ
);
"""

DDL_REFRESH_CLUSTER = f"""
CREATE TABLE IF NOT EXISTS {TABLE_REFRESH_CLUSTER} (
    refresh_id BIGINT NOT NULL REFERENCES {TABLE_REFRESH}(refresh_id) ON DELETE CASCADE,
    cluster_id TEXT NOT NULL,
    checkpoint_as_of_date DATE,
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
    source_transaction_min_at TIMESTAMPTZ,
    source_transaction_max_at TIMESTAMPTZ,
    calculation_cutoff_at TIMESTAMPTZ,
    calculated_at TIMESTAMPTZ,
    calculation_row_count INTEGER,
    matching_row_number INTEGER,
    matching_type TEXT,
    matching_transaction_id BIGINT,
    matching_row_hash TEXT,
    matching_transaction_at TIMESTAMPTZ,
    matching_running_saldo NUMERIC(18,2),
    status TEXT NOT NULL DEFAULT 'REQUESTED',
    error_message TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (refresh_id, cluster_id)
);
"""

DDL_REFRESH_ALTERS = f"""
ALTER TABLE {TABLE_REFRESH}
    ADD COLUMN IF NOT EXISTS reviewed_by TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS refresh_mode TEXT NOT NULL DEFAULT 'FULL';

ALTER TABLE {TABLE_REFRESH_CLUSTER}
    ADD COLUMN IF NOT EXISTS checkpoint_as_of_date DATE,
    ADD COLUMN IF NOT EXISTS bucket_reference_date DATE,
    ADD COLUMN IF NOT EXISTS verification_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source_transaction_min_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_transaction_max_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS calculation_cutoff_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS calculation_row_count INTEGER,
    ADD COLUMN IF NOT EXISTS matching_row_number INTEGER,
    ADD COLUMN IF NOT EXISTS matching_type TEXT,
    ADD COLUMN IF NOT EXISTS matching_transaction_id BIGINT,
    ADD COLUMN IF NOT EXISTS matching_row_hash TEXT,
    ADD COLUMN IF NOT EXISTS matching_transaction_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS matching_running_saldo NUMERIC(18,2);
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

DDL_INDEXES = f"""
CREATE INDEX IF NOT EXISTS finpay_topup_txn_cluster_date_idx
    ON {TABLE_TXN} (cluster_id, transaction_date, txn_id);
CREATE INDEX IF NOT EXISTS finpay_topup_txn_date_idx
    ON {TABLE_TXN} (transaction_date);
CREATE INDEX IF NOT EXISTS finpay_topup_txn_staging_refresh_idx
    ON {TABLE_TXN_STAGING} (refresh_id);
CREATE INDEX IF NOT EXISTS finpay_topup_txn_staging_cluster_date_idx
    ON {TABLE_TXN_STAGING} (cluster_id, transaction_date);
CREATE UNIQUE INDEX IF NOT EXISTS finpay_topup_txn_staging_refresh_hash_idx
    ON {TABLE_TXN_STAGING} (refresh_id, row_hash);
CREATE INDEX IF NOT EXISTS finpay_topup_classification_category_idx
    ON {TABLE_CLASS} (category);
CREATE INDEX IF NOT EXISTS finpay_topup_classification_outlet_idx
    ON {TABLE_CLASS} (outlet_code);
CREATE INDEX IF NOT EXISTS finpay_cluster_outlet_active_idx
    ON {TABLE_CLUSTER_OUTLET} (cluster_id, active);
CREATE INDEX IF NOT EXISTS finpay_manual_adjustment_scope_idx
    ON {TABLE_MANUAL_ADJUSTMENT} (cluster_id, effective_at)
    WHERE status = 'APPROVED';
CREATE INDEX IF NOT EXISTS finpay_checkpoint_override_scope_idx
    ON {TABLE_CHECKPOINT_OVERRIDE} (cluster_id, as_of_date, approved_at DESC)
    WHERE status = 'APPROVED';
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
        cur.execute(DDL_TXN_STAGING)
        cur.execute(DDL_OUTLET)
        cur.execute(DDL_CLUSTER_OUTLET)
        cur.execute(DDL_CLASS)
        cur.execute(DDL_CLASS_ALTERS)
        cur.execute(DDL_BALANCE)
        cur.execute(DDL_REFRESH)
        cur.execute(DDL_REFRESH_CLUSTER)
        cur.execute(DDL_REFRESH_ALTERS)
        cur.execute(DDL_BUCKET_SNAPSHOT)
        cur.execute(DDL_MANUAL_ADJUSTMENT)
        cur.execute(DDL_CHECKPOINT_OVERRIDE)
        cur.execute(DDL_INDEXES)
        outlet_rows = [
            (outlet_code(cluster_id, label), label)
            for cluster_id, labels in LEGACY_OUTLETS_BY_CLUSTER.items()
            for label in sorted(labels)
        ]
        cur.executemany(
            f"INSERT INTO {TABLE_OUTLET} (code, label, active) VALUES (%s,%s,true) "
            "ON CONFLICT (code) DO UPDATE SET label=EXCLUDED.label, active=true",
            outlet_rows,
        )
        cur.executemany(
            f"INSERT INTO {TABLE_CLUSTER_OUTLET} "
            "(cluster_id, outlet_code, source_label, source_file, confidence, active) "
            "VALUES (%s,%s,%s,'finance_authoritative_2026-09-09','exact',true) "
            "ON CONFLICT (cluster_id, outlet_code) DO UPDATE SET "
            "source_label=EXCLUDED.source_label, source_file=EXCLUDED.source_file, "
            "confidence='exact', active=true",
            [
                (cluster_id, outlet_code(cluster_id, label), label)
                for cluster_id, labels in LEGACY_OUTLETS_BY_CLUSTER.items()
                for label in sorted(labels)
            ],
        )
    conn.commit()
