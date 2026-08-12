/*
Official LinkAja monthly fee release report.

Run linkaja_monthly_materialization_v1 with publish=true first, then edit only
report_month and requested_cluster_id. A NULL cluster includes every published
cluster for the month.

Digipos is sourced only from original completed `Digipos B2B Transfer Fee`
transactions whose transaction-grain Fee is exactly Rp200. Company DEBIT and
KREDIT never determine the Digipos fee. PPOB uses its transaction-grain Fee;
an active missing PPOB Fee makes net/payable NULL instead of zero.
*/

WITH parameters AS (
    SELECT
        DATE '2026-07-01' AS report_month,
        NULL::TEXT AS requested_cluster_id
)
SELECT
    summary.report_month,
    summary.calculation_cutoff,
    summary.materialization_id,
    summary.cluster_id,
    summary.fee_category,
    summary.gross_transaction_count,
    summary.reversed_transaction_count,
    summary.active_transaction_count,
    summary.gross_fee,
    summary.reversed_fee,
    summary.net_fee,
    summary.monthly_payable_fee,
    summary.gross_missing_fee_count,
    summary.reversed_missing_fee_count,
    summary.active_missing_fee_count,
    summary.calculation_status
FROM linkaja_monthly_fee_summary_v AS summary
CROSS JOIN parameters
WHERE summary.report_month = parameters.report_month
  AND (
      parameters.requested_cluster_id IS NULL
      OR summary.cluster_id = parameters.requested_cluster_id
  )
ORDER BY summary.report_month, summary.cluster_id, summary.fee_category;


/*
Current unresolved reversals inside each published cluster's closing window.

The publication audit preserves the unresolved count seen when the snapshot was
published. This detail query is intentionally live: a late-arriving original
whose finalized timestamp is before the frozen cutoff removes the exception.
Run linkaja_monthly_refresh_candidates.sql and republish affected cluster-months
before approving a release when live state changed after publication.
*/

WITH parameters AS (
    SELECT
        DATE '2026-07-01' AS report_month,
        NULL::TEXT AS requested_cluster_id
), published_snapshots AS (
    SELECT DISTINCT ON (refresh.cluster_id, refresh.report_month)
        refresh.cluster_id,
        refresh.report_month,
        refresh.calculation_cutoff,
        refresh.materialization_id,
        refresh.unresolved_reversal_count,
        refresh.refreshed_at
    FROM linkaja_monthly_refreshes AS refresh
    CROSS JOIN parameters
    WHERE refresh.report_month = parameters.report_month
      AND (
          parameters.requested_cluster_id IS NULL
          OR refresh.cluster_id = parameters.requested_cluster_id
      )
    ORDER BY
        refresh.cluster_id,
        refresh.report_month,
        refresh.refreshed_at DESC,
        refresh.materialization_id DESC
)
SELECT
    publication.report_month,
    publication.calculation_cutoff,
    publication.materialization_id,
    publication.refreshed_at AS snapshot_refreshed_at,
    publication.unresolved_reversal_count
        AS published_unresolved_reversal_count,
    reversal.cluster_id,
    reversal.report_date AS reversal_report_date,
    reversal.finalized_at_local,
    reversal.transaction_id AS reversal_transaction_id,
    reversal.original_transaction_id,
    reversal.transaction_scenario AS reversal_scenario,
    reversal.company_debit,
    reversal.company_credit,
    'UNRESOLVED' AS reversal_resolution_status,
    ARRAY['UNRESOLVED_REVERSAL']::TEXT[] AS unusual_reason_codes,
    reversal.source_files,
    reversal.updated_load_id
FROM published_snapshots AS publication
JOIN linkaja_transaction_base_v AS reversal
  ON reversal.cluster_id = publication.cluster_id
 AND reversal.finalized_at_local >= publication.report_month
 AND reversal.finalized_at_local < publication.calculation_cutoff
WHERE reversal.original_transaction_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM linkaja_raw_transactions AS original
      WHERE original.cluster_id = reversal.cluster_id
        AND original.transaction_id = reversal.original_transaction_id
        AND LOWER(BTRIM(COALESCE(original.transaction_status, ''))) =
              'completed'
        AND original.finalized_date + original.finalized_time <
              publication.calculation_cutoff
  )
ORDER BY
    reversal.cluster_id,
    reversal.report_date,
    reversal.finalized_at_local,
    reversal.transaction_id;
