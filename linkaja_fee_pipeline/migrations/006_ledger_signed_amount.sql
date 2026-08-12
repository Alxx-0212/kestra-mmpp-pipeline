/*
Make signed_amount a ledger-level measure rather than a company Purchase
Account measure. The live view places it beside the ledger totals so its scope
is explicit.
*/

DROP VIEW linkaja_transactions_current_v;

ALTER TABLE linkaja_transactions
DROP COLUMN signed_amount;

ALTER TABLE linkaja_transactions
ADD COLUMN signed_amount NUMERIC(20, 2)
    GENERATED ALWAYS AS (
        ledger_debit_total - ledger_credit_total
    ) STORED;

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
    base.ledger_debit_total - base.ledger_credit_total AS signed_amount,
    base.source_fee,
    base.source_fee_value_count,
    base.company_organization,
    base.company_account,
    (base.company_account IS NOT NULL) AS has_company_purchase_account,
    base.company_debit,
    base.company_credit,
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
