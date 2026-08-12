/*
Split live daily calculation from month-scoped publication.

linkaja_raw_transactions remains the source of truth. The existing
linkaja_transactions rows are derived and are cleared once because this table
now represents explicitly published monthly snapshots rather than daily
current state.
*/

TRUNCATE TABLE linkaja_transactions;

ALTER TABLE linkaja_transactions
ADD COLUMN report_month DATE NOT NULL,
ADD COLUMN calculation_cutoff TIMESTAMP NOT NULL,
ADD COLUMN materialization_id TEXT NOT NULL,
ADD COLUMN materialized_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
ADD CONSTRAINT linkaja_transactions_report_month_check
    CHECK (
        report_month = DATE_TRUNC('month', report_date)::DATE
        AND report_month = DATE_TRUNC('month', report_month)::DATE
    ),
ADD CONSTRAINT linkaja_transactions_calculation_cutoff_check
    CHECK (finalized_at_local < calculation_cutoff);

CREATE INDEX linkaja_transactions_cluster_report_month_idx
ON linkaja_transactions (cluster_id, report_month);

CREATE INDEX linkaja_transactions_materialization_idx
ON linkaja_transactions (materialization_id);

CREATE TABLE linkaja_monthly_refreshes (
    materialization_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    report_month DATE NOT NULL,
    calculation_cutoff TIMESTAMP NOT NULL,
    transaction_rows INTEGER NOT NULL,
    unresolved_reversal_count INTEGER NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT linkaja_monthly_refreshes_report_month_check
        CHECK (report_month = DATE_TRUNC('month', report_month)::DATE),
    CONSTRAINT linkaja_monthly_refreshes_counts_check
        CHECK (
            transaction_rows >= 0
            AND unresolved_reversal_count >= 0
        )
);

CREATE INDEX linkaja_monthly_refreshes_cluster_month_idx
ON linkaja_monthly_refreshes (cluster_id, report_month, refreshed_at DESC);

CREATE VIEW linkaja_transaction_base_v AS
WITH completed_raw AS (
    SELECT
        raw.*,
        (
            raw.organization = raw.top_organization
            AND POSITION(
                'organization mfs purchase account' IN LOWER(raw.account)
            ) > 0
        ) AS is_company_purchase_row
    FROM linkaja_raw_transactions AS raw
    WHERE LOWER(BTRIM(COALESCE(raw.transaction_status, ''))) = 'completed'
)
SELECT
    raw.cluster_id,
    raw.transaction_id,
    raw.finalized_date AS report_date,
    MIN(raw.finalized_date + raw.finalized_time) AS finalized_at_local,
    MAX(NULLIF(BTRIM(raw.original_transaction_id), ''))
        AS original_transaction_id,
    MAX(NULLIF(BTRIM(raw.partner_reference_number), ''))
        AS partner_reference_number,
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
    COALESCE(
        SUM(raw.debit) FILTER (WHERE raw.is_company_purchase_row),
        0
    ) AS company_debit,
    COALESCE(
        SUM(raw.credit) FILTER (WHERE raw.is_company_purchase_row),
        0
    ) AS company_credit,
    MAX(raw.balance) FILTER (WHERE raw.is_company_purchase_row)
        AS company_balance,
    STRING_AGG(DISTINCT raw.source_file, ', ' ORDER BY raw.source_file)
        AS source_files,
    (
        ARRAY_AGG(
            raw.load_id
            ORDER BY raw.ingested_at DESC, raw.source_row_number DESC
        )
    )[1] AS updated_load_id,
    MAX(raw.ingested_at) AS updated_at
FROM completed_raw AS raw
GROUP BY raw.cluster_id, raw.transaction_id, raw.finalized_date;

