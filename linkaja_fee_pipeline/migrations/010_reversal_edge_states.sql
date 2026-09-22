CREATE VIEW linkaja_reversal_edges_current_v AS
WITH reversal_facts AS (
    SELECT
        reversal.cluster_id,
        reversal.transaction_id AS reversal_transaction_id,
        reversal.original_transaction_id,
        reversal.report_date AS reversal_report_date,
        reversal.finalized_at_local AS reversal_finalized_at_local,
        reversal.transaction_scenario AS reversal_scenario,
        reversal.company_debit AS reversal_company_debit,
        reversal.company_credit AS reversal_company_credit,
        reversal.source_files AS reversal_source_files,
        reversal.updated_load_id AS reversal_updated_load_id
    FROM linkaja_transaction_base_v AS reversal
    WHERE reversal.original_transaction_id IS NOT NULL
      AND BTRIM(reversal.original_transaction_id) <> ''
), same_cluster_originals AS (
    SELECT
        reversal.cluster_id,
        reversal.reversal_transaction_id,
        COUNT(original.transaction_id)::INTEGER AS original_fact_count,
        COUNT(DISTINCT original.transaction_scenario)::INTEGER AS scenario_count,
        COUNT(original.transaction_id) FILTER (
            WHERE original.finalized_at_local < reversal.reversal_finalized_at_local
        )::INTEGER AS prior_original_count,
        MIN(original.finalized_at_local) AS first_original_at_local,
        MAX(original.finalized_at_local) AS latest_original_at_local,
        MAX(original.transaction_scenario) AS original_scenario,
        MAX(original.report_date) AS original_report_date,
        MAX(original.company_credit - original.company_debit)
            FILTER (WHERE original.finalized_at_local < reversal.reversal_finalized_at_local)
            AS original_signed_amount
    FROM reversal_facts AS reversal
    LEFT JOIN linkaja_transaction_base_v AS original
      ON original.cluster_id = reversal.cluster_id
     AND original.transaction_id = reversal.original_transaction_id
     AND original.original_transaction_id IS NULL
    GROUP BY
        reversal.cluster_id,
        reversal.reversal_transaction_id,
        reversal.reversal_finalized_at_local
), cross_cluster_originals AS (
    SELECT DISTINCT
        reversal.cluster_id,
        reversal.reversal_transaction_id
    FROM reversal_facts AS reversal
    JOIN linkaja_transaction_base_v AS original
      ON original.transaction_id = reversal.original_transaction_id
     AND original.cluster_id <> reversal.cluster_id
     AND original.original_transaction_id IS NULL
), status_rows AS (
    SELECT
        reversal.*,
        COALESCE(originals.original_fact_count, 0) AS original_fact_count,
        COALESCE(originals.scenario_count, 0) AS original_scenario_count,
        COALESCE(originals.prior_original_count, 0) AS prior_original_count,
        originals.first_original_at_local,
        originals.latest_original_at_local,
        originals.original_scenario,
        originals.original_report_date,
        originals.original_signed_amount,
        (cross_cluster.reversal_transaction_id IS NOT NULL)
            AS has_cross_cluster_original
    FROM reversal_facts AS reversal
    LEFT JOIN same_cluster_originals AS originals
      ON originals.cluster_id = reversal.cluster_id
     AND originals.reversal_transaction_id = reversal.reversal_transaction_id
    LEFT JOIN cross_cluster_originals AS cross_cluster
      ON cross_cluster.cluster_id = reversal.cluster_id
     AND cross_cluster.reversal_transaction_id = reversal.reversal_transaction_id
)
SELECT
    status_rows.*,
    CASE
        WHEN reversal_transaction_id = original_transaction_id
            THEN 'SELF_REFERENCE'
        WHEN original_fact_count = 0 AND has_cross_cluster_original
            THEN 'CROSS_CLUSTER_REFERENCE'
        WHEN original_fact_count = 0
            THEN 'UNRESOLVED_MISSING_ORIGINAL'
        WHEN prior_original_count = 0
            THEN 'INVALID_CHRONOLOGY'
        WHEN original_fact_count > 1 OR original_scenario_count <> 1
            THEN 'AMBIGUOUS_ORIGINAL'
        ELSE 'COMPLETE'
    END AS resolution_status,
    CASE
        WHEN original_signed_amount IS NULL THEN NULL
        ELSE ABS(
            original_signed_amount
            + reversal_company_credit
            - reversal_company_debit
        ) <= 0.01
    END AS amount_matches
FROM status_rows;

CREATE INDEX linkaja_raw_cluster_finalized_transaction_idx
ON linkaja_raw_transactions (cluster_id, finalized_date, finalized_time, transaction_id);

CREATE INDEX linkaja_raw_original_finalized_idx
ON linkaja_raw_transactions (
    cluster_id, original_transaction_id, finalized_date, finalized_time
)
WHERE original_transaction_id IS NOT NULL;
