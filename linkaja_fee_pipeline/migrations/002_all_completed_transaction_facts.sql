DO $$
DECLARE
    conflict RECORD;
BEGIN
    SELECT
        cluster_id,
        transaction_id,
        COUNT(DISTINCT finalized_date) AS finalized_dates,
        COUNT(DISTINCT transaction_scenario) AS scenarios,
        COUNT(DISTINCT LOWER(BTRIM(COALESCE(transaction_status, '')))) AS statuses,
        COUNT(DISTINCT COALESCE(transaction_type, '<NULL>')) AS transaction_types,
        COUNT(DISTINCT COALESCE(NULLIF(BTRIM(original_transaction_id), ''), '<NULL>'))
            AS original_ids,
        COUNT(DISTINCT fee) FILTER (WHERE fee IS NOT NULL) AS fee_values,
        COUNT(DISTINCT account) FILTER (
            WHERE organization = top_organization
              AND POSITION('organization mfs purchase account' IN LOWER(account)) > 0
        ) AS company_accounts
    INTO conflict
    FROM linkaja_raw_transactions
    GROUP BY cluster_id, transaction_id
    HAVING COUNT(DISTINCT finalized_date) > 1
        OR COUNT(DISTINCT transaction_scenario) > 1
        OR COUNT(DISTINCT LOWER(BTRIM(COALESCE(transaction_status, '')))) > 1
        OR COUNT(DISTINCT COALESCE(transaction_type, '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(NULLIF(BTRIM(original_transaction_id), ''), '<NULL>')) > 1
        OR COUNT(DISTINCT fee) FILTER (WHERE fee IS NOT NULL) > 1
        OR COUNT(DISTINCT account) FILTER (
            WHERE organization = top_organization
              AND POSITION('organization mfs purchase account' IN LOWER(account)) > 0
        ) > 1
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'Cannot migrate inconsistent LinkAja transaction %.% '
            '(dates %, scenarios %, statuses %, types %, originals %, fee values %, company accounts %)',
            conflict.cluster_id,
            conflict.transaction_id,
            conflict.finalized_dates,
            conflict.scenarios,
            conflict.statuses,
            conflict.transaction_types,
            conflict.original_ids,
            conflict.fee_values,
            conflict.company_accounts;
    END IF;
END
$$;

DROP TABLE linkaja_transactions;

CREATE TABLE linkaja_transactions (
    cluster_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    finalized_at_local TIMESTAMP NOT NULL,
    original_transaction_id TEXT,
    partner_reference_number TEXT,
    invoice_id TEXT,
    transaction_type TEXT,
    transaction_scenario TEXT NOT NULL,
    transaction_status TEXT NOT NULL,
    counter_party TEXT,
    source_ledger_row_count INTEGER NOT NULL,
    ledger_debit_total NUMERIC(20, 2) NOT NULL DEFAULT 0,
    ledger_credit_total NUMERIC(20, 2) NOT NULL DEFAULT 0,
    source_fee NUMERIC(20, 2),
    source_fee_value_count INTEGER NOT NULL DEFAULT 0,
    company_organization TEXT,
    company_account TEXT,
    has_company_purchase_account BOOLEAN
        GENERATED ALWAYS AS (company_account IS NOT NULL) STORED,
    company_debit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    company_credit NUMERIC(20, 2) NOT NULL DEFAULT 0,
    signed_amount NUMERIC(20, 2)
        GENERATED ALWAYS AS (company_credit - company_debit) STORED,
    company_balance NUMERIC(20, 2),
    balance_before NUMERIC(20, 2)
        GENERATED ALWAYS AS (
            CASE
                WHEN company_balance IS NULL THEN NULL
                ELSE company_balance + company_debit - company_credit
            END
        ) STORED,
    transfer_scope TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    is_reversal BOOLEAN
        GENERATED ALWAYS AS (original_transaction_id IS NOT NULL) STORED,
    original_transaction_scenario TEXT,
    original_is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    reversal_category TEXT
        GENERATED ALWAYS AS (
            CASE
                WHEN original_transaction_id IS NULL THEN NULL
                WHEN original_is_resolved
                    THEN 'REVERSAL OF ' || original_transaction_scenario
                ELSE 'UNRESOLVED REVERSAL'
            END
        ) STORED,
    reversal_count INTEGER NOT NULL DEFAULT 0,
    is_reversed BOOLEAN
        GENERATED ALWAYS AS (reversal_count > 0) STORED,
    reversal_transaction_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    first_reversal_at_local TIMESTAMP,
    latest_reversal_at_local TIMESTAMP,
    source_files TEXT,
    updated_load_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, transaction_id),
    CONSTRAINT linkaja_transactions_completed_status_check
        CHECK (LOWER(BTRIM(transaction_status)) = 'completed'),
    CONSTRAINT linkaja_transactions_source_row_count_check
        CHECK (source_ledger_row_count > 0),
    CONSTRAINT linkaja_transactions_source_fee_check
        CHECK (
            source_fee_value_count IN (0, 1)
            AND (
                (source_fee_value_count = 0 AND source_fee IS NULL)
                OR (source_fee_value_count = 1 AND source_fee IS NOT NULL)
            )
        ),
    CONSTRAINT linkaja_transactions_company_account_check
        CHECK (
            (
                company_account IS NOT NULL
                AND company_organization IS NOT NULL
            )
            OR (
                company_account IS NULL
                AND company_organization IS NULL
                AND company_debit = 0
                AND company_credit = 0
                AND company_balance IS NULL
            )
        ),
    CONSTRAINT linkaja_transactions_transfer_scope_check
        CHECK (transfer_scope IN ('IN_CLUSTER', 'OUT_CLUSTER', 'NOT_APPLICABLE')),
    CONSTRAINT linkaja_transactions_resolved_original_check
        CHECK (
            NOT original_is_resolved
            OR (
                original_transaction_id IS NOT NULL
                AND original_transaction_scenario IS NOT NULL
            )
        ),
    CONSTRAINT linkaja_transactions_reversal_count_check
        CHECK (
            reversal_count >= 0
            AND reversal_count = CARDINALITY(reversal_transaction_ids)
        ),
    CONSTRAINT linkaja_transactions_reversal_timestamp_check
        CHECK (
            (
                reversal_count = 0
                AND first_reversal_at_local IS NULL
                AND latest_reversal_at_local IS NULL
            )
            OR (
                reversal_count > 0
                AND first_reversal_at_local IS NOT NULL
                AND latest_reversal_at_local IS NOT NULL
                AND first_reversal_at_local <= latest_reversal_at_local
            )
        )
);

CREATE INDEX linkaja_transactions_cluster_report_date_idx
ON linkaja_transactions (cluster_id, report_date);

CREATE INDEX linkaja_transactions_cluster_scenario_report_date_idx
ON linkaja_transactions (cluster_id, transaction_scenario, report_date);

CREATE INDEX linkaja_transactions_cluster_original_idx
ON linkaja_transactions (cluster_id, original_transaction_id)
WHERE original_transaction_id IS NOT NULL;

WITH completed_raw AS (
    SELECT
        raw.*,
        (
            raw.organization = raw.top_organization
            AND POSITION('organization mfs purchase account' IN LOWER(raw.account)) > 0
        ) AS is_company_purchase_row
    FROM linkaja_raw_transactions AS raw
    WHERE LOWER(BTRIM(COALESCE(raw.transaction_status, ''))) = 'completed'
), grouped AS (
    SELECT
        raw.cluster_id,
        raw.transaction_id,
        MIN(raw.finalized_date) AS report_date,
        MIN(raw.finalized_date + raw.finalized_time) AS finalized_at_local,
        MAX(NULLIF(BTRIM(raw.original_transaction_id), '')) AS original_transaction_id,
        MAX(NULLIF(BTRIM(raw.partner_reference_number), '')) AS partner_reference_number,
        MAX(NULLIF(BTRIM(raw.invoice_id), '')) AS invoice_id,
        MAX(raw.transaction_type) AS transaction_type,
        MAX(raw.transaction_scenario) AS transaction_scenario,
        MAX(raw.transaction_status) AS transaction_status,
        COALESCE(
            MAX(raw.counter_party) FILTER (WHERE raw.is_company_purchase_row),
            MAX(raw.counter_party)
        ) AS counter_party,
        COUNT(*)::INTEGER AS source_ledger_row_count,
        COALESCE(SUM(raw.debit), 0) AS ledger_debit_total,
        COALESCE(SUM(raw.credit), 0) AS ledger_credit_total,
        MAX(raw.fee) FILTER (WHERE raw.fee IS NOT NULL) AS source_fee,
        (
            COUNT(DISTINCT raw.fee) FILTER (WHERE raw.fee IS NOT NULL)
        )::INTEGER AS source_fee_value_count,
        MAX(raw.organization) FILTER (WHERE raw.is_company_purchase_row)
            AS company_organization,
        MAX(raw.account) FILTER (WHERE raw.is_company_purchase_row)
            AS company_account,
        COALESCE(SUM(raw.debit) FILTER (WHERE raw.is_company_purchase_row), 0)
            AS company_debit,
        COALESCE(SUM(raw.credit) FILTER (WHERE raw.is_company_purchase_row), 0)
            AS company_credit,
        MAX(raw.balance) FILTER (WHERE raw.is_company_purchase_row)
            AS company_balance,
        STRING_AGG(DISTINCT raw.source_file, ', ' ORDER BY raw.source_file)
            AS source_files,
        (ARRAY_AGG(
            raw.load_id
            ORDER BY raw.ingested_at DESC, raw.source_row_number DESC
        ))[1] AS updated_load_id
    FROM completed_raw AS raw
    GROUP BY raw.cluster_id, raw.transaction_id
), original_info AS (
    SELECT
        original.cluster_id,
        original.transaction_id,
        COUNT(DISTINCT original.transaction_scenario) AS scenario_count,
        MAX(original.transaction_scenario) AS transaction_scenario
    FROM completed_raw AS original
    JOIN (
        SELECT DISTINCT cluster_id, original_transaction_id
        FROM grouped
        WHERE original_transaction_id IS NOT NULL
    ) AS referenced
      ON referenced.cluster_id = original.cluster_id
     AND referenced.original_transaction_id = original.transaction_id
    GROUP BY original.cluster_id, original.transaction_id
), reversal_info AS (
    SELECT
        reversal.cluster_id,
        reversal.original_transaction_id AS transaction_id,
        COUNT(DISTINCT reversal.transaction_id)::INTEGER AS reversal_count,
        ARRAY_AGG(DISTINCT reversal.transaction_id ORDER BY reversal.transaction_id)
            AS reversal_transaction_ids,
        MIN(reversal.finalized_date + reversal.finalized_time)
            AS first_reversal_at_local,
        MAX(reversal.finalized_date + reversal.finalized_time)
            AS latest_reversal_at_local
    FROM completed_raw AS reversal
    WHERE reversal.original_transaction_id IS NOT NULL
      AND BTRIM(reversal.original_transaction_id) <> ''
    GROUP BY reversal.cluster_id, reversal.original_transaction_id
)
INSERT INTO linkaja_transactions (
    cluster_id,
    transaction_id,
    report_date,
    finalized_at_local,
    original_transaction_id,
    partner_reference_number,
    invoice_id,
    transaction_type,
    transaction_scenario,
    transaction_status,
    counter_party,
    source_ledger_row_count,
    ledger_debit_total,
    ledger_credit_total,
    source_fee,
    source_fee_value_count,
    company_organization,
    company_account,
    company_debit,
    company_credit,
    company_balance,
    transfer_scope,
    original_transaction_scenario,
    original_is_resolved,
    reversal_count,
    reversal_transaction_ids,
    first_reversal_at_local,
    latest_reversal_at_local,
    source_files,
    updated_load_id
)
SELECT
    grouped.cluster_id,
    grouped.transaction_id,
    grouped.report_date,
    grouped.finalized_at_local,
    grouped.original_transaction_id,
    grouped.partner_reference_number,
    grouped.invoice_id,
    grouped.transaction_type,
    grouped.transaction_scenario,
    grouped.transaction_status,
    grouped.counter_party,
    grouped.source_ledger_row_count,
    grouped.ledger_debit_total,
    grouped.ledger_credit_total,
    grouped.source_fee,
    grouped.source_fee_value_count,
    grouped.company_organization,
    grouped.company_account,
    grouped.company_debit,
    grouped.company_credit,
    grouped.company_balance,
    CASE
        WHEN grouped.transaction_scenario = 'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN grouped.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario = 'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        ELSE 'NOT_APPLICABLE'
    END,
    CASE
        WHEN original_info.scenario_count = 1
            THEN original_info.transaction_scenario
        ELSE NULL
    END,
    COALESCE(original_info.scenario_count = 1, FALSE),
    COALESCE(reversal_info.reversal_count, 0),
    COALESCE(reversal_info.reversal_transaction_ids, ARRAY[]::TEXT[]),
    reversal_info.first_reversal_at_local,
    reversal_info.latest_reversal_at_local,
    grouped.source_files,
    grouped.updated_load_id
FROM grouped
LEFT JOIN original_info
  ON original_info.cluster_id = grouped.cluster_id
 AND original_info.transaction_id = grouped.original_transaction_id
LEFT JOIN reversal_info
  ON reversal_info.cluster_id = grouped.cluster_id
 AND reversal_info.transaction_id = grouped.transaction_id;
