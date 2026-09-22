CREATE TABLE linkaja_source_loads (
    load_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 TEXT,
    source_start_date DATE,
    source_end_date DATE,
    row_count INTEGER NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMPTZ,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    supersedes_load_id TEXT,
    validation_status TEXT NOT NULL DEFAULT 'VALID'
);

CREATE INDEX linkaja_source_loads_current_idx
ON linkaja_source_loads (cluster_id, source_start_date, source_end_date, ingested_at DESC)
WHERE is_current;

CREATE TABLE linkaja_ledger_versions (
    cluster_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    load_id TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_no BIGINT,
    top_organization TEXT,
    parent_organization TEXT,
    organization TEXT,
    transaction_id TEXT NOT NULL,
    original_transaction_id TEXT,
    partner_reference_number TEXT,
    invoice_id TEXT,
    finalized_date DATE NOT NULL,
    finalized_time TIME NOT NULL,
    initiate_date DATE,
    initiate_time TIME,
    transaction_type TEXT,
    transaction_scenario TEXT NOT NULL,
    transaction_status TEXT,
    transaction_statement TEXT,
    account TEXT,
    counter_party TEXT,
    debit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    credit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    balance NUMERIC(20, 2),
    fee NUMERIC(20, 2),
    source_row_hash TEXT,
    PRIMARY KEY (load_id, source_row_number)
);

CREATE INDEX linkaja_ledger_versions_cluster_transaction_idx
ON linkaja_ledger_versions (cluster_id, transaction_id, finalized_date);

CREATE INDEX linkaja_ledger_versions_cluster_original_idx
ON linkaja_ledger_versions (cluster_id, original_transaction_id, finalized_date)
WHERE original_transaction_id IS NOT NULL;

CREATE TABLE linkaja_monthly_impacts (
    impact_id BIGSERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    transaction_id TEXT NOT NULL,
    old_load_id TEXT,
    new_load_id TEXT,
    impact_reason TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX linkaja_monthly_impacts_cluster_month_idx
ON linkaja_monthly_impacts (cluster_id, report_month, detected_at DESC);
