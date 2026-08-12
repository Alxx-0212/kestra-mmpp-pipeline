/*
Conservative late-data detector for published LinkAja cluster-month snapshots.

Set report_month and requested_cluster_id. A returned row means raw data that
can affect the frozen monthly facts or its closing-window reversal exceptions
was ingested after publication. Preview and republish that cluster-month.

Republishing with the same cutoff captures late uploads whose finalized
timestamp was already inside the close. Use a later cutoff only when the close
is intentionally extended.
*/

WITH parameters AS (
    SELECT
        DATE '2026-07-01' AS report_month,
        NULL::TEXT AS requested_cluster_id
), latest_publications AS (
    SELECT DISTINCT ON (refresh.cluster_id, refresh.report_month)
        refresh.cluster_id,
        refresh.report_month,
        refresh.calculation_cutoff,
        refresh.materialization_id,
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
), late_impacts AS (
    SELECT DISTINCT
        publication.cluster_id,
        publication.report_month,
        publication.calculation_cutoff,
        publication.materialization_id,
        publication.refreshed_at,
        raw.transaction_id,
        raw.load_id,
        raw.source_file,
        raw.ingested_at,
        'MONTH_TRANSACTION_CHANGED'::TEXT AS impact_reason
    FROM latest_publications AS publication
    JOIN linkaja_raw_transactions AS raw
      ON raw.cluster_id = publication.cluster_id
     AND raw.ingested_at > publication.refreshed_at
     AND raw.finalized_date >= publication.report_month
     AND raw.finalized_date <
           (publication.report_month + INTERVAL '1 month')::DATE
     AND raw.finalized_date + raw.finalized_time <
           publication.calculation_cutoff

    UNION ALL

    SELECT DISTINCT
        publication.cluster_id,
        publication.report_month,
        publication.calculation_cutoff,
        publication.materialization_id,
        publication.refreshed_at,
        reversal.transaction_id,
        reversal.load_id,
        reversal.source_file,
        reversal.ingested_at,
        'CLOSING_WINDOW_REVERSAL_CHANGED'::TEXT AS impact_reason
    FROM latest_publications AS publication
    JOIN linkaja_raw_transactions AS reversal
      ON reversal.cluster_id = publication.cluster_id
     AND reversal.ingested_at > publication.refreshed_at
     AND reversal.original_transaction_id IS NOT NULL
     AND BTRIM(reversal.original_transaction_id) <> ''
     AND reversal.finalized_date >= publication.report_month
     AND reversal.finalized_date + reversal.finalized_time <
           publication.calculation_cutoff
    WHERE EXISTS (
        SELECT 1
        FROM linkaja_raw_transactions AS original
        WHERE original.cluster_id = reversal.cluster_id
          AND original.transaction_id = reversal.original_transaction_id
          AND original.finalized_date >= publication.report_month
          AND original.finalized_date <
                (publication.report_month + INTERVAL '1 month')::DATE
          AND original.finalized_date + original.finalized_time <
                publication.calculation_cutoff
    )

    UNION ALL

    SELECT DISTINCT
        publication.cluster_id,
        publication.report_month,
        publication.calculation_cutoff,
        publication.materialization_id,
        publication.refreshed_at,
        original.transaction_id,
        original.load_id,
        original.source_file,
        original.ingested_at,
        'ORIGINAL_FOR_CLOSING_REVERSAL_CHANGED'::TEXT AS impact_reason
    FROM latest_publications AS publication
    JOIN linkaja_raw_transactions AS original
      ON original.cluster_id = publication.cluster_id
     AND original.ingested_at > publication.refreshed_at
     AND original.finalized_date + original.finalized_time <
           publication.calculation_cutoff
    WHERE EXISTS (
        SELECT 1
        FROM linkaja_raw_transactions AS reversal
        WHERE reversal.cluster_id = original.cluster_id
          AND reversal.original_transaction_id = original.transaction_id
          AND reversal.finalized_date >= publication.report_month
          AND reversal.finalized_date + reversal.finalized_time <
                publication.calculation_cutoff
    )
)
SELECT
    cluster_id,
    report_month,
    calculation_cutoff,
    materialization_id,
    refreshed_at,
    COUNT(DISTINCT transaction_id) AS impacted_transaction_count,
    COUNT(DISTINCT load_id) AS late_load_count,
    MIN(ingested_at) AS first_late_ingestion_at,
    MAX(ingested_at) AS latest_late_ingestion_at,
    ARRAY_AGG(DISTINCT impact_reason ORDER BY impact_reason) AS impact_reasons,
    STRING_AGG(DISTINCT source_file, ', ' ORDER BY source_file)
        AS late_source_files
FROM late_impacts
GROUP BY
    cluster_id,
    report_month,
    calculation_cutoff,
    materialization_id,
    refreshed_at
ORDER BY report_month, cluster_id;
