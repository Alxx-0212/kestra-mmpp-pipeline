/*
Transaction-level live LinkAja view.

Use this for daily investigation. The view contains one completed transaction
fact per cluster, transaction ID, and posting date, enriched with current
reversal status. It is not a monthly snapshot.
*/

WITH parameters AS (
    SELECT
        '411311'::TEXT AS cluster_id,
        DATE '2026-07-01' AS start_date,
        DATE '2026-08-01' AS end_date_exclusive
)
SELECT
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    transaction.original_transaction_id,
    transaction.transaction_scenario,
    transaction.source_ledger_row_count,
    transaction.ledger_debit_total,
    transaction.ledger_credit_total,
    transaction.signed_amount,
    transaction.source_fee,
    transaction.company_organization,
    transaction.company_account,
    transaction.company_debit,
    transaction.company_credit,
    transaction.transfer_scope,
    transaction.is_reversal,
    transaction.original_transaction_scenario,
    transaction.reversal_resolution_status,
    transaction.is_reversed,
    transaction.reversal_transaction_ids,
    transaction.is_unusual,
    transaction.unusual_reason_codes,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
CROSS JOIN parameters
WHERE transaction.cluster_id = parameters.cluster_id
  AND transaction.report_date >= parameters.start_date
  AND transaction.report_date < parameters.end_date_exclusive
ORDER BY transaction.finalized_at_local, transaction.transaction_id;
