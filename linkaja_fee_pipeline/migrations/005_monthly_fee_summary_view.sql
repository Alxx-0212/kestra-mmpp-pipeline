CREATE VIEW linkaja_monthly_fee_summary_v AS
WITH monthly_targets AS (
    SELECT
        transaction.report_month,
        transaction.calculation_cutoff,
        transaction.materialization_id,
        transaction.cluster_id,
        transaction.transaction_scenario AS fee_category,
        transaction.is_reversed,
        transaction.source_fee AS fee_amount
    FROM linkaja_transactions AS transaction
    WHERE NOT transaction.is_reversal
      AND (
          (
              transaction.transaction_scenario =
                  'Digipos B2B Transfer Fee'
              AND transaction.source_fee = 200::NUMERIC
          )
          OR transaction.transaction_scenario =
              'General to Purchase B2B Transfer Agent Telco'
      )
), monthly_aggregates AS (
    SELECT
        report_month,
        calculation_cutoff,
        materialization_id,
        cluster_id,
        fee_category,
        COUNT(*) AS gross_transaction_count,
        COUNT(*) FILTER (WHERE is_reversed) AS reversed_transaction_count,
        COUNT(*) FILTER (WHERE NOT is_reversed) AS active_transaction_count,
        COUNT(*) FILTER (WHERE fee_amount IS NULL)
            AS gross_missing_fee_count,
        COUNT(*) FILTER (
            WHERE is_reversed
              AND fee_amount IS NULL
        ) AS reversed_missing_fee_count,
        COUNT(*) FILTER (
            WHERE NOT is_reversed
              AND fee_amount IS NULL
        ) AS active_missing_fee_count,
        COALESCE(SUM(fee_amount), 0) AS calculated_gross_fee,
        COALESCE(
            SUM(fee_amount) FILTER (WHERE is_reversed),
            0
        ) AS calculated_reversed_fee,
        COALESCE(
            SUM(fee_amount) FILTER (WHERE NOT is_reversed),
            0
        ) AS calculated_net_fee
    FROM monthly_targets
    GROUP BY
        report_month,
        calculation_cutoff,
        materialization_id,
        cluster_id,
        fee_category
)
SELECT
    report_month,
    calculation_cutoff,
    materialization_id,
    cluster_id,
    fee_category,
    gross_transaction_count,
    reversed_transaction_count,
    active_transaction_count,
    CASE
        WHEN gross_missing_fee_count > 0 THEN NULL
        ELSE calculated_gross_fee
    END AS gross_fee,
    CASE
        WHEN reversed_missing_fee_count > 0 THEN NULL
        ELSE calculated_reversed_fee
    END AS reversed_fee,
    CASE
        WHEN active_missing_fee_count > 0 THEN NULL
        ELSE calculated_net_fee
    END AS net_fee,
    CASE
        WHEN active_missing_fee_count > 0 THEN NULL
        ELSE calculated_net_fee
    END AS monthly_payable_fee,
    gross_missing_fee_count,
    reversed_missing_fee_count,
    active_missing_fee_count,
    CASE
        WHEN gross_missing_fee_count > 0
            THEN 'INCOMPLETE_MISSING_FEE'
        ELSE 'COMPLETE'
    END AS calculation_status
FROM monthly_aggregates;
