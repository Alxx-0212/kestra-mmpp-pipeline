/* Daily evidence only. Monthly facts/publications live in 002. */

CREATE TABLE IF NOT EXISTS finpay_source_loads (
    load_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    report_date DATE NOT NULL,
    source_start_date DATE,
    source_end_date DATE,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    supersedes_load_id TEXT
);

CREATE INDEX IF NOT EXISTS finpay_source_loads_current_idx
    ON finpay_source_loads (cluster_id, report_date, loaded_at DESC)
    WHERE is_current;

CREATE TABLE IF NOT EXISTS finpay_ledger_events (
    load_id TEXT NOT NULL REFERENCES finpay_source_loads(load_id),
    source_row_number INTEGER NOT NULL CHECK (source_row_number > 0),
    source_row_hash TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    transaction_date TIMESTAMP NOT NULL,
    transaction_id TEXT NOT NULL,
    base_id TEXT,
    transaction_id_type TEXT,
    raw_transaction_label TEXT,
    transaction_type TEXT,
    remarks TEXT,
    kredit NUMERIC(18, 2) NOT NULL DEFAULT 0,
    debet NUMERIC(18, 2) NOT NULL DEFAULT 0,
    saldo_awal NUMERIC(18, 2),
    saldo_akhir NUMERIC(18, 2),
    nomor_rs TEXT,
    PRIMARY KEY (load_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS finpay_ledger_events_transaction_idx
    ON finpay_ledger_events (cluster_id, transaction_id, transaction_date);

CREATE INDEX IF NOT EXISTS finpay_ledger_events_report_date_idx
    ON finpay_ledger_events (cluster_id, report_date, transaction_date);
