CREATE TABLE linkaja_raw_transactions (
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
    PRIMARY KEY (load_id, source_row_number)
);

CREATE INDEX linkaja_raw_cluster_date_transaction_idx
ON linkaja_raw_transactions (cluster_id, finalized_date, transaction_id);

CREATE INDEX linkaja_raw_cluster_transaction_idx
ON linkaja_raw_transactions (cluster_id, transaction_id);

CREATE INDEX linkaja_raw_cluster_original_transaction_idx
ON linkaja_raw_transactions (cluster_id, original_transaction_id)
WHERE original_transaction_id IS NOT NULL;

CREATE TABLE linkaja_transactions (
    cluster_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    finalized_at_local TIMESTAMP NOT NULL,
    transaction_id TEXT NOT NULL,
    original_transaction_id TEXT,
    partner_reference_number TEXT,
    invoice_id TEXT,
    transaction_type TEXT,
    transaction_scenario TEXT NOT NULL,
    transaction_status TEXT,
    counter_party TEXT,
    company_organization TEXT NOT NULL,
    company_account TEXT NOT NULL,
    company_debit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    company_credit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    signed_amount NUMERIC(20, 2) NOT NULL DEFAULT 0,
    company_balance NUMERIC(20, 2),
    balance_before NUMERIC(20, 2),
    source_fee NUMERIC(20, 2) NOT NULL DEFAULT 0,
    expected_fee NUMERIC(20, 2) NOT NULL DEFAULT 0,
    transfer_scope TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    is_reversal BOOLEAN NOT NULL DEFAULT FALSE,
    original_transaction_scenario TEXT,
    original_is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    reversal_category TEXT,
    reversal_in_cluster_fee NUMERIC(20, 2) NOT NULL DEFAULT 0,
    is_reversed BOOLEAN NOT NULL DEFAULT FALSE,
    reversal_count INTEGER NOT NULL DEFAULT 0,
    reversal_transaction_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    first_reversal_at_local TIMESTAMP,
    latest_reversal_at_local TIMESTAMP,
    source_files TEXT,
    updated_load_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, report_date, transaction_id),
    CONSTRAINT linkaja_transactions_transfer_scope_check
        CHECK (transfer_scope IN ('IN_CLUSTER', 'OUT_CLUSTER', 'NOT_APPLICABLE'))
);

CREATE INDEX linkaja_transactions_cluster_transaction_idx
ON linkaja_transactions (cluster_id, transaction_id);

CREATE INDEX linkaja_transactions_cluster_original_idx
ON linkaja_transactions (cluster_id, original_transaction_id)
WHERE original_transaction_id IS NOT NULL;

CREATE INDEX linkaja_transactions_cluster_report_date_idx
ON linkaja_transactions (cluster_id, report_date);
