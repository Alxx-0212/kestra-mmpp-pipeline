/* Monthly data model. The daily workflow does not execute this file. */

CREATE TABLE IF NOT EXISTS finpay_monthly_transactions (
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    calculation_cutoff TIMESTAMP NOT NULL,
    publication_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    event_role TEXT NOT NULL CHECK (event_role IN ('ORIGINAL', 'REVERSAL')),
    linked_original_event_id TEXT,
    event_timestamp TIMESTAMP NOT NULL,
    settlement_date DATE,
    settlement_category TEXT NOT NULL,
    signed_amount NUMERIC(18, 2) NOT NULL,
    gross_amount NUMERIC(18, 2) NOT NULL,
    reversed_amount NUMERIC(18, 2) NOT NULL,
    net_payable NUMERIC(18, 2) NOT NULL,
    active_at_cutoff BOOLEAN NOT NULL,
    reversal_resolution_status TEXT NOT NULL,
    reversal_count INTEGER NOT NULL CHECK (reversal_count >= 0),
    source_fingerprint TEXT NOT NULL,
    classification_version TEXT NOT NULL,
    eligible BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (cluster_id, report_month, event_id)
);

ALTER TABLE finpay_monthly_transactions
    ADD COLUMN IF NOT EXISTS eligible BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE finpay_monthly_transactions
    ADD COLUMN IF NOT EXISTS linked_original_event_id TEXT;
ALTER TABLE finpay_monthly_transactions
    ADD COLUMN IF NOT EXISTS settlement_date DATE;

CREATE INDEX IF NOT EXISTS finpay_monthly_transactions_publication_idx
    ON finpay_monthly_transactions (publication_id);

CREATE TABLE IF NOT EXISTS finpay_monthly_publications (
    publication_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    calculation_cutoff TIMESTAMP NOT NULL,
    source_fingerprint TEXT NOT NULL,
    source_day_count INTEGER NOT NULL CHECK (source_day_count >= 0),
    missing_source_day_count INTEGER NOT NULL CHECK (missing_source_day_count >= 0),
    unresolved_reversal_count INTEGER NOT NULL CHECK (unresolved_reversal_count >= 0),
    multiple_reversal_count INTEGER NOT NULL DEFAULT 0 CHECK (multiple_reversal_count >= 0),
    duplicate_source_row_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_source_row_count >= 0),
    blocking_exception_count INTEGER NOT NULL CHECK (blocking_exception_count >= 0),
    expected_period_complete BOOLEAN NOT NULL DEFAULT FALSE,
    database_status TEXT NOT NULL DEFAULT 'NOT_PUBLISHED',
    google_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
    google_error TEXT,
    preview_fingerprint TEXT,
    calculation_fingerprint TEXT,
    status TEXT NOT NULL,
    approved_by TEXT,
    previewed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ
);

ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS multiple_reversal_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS duplicate_source_row_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS expected_period_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS database_status TEXT NOT NULL DEFAULT 'NOT_PUBLISHED';
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS google_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED';
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS google_error TEXT;
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS preview_fingerprint TEXT;
ALTER TABLE finpay_monthly_publications
    ADD COLUMN IF NOT EXISTS calculation_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS finpay_monthly_publications_cluster_month_idx
    ON finpay_monthly_publications (cluster_id, report_month, published_at DESC);

CREATE TABLE IF NOT EXISTS finpay_monthly_source_expectations (
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    expected_source_date DATE NOT NULL,
    expected_file_key TEXT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, report_month, expected_source_date)
);

