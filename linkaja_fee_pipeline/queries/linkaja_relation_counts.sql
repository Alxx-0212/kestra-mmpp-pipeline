/* Quick LinkAja raw, live-view, and published-snapshot relation counts. */

SELECT 'linkaja_raw_transactions' AS relation, COUNT(*) AS row_count
FROM linkaja_raw_transactions
UNION ALL
SELECT 'linkaja_transaction_base_v', COUNT(*)
FROM linkaja_transaction_base_v
UNION ALL
SELECT 'linkaja_transactions_current_v', COUNT(*)
FROM linkaja_transactions_current_v
UNION ALL
SELECT 'linkaja_transactions', COUNT(*)
FROM linkaja_transactions
UNION ALL
SELECT 'linkaja_monthly_refreshes', COUNT(*)
FROM linkaja_monthly_refreshes
ORDER BY relation;
