/* Explicit, additive cell reporting migration. Never run from upload code.
   Apply only in the separately approved schema with an approved migration role.
   Legacy daily/monthly relations and their consumers are not modified. */

CREATE TABLE IF NOT EXISTS finpay_cell_configs_v1 (
    config_id TEXT PRIMARY KEY CHECK (config_id ~ '^[a-f0-9]{64}$'),
    configuration JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS finpay_cell_sources_v1 (
    generation_id TEXT PRIMARY KEY CHECK (generation_id ~ '^[a-f0-9]{64}$'),
    account TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    cell_code TEXT NOT NULL,
    parentcode TEXT NOT NULL,
    report_date DATE NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
    raw_fingerprint TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (account = cluster_id || '_B'),
    CHECK (parentcode = cluster_id || cell_code),
    UNIQUE (generation_id, account, cluster_id, cell_code, report_date)
);

CREATE TABLE IF NOT EXISTS finpay_cell_ledger_v1 (
    generation_id TEXT NOT NULL REFERENCES finpay_cell_sources_v1(generation_id),
    source_position INTEGER NOT NULL CHECK (source_position > 0),
    transaction_id TEXT NOT NULL CHECK (transaction_id <> ''),
    row_fingerprint TEXT NOT NULL,
    raw_row JSONB NOT NULL CHECK (NOT (raw_row ? 'No')),
    PRIMARY KEY (generation_id, source_position)
);

CREATE INDEX IF NOT EXISTS finpay_cell_sources_v1_scope_idx
    ON finpay_cell_sources_v1 (account, cluster_id, cell_code, report_date);
CREATE INDEX IF NOT EXISTS finpay_cell_ledger_v1_identifier_idx
    ON finpay_cell_ledger_v1 (generation_id, transaction_id);

CREATE TABLE IF NOT EXISTS finpay_cell_calculations_v1 (
    calculation_id TEXT PRIMARY KEY CHECK (calculation_id ~ '^[a-f0-9]{64}$'),
    generation_id TEXT NOT NULL REFERENCES finpay_cell_sources_v1(generation_id),
    config_id TEXT NOT NULL REFERENCES finpay_cell_configs_v1(config_id),
    rules_version TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (calculation_id, generation_id)
);

CREATE TABLE IF NOT EXISTS finpay_cell_current_v1 (
    account TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    cell_code TEXT NOT NULL,
    report_date DATE NOT NULL,
    generation_id TEXT NOT NULL,
    calculation_id TEXT NOT NULL,
    selected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account, cluster_id, cell_code, report_date),
    FOREIGN KEY (generation_id, account, cluster_id, cell_code, report_date)
        REFERENCES finpay_cell_sources_v1
        (generation_id, account, cluster_id, cell_code, report_date),
    FOREIGN KEY (calculation_id, generation_id)
        REFERENCES finpay_cell_calculations_v1 (calculation_id, generation_id)
);

CREATE TABLE IF NOT EXISTS finpay_cell_publications_v1 (
    cluster_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    range_start DATE NOT NULL,
    range_end DATE NOT NULL,
    snapshot_fingerprint TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('DB_ONLY', 'PENDING', 'IN_FLIGHT', 'UNKNOWN', 'COMPLETE', 'SUPERSEDED')),
    confirmed_chunks JSONB NOT NULL DEFAULT '[]'::JSONB,
    error_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMPTZ,
    PRIMARY KEY (cluster_id, target_id, epoch),
    CHECK (range_start <= range_end)
);

CREATE INDEX IF NOT EXISTS finpay_cell_publications_v1_recovery_idx
    ON finpay_cell_publications_v1 (target_id, status);

/* Covers ambiguous protection/setup writes before a publication snapshot exists. */
CREATE TABLE IF NOT EXISTS finpay_cell_target_operations_v1 (
    target_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IDLE', 'IN_FLIGHT', 'UNKNOWN')),
    error_class TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS finpay_cell_dirty_dates_v1 (
    cluster_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    calculation_fingerprint TEXT NOT NULL,
    reset_required BOOLEAN NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, report_date)
);

CREATE TABLE IF NOT EXISTS finpay_cell_publication_dates_v1 (
    cluster_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    epoch BIGINT NOT NULL,
    report_date DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DB_ONLY', 'PENDING', 'COMPLETE', 'SUPERSEDED')),
    PRIMARY KEY (cluster_id, target_id, epoch, report_date),
    FOREIGN KEY (cluster_id, target_id, epoch)
        REFERENCES finpay_cell_publications_v1 (cluster_id, target_id, epoch)
);

CREATE TABLE IF NOT EXISTS finpay_cell_manual_resets_v1 (
    cluster_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    epoch BIGINT NOT NULL,
    cell_code TEXT NOT NULL,
    report_date DATE NOT NULL,
    previous_value JSONB NOT NULL,
    replacement_value JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, target_id, epoch, cell_code, report_date),
    FOREIGN KEY (cluster_id, target_id, epoch)
        REFERENCES finpay_cell_publications_v1 (cluster_id, target_id, epoch)
);

CREATE OR REPLACE VIEW finpay_cell_current_results_v1 AS
SELECT selected.account, selected.cluster_id, selected.cell_code, selected.report_date,
       selected.generation_id, selected.calculation_id, calculation.config_id,
       calculation.result
FROM finpay_cell_current_v1 AS selected
JOIN finpay_cell_calculations_v1 AS calculation
  ON selected.calculation_id = calculation.calculation_id;

/* Rollback: stop the new flows, retain these evidence relations, and select a
   previously approved cell publication/configuration. Do not truncate evidence
   or route _B accounts into the cluster/date legacy writer. */
