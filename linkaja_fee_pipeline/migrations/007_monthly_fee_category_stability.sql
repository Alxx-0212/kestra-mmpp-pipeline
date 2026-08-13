/*
Keep the two approved monthly fee categories stable and make release status
depend on missing fees among active transactions. A completed linked reversal
makes the directly referenced original inactive; the reversal event itself is
not a fee target.
*/

DROP VIEW linkaja_monthly_fee_summary_v;

CREATE VIEW linkaja_monthly_fee_summary_v AS
WITH latest_publications AS (
    SELECT DISTINCT ON (refresh.cluster_id, refresh.report_month)
        refresh.report_month,
        refresh.calculation_cutoff,
        refresh.materialization_id,
        refresh.cluster_id
    FROM linkaja_monthly_refreshes AS refresh
    ORDER BY
        refresh.cluster_id,
        refresh.report_month,
        refresh.refreshed_at DESC,
        refresh.materialization_id DESC
), fee_categories AS (
    SELECT fee_category
    FROM (VALUES
        ('Digipos B2B Transfer Fee'::TEXT),
        ('General to Purchase B2B Transfer Agent Telco'::TEXT)
    ) AS categories(fee_category)
), monthly_targets AS (
    SELECT
        publication.report_month,
        publication.calculation_cutoff,
        publication.materialization_id,
        publication.cluster_id,
        transaction.transaction_scenario AS fee_category,
        transaction.is_reversed,
        transaction.source_fee AS fee_amount
    FROM latest_publications AS publication
    JOIN linkaja_transactions AS transaction
      ON transaction.cluster_id = publication.cluster_id
     AND transaction.report_month = publication.report_month
     AND transaction.materialization_id = publication.materialization_id
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
    publication.report_month,
    publication.calculation_cutoff,
    publication.materialization_id,
    publication.cluster_id,
    category.fee_category,
    COALESCE(aggregate.gross_transaction_count, 0)
        AS gross_transaction_count,
    COALESCE(aggregate.reversed_transaction_count, 0)
        AS reversed_transaction_count,
    COALESCE(aggregate.active_transaction_count, 0)
        AS active_transaction_count,
    CASE
        WHEN COALESCE(aggregate.gross_missing_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_gross_fee, 0)
    END AS gross_fee,
    CASE
        WHEN COALESCE(aggregate.reversed_missing_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_reversed_fee, 0)
    END AS reversed_fee,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_net_fee, 0)
    END AS net_fee,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_net_fee, 0)
    END AS monthly_payable_fee,
    COALESCE(aggregate.gross_missing_fee_count, 0)
        AS gross_missing_fee_count,
    COALESCE(aggregate.reversed_missing_fee_count, 0)
        AS reversed_missing_fee_count,
    COALESCE(aggregate.active_missing_fee_count, 0)
        AS active_missing_fee_count,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0
            THEN 'INCOMPLETE_MISSING_FEE'
        ELSE 'COMPLETE'
    END AS calculation_status
FROM latest_publications AS publication
CROSS JOIN fee_categories AS category
LEFT JOIN monthly_aggregates AS aggregate
  ON aggregate.report_month = publication.report_month
 AND aggregate.calculation_cutoff = publication.calculation_cutoff
 AND aggregate.materialization_id = publication.materialization_id
 AND aggregate.cluster_id = publication.cluster_id
 AND aggregate.fee_category = category.fee_category;