CREATE VIEW linkaja_transactions_current_v AS
SELECT
    base.cluster_id,
    base.transaction_id,
    base.report_date,
    base.finalized_at_local,
    base.original_transaction_id,
    base.partner_reference_number,
    base.invoice_id,
    base.transaction_type,
    base.transaction_scenario,
    base.transaction_status,
    base.counter_party,
    base.source_ledger_row_count,
    base.ledger_debit_total,
    base.ledger_credit_total,
    base.source_fee,
    base.source_fee_value_count,
    base.company_organization,
    base.company_account,
    (base.company_account IS NOT NULL) AS has_company_purchase_account,
    base.company_debit,
    base.company_credit,
    base.company_credit - base.company_debit AS signed_amount,
    base.company_balance,
    CASE
        WHEN base.company_balance IS NULL THEN NULL
        ELSE base.company_balance + base.company_debit - base.company_credit
    END AS balance_before,
    CASE
        WHEN base.transaction_scenario = 'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN base.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario =
                'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        ELSE 'NOT_APPLICABLE'
    END AS transfer_scope,
    (base.original_transaction_id IS NOT NULL) AS is_reversal,
    CASE
        WHEN original_info.scenario_count = 1
            THEN original_info.transaction_scenario
        ELSE NULL
    END AS original_transaction_scenario,
    COALESCE(original_info.scenario_count = 1, FALSE)
        AS original_is_resolved,
    CASE
        WHEN base.original_transaction_id IS NULL THEN NULL
        WHEN original_info.scenario_count = 1
            THEN 'REVERSAL OF ' || original_info.transaction_scenario
        ELSE 'UNRESOLVED REVERSAL'
    END AS reversal_category,
    COALESCE(reversal_info.reversal_count, 0) AS reversal_count,
    (COALESCE(reversal_info.reversal_count, 0) > 0) AS is_reversed,
    COALESCE(
        reversal_info.reversal_transaction_ids,
        ARRAY[]::TEXT[]
    ) AS reversal_transaction_ids,
    reversal_info.first_reversal_at_local,
    reversal_info.latest_reversal_at_local,
    base.source_files,
    base.updated_load_id,
    base.updated_at,
    CASE
        WHEN base.original_transaction_id IS NULL THEN 'NOT_REVERSAL'
        WHEN original_info.scenario_count = 1 THEN 'COMPLETE'
        ELSE 'UNRESOLVED'
    END AS reversal_resolution_status,
    (
        base.original_transaction_id IS NOT NULL
        AND COALESCE(original_info.scenario_count, 0) <> 1
    ) AS is_unusual,
    CASE
        WHEN base.original_transaction_id IS NOT NULL
         AND COALESCE(original_info.scenario_count, 0) <> 1
            THEN ARRAY['UNRESOLVED_REVERSAL']::TEXT[]
        ELSE ARRAY[]::TEXT[]
    END AS unusual_reason_codes
FROM linkaja_transaction_base_v AS base
LEFT JOIN LATERAL (
    SELECT
        COUNT(DISTINCT original.transaction_scenario) AS scenario_count,
        MAX(original.transaction_scenario) AS transaction_scenario
    FROM linkaja_raw_transactions AS original
    WHERE original.cluster_id = base.cluster_id
      AND original.transaction_id = base.original_transaction_id
      AND LOWER(BTRIM(COALESCE(original.transaction_status, ''))) =
            'completed'
) AS original_info
  ON base.original_transaction_id IS NOT NULL
LEFT JOIN LATERAL (
    SELECT
        COUNT(DISTINCT reversal.transaction_id)::INTEGER AS reversal_count,
        ARRAY_AGG(
            DISTINCT reversal.transaction_id
            ORDER BY reversal.transaction_id
        ) AS reversal_transaction_ids,
        MIN(reversal.finalized_date + reversal.finalized_time)
            AS first_reversal_at_local,
        MAX(reversal.finalized_date + reversal.finalized_time)
            AS latest_reversal_at_local
    FROM linkaja_raw_transactions AS reversal
    WHERE reversal.cluster_id = base.cluster_id
      AND reversal.original_transaction_id = base.transaction_id
      AND LOWER(BTRIM(COALESCE(reversal.transaction_status, ''))) =
            'completed'
) AS reversal_info
  ON TRUE;
