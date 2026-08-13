/*
Daily LinkAja scenario and ledger source measures.

This read-only query returns scenario-level company debit/credit, source fee,
expected out-cluster fee, in-cluster fee, and reversal exception measures for
database clients and dashboards.
*/

WITH parameters AS (
    SELECT
        '411311'::TEXT AS cluster_id,
        DATE '2026-07-01' AS start_date,
        DATE '2026-08-01' AS end_date_exclusive
),
classified AS (
    SELECT
        transaction.*,
        CASE
            WHEN NOT transaction.is_reversal
                THEN transaction.transaction_scenario
            WHEN transaction.reversal_resolution_status = 'COMPLETE'
                THEN 'Complete Reversal - '
                    || transaction.original_transaction_scenario
            ELSE 'Unusual - Unresolved Reversal'
        END AS ledger_label
    FROM linkaja_transactions_current_v AS transaction
    CROSS JOIN parameters
    WHERE transaction.cluster_id = parameters.cluster_id
      AND transaction.report_date >= parameters.start_date
      AND transaction.report_date < parameters.end_date_exclusive
)
SELECT
    report_date,
    ledger_label AS transaction_scenario,
    COALESCE(SUM(company_debit), 0) AS company_debit,
    COALESCE(SUM(company_credit), 0) AS company_credit,
    COALESCE(SUM(source_fee), 0) AS source_fee,
    COUNT(*) FILTER (
        WHERE transaction_scenario =
                'General to Purchase B2B Transfer Agent Telco'
          AND source_fee IS NULL
    ) AS source_fee_missing_count,
    COUNT(*) FILTER (
        WHERE NOT is_reversal
          AND NOT is_reversed
          AND transaction_scenario = 'Digipos B2B Transfer'
          AND company_credit > 0
    ) * 200 AS expected_fee,
    COUNT(*) FILTER (
        WHERE NOT is_reversal
          AND NOT is_reversed
          AND transaction_scenario = 'Digipos B2B Transfer In Cluster'
    ) * 20 AS in_cluster_fee,
    COUNT(*) FILTER (
        WHERE reversal_resolution_status = 'COMPLETE'
          AND original_transaction_scenario =
              'Digipos B2B Transfer In Cluster'
    ) AS reversal_in_cluster_count,
    COUNT(*) FILTER (
        WHERE reversal_resolution_status = 'COMPLETE'
          AND original_transaction_scenario =
              'Digipos B2B Transfer In Cluster'
    ) * 20 AS reversal_in_cluster_fee,
    COUNT(*) FILTER (
        WHERE reversal_resolution_status = 'UNRESOLVED'
    ) AS unresolved_reversal_count
FROM classified
GROUP BY report_date, ledger_label
ORDER BY report_date, ledger_label;