CREATE TABLE IF NOT EXISTS finpay_monthly_previews (
    preview_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    calculation_cutoff TIMESTAMP NOT NULL,
    source_fingerprint TEXT NOT NULL,
    calculation_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_period_complete BOOLEAN NOT NULL,
    missing_source_day_count INTEGER NOT NULL DEFAULT 0,
    unresolved_reversal_count INTEGER NOT NULL DEFAULT 0,
    multiple_reversal_count INTEGER NOT NULL DEFAULT 0,
    duplicate_source_row_count INTEGER NOT NULL DEFAULT 0,
    blocking_exception_count INTEGER NOT NULL DEFAULT 0,
    previewed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS finpay_monthly_previews_cluster_month_idx
    ON finpay_monthly_previews (cluster_id, report_month, previewed_at DESC);

CREATE OR REPLACE VIEW finpay_current_ledger_events_v AS
SELECT events.*
FROM finpay_ledger_events AS events
JOIN finpay_source_loads AS loads
  ON loads.load_id = events.load_id
WHERE loads.is_current;

CREATE OR REPLACE VIEW finpay_transaction_events_current_v AS
WITH grouped AS (
    SELECT
        cluster_id,
        transaction_id,
        CASE
            WHEN UPPER(BTRIM(COALESCE(raw_transaction_label, ''))) = 'REVERSAL'
                THEN 'REVERSAL'
            ELSE 'ORIGINAL'
        END AS event_role,
        MIN(transaction_date) AS event_timestamp,
        MAX(report_date) AS report_date,
        MAX(base_id) AS base_id,
        MAX(transaction_id_type) AS transaction_id_type,
        ARRAY_AGG(DISTINCT NULLIF(BTRIM(raw_transaction_label), '')
                  ORDER BY NULLIF(BTRIM(raw_transaction_label), '')) AS raw_labels,
        ARRAY_AGG(DISTINCT NULLIF(BTRIM(remarks), '')
                  ORDER BY NULLIF(BTRIM(remarks), '')) AS remarks,
        COUNT(*)::INTEGER AS source_row_count,
        COALESCE(SUM(kredit), 0)::NUMERIC(18, 2) AS kredit_total,
        COALESCE(SUM(debet), 0)::NUMERIC(18, 2) AS debet_total,
        (COALESCE(SUM(kredit), 0) - COALESCE(SUM(debet), 0))::NUMERIC(18, 2)
            AS signed_amount,
        ARRAY_AGG(DISTINCT load_id ORDER BY load_id) AS load_ids
    FROM finpay_current_ledger_events_v
    GROUP BY
        cluster_id,
        transaction_id,
        CASE
            WHEN UPPER(BTRIM(COALESCE(raw_transaction_label, ''))) = 'REVERSAL'
                THEN 'REVERSAL'
            ELSE 'ORIGINAL'
        END,
        transaction_date
)
SELECT
    md5(
        cluster_id || ':' || transaction_id || ':' || event_role || ':' ||
        event_timestamp::TEXT
    ) AS event_id,
    grouped.*
FROM grouped;

CREATE OR REPLACE VIEW finpay_reversal_status_current_v AS
WITH base_events AS (
    SELECT * FROM finpay_transaction_events_current_v
), reversal_matches AS (
    SELECT
        reversal.event_id,
        COUNT(original.event_id)::INTEGER AS original_candidate_count,
        COUNT(original.event_id) FILTER (
            WHERE original.event_timestamp < reversal.event_timestamp
        )::INTEGER AS prior_original_count,
        MAX(original.signed_amount) FILTER (
            WHERE original.event_timestamp < reversal.event_timestamp
        ) AS prior_signed_amount
    FROM base_events AS reversal
    LEFT JOIN base_events AS original
      ON original.event_role = 'ORIGINAL'
     AND original.cluster_id = reversal.cluster_id
     AND original.transaction_id = reversal.transaction_id
    WHERE reversal.event_role = 'REVERSAL'
    GROUP BY reversal.event_id
), reversal_status AS (
    SELECT
        matches.*,
        CASE
            WHEN original_candidate_count = 0 THEN 'UNRESOLVED_MISSING_ORIGINAL'
            WHEN prior_original_count = 0 THEN 'INVALID_CHRONOLOGY'
            WHEN prior_original_count > 1 THEN 'AMBIGUOUS_MULTIPLE_ORIGINALS'
            WHEN ABS(COALESCE(prior_signed_amount, 0) + reversal.signed_amount) > 0.01
                THEN 'AMOUNT_MISMATCH'
            ELSE 'COMPLETE'
        END AS reversal_resolution_status
    FROM reversal_matches AS matches
    JOIN base_events AS reversal ON reversal.event_id = matches.event_id
), original_reversals AS (
    SELECT
        original.event_id,
        COUNT(DISTINCT reversal.event_id) FILTER (
            WHERE reversal.event_timestamp > original.event_timestamp
              AND status.reversal_resolution_status = 'COMPLETE'
        )::INTEGER AS valid_reversal_count,
        COUNT(DISTINCT reversal.event_id)::INTEGER AS all_reversal_count
    FROM base_events AS original
    LEFT JOIN base_events AS reversal
      ON reversal.event_role = 'REVERSAL'
     AND reversal.cluster_id = original.cluster_id
     AND reversal.transaction_id = original.transaction_id
    LEFT JOIN reversal_status AS status ON status.event_id = reversal.event_id
    WHERE original.event_role = 'ORIGINAL'
    GROUP BY original.event_id
), enriched AS (
    SELECT
        event.*,
        CASE WHEN event.event_role = 'REVERSAL'
            THEN status.original_candidate_count END AS original_candidate_count,
        CASE WHEN event.event_role = 'REVERSAL'
            THEN status.prior_original_count END AS prior_original_count,
        CASE WHEN event.event_role = 'REVERSAL'
            THEN status.prior_signed_amount END AS prior_signed_amount,
        COALESCE(original_info.valid_reversal_count, 0)::INTEGER AS valid_reversal_count,
        COALESCE(original_info.all_reversal_count, 0)::INTEGER AS all_reversal_count,
        status.reversal_resolution_status AS reversal_status
    FROM base_events AS event
    LEFT JOIN reversal_status AS status ON status.event_id = event.event_id
    LEFT JOIN original_reversals AS original_info ON original_info.event_id = event.event_id
)
SELECT
    enriched.*,
    CASE
        WHEN event_role = 'ORIGINAL' AND valid_reversal_count > 1 THEN 'MULTIPLE_REVERSALS'
        WHEN event_role = 'ORIGINAL' THEN 'NOT_REVERSAL'
        ELSE reversal_status
    END AS reversal_resolution_status,
    event_role = 'ORIGINAL' AND valid_reversal_count > 0 AS is_reversed,
    event_role = 'ORIGINAL' AND valid_reversal_count = 0 AS active_without_cutoff,
    CASE
        WHEN event_role = 'ORIGINAL' AND valid_reversal_count > 1 THEN TRUE
        WHEN event_role = 'REVERSAL' AND reversal_status <> 'COMPLETE' THEN TRUE
        ELSE FALSE
    END AS is_unusual
FROM enriched;
