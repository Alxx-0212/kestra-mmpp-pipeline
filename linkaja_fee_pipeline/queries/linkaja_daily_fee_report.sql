/*
Daily LinkAja central-fee monitoring query.

Set cluster_id and the inclusive/exclusive report-date interval. The returned
EXPECTED FEE and IN CLUSTER FEE values match the live active daily fee
calculation: non-reversed out-cluster completed company-credit transactions *
200, plus non-reversed in-cluster transactions * 20. Completed reversal events
remain on their actual posting date but contribute no fee.
*/

WITH parameters AS (
    SELECT
        '411311'::TEXT AS cluster_id,
        DATE '2026-07-01' AS start_date,
        DATE '2026-08-01' AS end_date_exclusive
),
requested_dates AS (
    SELECT generated_date::DATE AS report_date
    FROM parameters
    CROSS JOIN GENERATE_SERIES(
        parameters.start_date,
        parameters.end_date_exclusive - 1,
        INTERVAL '1 day'
    ) AS generated_date
),
transaction_totals AS (
    SELECT
        transaction.report_date,
        COUNT(*) FILTER (
            WHERE transaction.transaction_scenario = 'Digipos B2B Transfer'
              AND transaction.company_credit > 0
              AND NOT transaction.is_reversal
              AND NOT transaction.is_reversed
        ) * 200 AS expected_fee,
        COUNT(*) FILTER (
            WHERE transaction.transaction_scenario =
                    'Digipos B2B Transfer In Cluster'
              AND NOT transaction.is_reversal
              AND NOT transaction.is_reversed
        ) * 20 AS in_cluster_fee
    FROM linkaja_transactions_current_v AS transaction
    CROSS JOIN parameters
    WHERE transaction.cluster_id = parameters.cluster_id
      AND transaction.report_date >= parameters.start_date
      AND transaction.report_date < parameters.end_date_exclusive
    GROUP BY transaction.report_date
),
raw_totals AS (
    SELECT
        raw.finalized_date AS report_date,
        COUNT(*) AS source_rows,
        STRING_AGG(DISTINCT raw.source_file, ', ' ORDER BY raw.source_file)
            AS source_file
    FROM linkaja_raw_transactions AS raw
    CROSS JOIN parameters
    WHERE raw.cluster_id = parameters.cluster_id
      AND raw.finalized_date >= parameters.start_date
      AND raw.finalized_date < parameters.end_date_exclusive
    GROUP BY raw.finalized_date
)
SELECT
    requested_dates.report_date,
    parameters.cluster_id,
    COALESCE(transaction_totals.expected_fee, 0) AS expected_fee,
    COALESCE(transaction_totals.in_cluster_fee, 0) AS in_cluster_fee,
    COALESCE(raw_totals.source_rows, 0) AS source_rows,
    COALESCE(raw_totals.source_file, '') AS source_file
FROM requested_dates
CROSS JOIN parameters
LEFT JOIN transaction_totals
  ON transaction_totals.report_date = requested_dates.report_date
LEFT JOIN raw_totals
  ON raw_totals.report_date = requested_dates.report_date
ORDER BY requested_dates.report_date;
