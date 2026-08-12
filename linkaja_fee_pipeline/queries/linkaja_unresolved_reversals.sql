/*
Current unresolved reversal exceptions from the live daily view.

An unresolved reversal has an Original Transaction ID but no single completed
original transaction scenario currently available in raw history.
*/

WITH parameters AS (
    SELECT
        '411311'::TEXT AS cluster_id,
        DATE '2026-07-01' AS start_date,
        DATE '2026-08-01' AS end_date_exclusive
)
SELECT
    transaction.report_date AS reversal_report_date,
    transaction.finalized_at_local,
    transaction.transaction_id AS reversal_transaction_id,
    transaction.original_transaction_id,
    transaction.transaction_scenario AS reversal_scenario,
    transaction.company_debit,
    transaction.company_credit,
    transaction.company_balance,
    transaction.balance_before,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.unusual_reason_codes
FROM linkaja_transactions_current_v AS transaction
CROSS JOIN parameters
WHERE transaction.cluster_id = parameters.cluster_id
  AND transaction.report_date >= parameters.start_date
  AND transaction.report_date < parameters.end_date_exclusive
  AND transaction.reversal_resolution_status = 'UNRESOLVED'
ORDER BY transaction.finalized_at_local, transaction.transaction_id;
