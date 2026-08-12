/*
Published LinkAja monthly snapshot audit.

Set requested_cluster_id and requested_report_month. NULL values include all
published clusters or months, respectively.
*/

WITH parameters AS (
    SELECT
        NULL::TEXT AS requested_cluster_id,
        NULL::DATE AS requested_report_month
)
SELECT
    refresh.cluster_id,
    refresh.report_month,
    refresh.calculation_cutoff,
    refresh.materialization_id,
    refresh.transaction_rows,
    refresh.unresolved_reversal_count,
    refresh.refreshed_at,
    (
        SELECT COUNT(*)
        FROM linkaja_transactions AS snapshot
        WHERE snapshot.cluster_id = refresh.cluster_id
          AND snapshot.report_month = refresh.report_month
          AND snapshot.materialization_id = refresh.materialization_id
    ) AS published_snapshot_rows
FROM linkaja_monthly_refreshes AS refresh
CROSS JOIN parameters
WHERE (
    parameters.requested_cluster_id IS NULL
    OR refresh.cluster_id = parameters.requested_cluster_id
)
  AND (
      parameters.requested_report_month IS NULL
      OR refresh.report_month = parameters.requested_report_month
  )
ORDER BY refresh.report_month DESC, refresh.cluster_id, refresh.refreshed_at DESC;
