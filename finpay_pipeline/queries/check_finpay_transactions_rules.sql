-- Validate rows currently persisted in finpay_transactions.
-- Expected result for a clean table: zero rows.
--
-- Notes:
-- - This checks rows that are present in finpay_transactions only.
-- - It cannot prove excluded source rows were handled correctly; compare with
--   finpay_raw_transactions / finpay_unusual_transactions for that.
-- - Label checks normalize processed_transaction_label to lowercase so older
--   mixed-case batches can still be validated while also being reported by the
--   *_not_lowercase checks.

WITH normalized AS (
    SELECT
        cluster_id,
        report_date,
        transaction_date,
        transaction_id,
        base_id,
        transaction_id_type,
        transaction_date::date AS transaction_day,
        COALESCE(kredit, 0)::numeric AS kredit,
        COALESCE(debet, 0)::numeric AS debet,
        raw_transaction_label,
        processed_transaction_label,
        LOWER(BTRIM(COALESCE(processed_transaction_label, ''))) AS label,
        LOWER(BTRIM(COALESCE(raw_transaction_label, ''))) AS raw_label,
        LOWER(COALESCE(remarks, '')) AS remarks_lc,
        REGEXP_REPLACE(
            COALESCE(transaction_id, ''),
            '(SLSFEE|SALESFEE|FEE)$',
            '',
            'i'
        ) AS expected_base_id,
        CASE
            WHEN UPPER(COALESCE(transaction_id, '')) ~ '(SLSFEE|SALESFEE|FEE)$'
                THEN SUBSTRING(
                    UPPER(COALESCE(transaction_id, ''))
                    FROM '(SLSFEE|SALESFEE|FEE)$'
                )
            ELSE 'MAIN'
        END AS expected_transaction_id_type
    FROM finpay_transactions
),
allowed_labels(label) AS (
    VALUES
        ('cashout apollo'),
        ('qrisduwit'),
        ('disbursement'),
        ('feetransaksi'),
        ('recharge'),
        ('rechargefee'),
        ('pembelian recharge out cluster'),
        ('recharge out cluster'),
        ('recharge out cluster fee'),
        ('reversal - ngrs'),
        ('reversal - ngrs fee'),
        ('reversal - pembelian recharge out cluster'),
        ('reversal - recharge out cluster'),
        ('reversal - recharge out cluster fee'),
        ('sellthru'),
        ('sellthrufee'),
        ('sellthrusalesfee')
),
grouped AS (
    SELECT
        cluster_id,
        report_date,
        base_id,
        ARRAY_AGG(DISTINCT label ORDER BY label) AS labels,
        COUNT(*) FILTER (WHERE label = 'recharge') AS recharge_rows,
        COUNT(*) FILTER (WHERE label = 'rechargefee') AS rechargefee_rows,
        COALESCE(SUM(debet) FILTER (WHERE label = 'rechargefee'), 0) AS rechargefee_debet,
        COUNT(*) FILTER (WHERE label = 'recharge out cluster') AS recharge_out_cluster_rows,
        COUNT(*) FILTER (WHERE label = 'recharge out cluster fee') AS recharge_out_cluster_fee_rows,
        COALESCE(SUM(debet) FILTER (WHERE label = 'recharge out cluster fee'), 0) AS recharge_out_cluster_fee_debet,
        COUNT(*) FILTER (WHERE label = 'sellthru') AS sellthru_rows,
        COUNT(*) FILTER (WHERE label = 'sellthrufee') AS sellthrufee_rows,
        COALESCE(SUM(debet) FILTER (WHERE label = 'sellthrufee'), 0) AS sellthrufee_debet,
        COUNT(*) FILTER (WHERE label = 'sellthrusalesfee') AS sellthrusalesfee_rows,
        COUNT(*) FILTER (WHERE label = 'reversal - ngrs') AS reversal_ngrs_rows,
        COUNT(*) FILTER (WHERE label = 'reversal - ngrs fee') AS reversal_ngrs_fee_rows,
        COALESCE(SUM(kredit) FILTER (WHERE label = 'reversal - ngrs fee'), 0) AS reversal_ngrs_fee_kredit,
        COUNT(*) FILTER (WHERE label = 'reversal - recharge out cluster') AS reversal_recharge_out_cluster_rows,
        COUNT(*) FILTER (WHERE label = 'reversal - recharge out cluster fee') AS reversal_recharge_out_cluster_fee_rows,
        COALESCE(SUM(kredit) FILTER (WHERE label = 'reversal - recharge out cluster fee'), 0) AS reversal_recharge_out_cluster_fee_kredit,
        BOOL_OR(
            label = 'sellthru'
            AND raw_label = 'recharge'
            AND BTRIM(remarks_lc) = 'transaksi sellthru'
        ) AS standalone_sellthru,
        BOOL_OR(label = 'sellthru' AND transaction_day >= DATE '2026-09-01') AS st_current_rows,
        BOOL_OR(label = 'sellthru' AND transaction_day < DATE '2026-09-01') AS st_legacy_rows
    FROM normalized
    GROUP BY cluster_id, report_date, base_id
),
violations AS (
    SELECT
        'raw_label_not_lowercase' AS rule_name,
        'row' AS scope,
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        CONCAT(
            'raw_transaction_label must be lowercase; found ',
            COALESCE(raw_transaction_label, '<null>')
        ) AS details
    FROM normalized
    WHERE raw_transaction_label IS NOT NULL
      AND raw_transaction_label <> LOWER(raw_transaction_label)

    UNION ALL

    SELECT
        'processed_label_not_lowercase',
        'row',
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        CONCAT(
            'processed_transaction_label must be lowercase; found ',
            COALESCE(processed_transaction_label, '<null>')
        )
    FROM normalized
    WHERE processed_transaction_label IS NOT NULL
      AND processed_transaction_label <> LOWER(processed_transaction_label)

    UNION ALL

    SELECT
        'processed_label_not_allowed',
        'row',
        n.cluster_id,
        n.report_date,
        n.base_id,
        n.transaction_id,
        n.label,
        CONCAT('unexpected processed_transaction_label: ', COALESCE(n.label, '<null>'))
    FROM normalized n
    LEFT JOIN allowed_labels a ON a.label = n.label
    WHERE n.label = ''
       OR a.label IS NULL

    UNION ALL

    SELECT
        'excluded_label_present_in_summary_table',
        'row',
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        'raw REVERSAL or ST reversal labels should not be present in finpay_transactions'
    FROM normalized
    WHERE label = 'reversal'
       OR label LIKE 'reversal - st%'

    UNION ALL

    SELECT
        'debet_kredit_both_nonzero',
        'row',
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        CONCAT('kredit=', kredit, ', debet=', debet)
    FROM normalized
    WHERE kredit <> 0
      AND debet <> 0

    UNION ALL

    SELECT
        'base_id_mismatch',
        'row',
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        CONCAT('base_id=', COALESCE(base_id, '<null>'), ', expected=', expected_base_id)
    FROM normalized
    WHERE COALESCE(transaction_id, '') <> ''
      AND COALESCE(base_id, '') <> expected_base_id

    UNION ALL

    SELECT
        'transaction_id_type_mismatch',
        'row',
        cluster_id,
        report_date,
        base_id,
        transaction_id,
        label,
        CONCAT(
            'transaction_id_type=',
            COALESCE(transaction_id_type, '<null>'),
            ', expected=',
            expected_transaction_id_type
        )
    FROM normalized
    WHERE COALESCE(transaction_id_type, '') <> expected_transaction_id_type

    UNION ALL

    SELECT
        'recharge_fee_total_invalid',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'RECHARGE requires RECHARGEFEE debet total = 20; found ',
            rechargefee_debet,
            '; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE recharge_rows > 0
      AND rechargefee_debet <> 20

    UNION ALL

    SELECT
        'rechargefee_without_recharge',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('RECHARGEFEE exists without RECHARGE; labels=', ARRAY_TO_STRING(labels, ', '))
    FROM grouped
    WHERE rechargefee_rows > 0
      AND recharge_rows = 0

    UNION ALL

    SELECT
        'rechargefee_cap_exceeded',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('RECHARGEFEE debet total exceeds 20; found ', rechargefee_debet)
    FROM grouped
    WHERE rechargefee_debet > 20

    UNION ALL

    SELECT
        'recharge_out_cluster_fee_without_main',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'RECHARGE OUT CLUSTER FEE exists without RECHARGE OUT CLUSTER; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE recharge_out_cluster_fee_rows > 0
      AND recharge_out_cluster_rows = 0

    UNION ALL

    SELECT
        'recharge_out_cluster_fee_cap_exceeded',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'RECHARGE OUT CLUSTER FEE debet total exceeds 20; found ',
            recharge_out_cluster_fee_debet
        )
    FROM grouped
    WHERE recharge_out_cluster_fee_debet > 20

    UNION ALL

    SELECT
        'sellthru_fee_total_invalid',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'SELLTHRU requires SELLTHRUFEE debet total = 100; found ',
            sellthrufee_debet,
            '; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE sellthru_rows > 0
      AND NOT standalone_sellthru
      AND sellthrufee_debet <> 100

    UNION ALL

    SELECT
        'sellthru_salesfee_missing',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('SELLTHRU requires SELLTHRUSALESFEE; labels=', ARRAY_TO_STRING(labels, ', '))
    FROM grouped
    WHERE sellthru_rows > 0
      AND NOT standalone_sellthru
      AND st_legacy_rows
      AND sellthrusalesfee_rows = 0

    UNION ALL

    SELECT
        'sellthru_salesfee_retired',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'SELLTHRUSALESFEE is retired from 2026-09-01; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE sellthru_rows > 0
      AND NOT standalone_sellthru
      AND st_current_rows
      AND sellthrusalesfee_rows > 0

    UNION ALL

    SELECT
        'sellthru_cutover_mixed_family',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        'SELLTHRU family crosses the 2026-09-01 rule cutoff'
    FROM grouped
    WHERE sellthru_rows > 0
      AND st_current_rows
      AND st_legacy_rows

    UNION ALL

    SELECT
        'sellthru_fee_without_sellthru',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('SELLTHRU fee rows exist without SELLTHRU; labels=', ARRAY_TO_STRING(labels, ', '))
    FROM grouped
    WHERE (sellthrufee_rows > 0 OR sellthrusalesfee_rows > 0)
      AND sellthru_rows = 0

    UNION ALL

    SELECT
        'sellthrufee_cap_exceeded',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('SELLTHRUFEE debet total exceeds 100; found ', sellthrufee_debet)
    FROM grouped
    WHERE sellthrufee_debet > 100

    UNION ALL

    SELECT
        'reversal_ngrs_fee_total_invalid',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'Reversal - NGRS requires Reversal - NGRS FEE kredit total = 20; found ',
            reversal_ngrs_fee_kredit,
            '; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE reversal_ngrs_rows > 0
      AND reversal_ngrs_fee_kredit <> 20

    UNION ALL

    SELECT
        'reversal_ngrs_fee_without_main',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'Reversal - NGRS FEE exists without Reversal - NGRS; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE reversal_ngrs_fee_rows > 0
      AND reversal_ngrs_rows = 0

    UNION ALL

    SELECT
        'reversal_ngrs_fee_cap_exceeded',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT('Reversal - NGRS FEE kredit total exceeds 20; found ', reversal_ngrs_fee_kredit)
    FROM grouped
    WHERE reversal_ngrs_fee_kredit > 20

    UNION ALL

    SELECT
        'reversal_recharge_out_cluster_fee_total_invalid',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'Reversal - Recharge Out Cluster requires fee kredit total = 20; found ',
            reversal_recharge_out_cluster_fee_kredit,
            '; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE reversal_recharge_out_cluster_rows > 0
      AND reversal_recharge_out_cluster_fee_kredit <> 20

    UNION ALL

    SELECT
        'reversal_recharge_out_cluster_fee_without_main',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'Reversal - Recharge Out Cluster FEE exists without main row; labels=',
            ARRAY_TO_STRING(labels, ', ')
        )
    FROM grouped
    WHERE reversal_recharge_out_cluster_fee_rows > 0
      AND reversal_recharge_out_cluster_rows = 0

    UNION ALL

    SELECT
        'reversal_recharge_out_cluster_fee_cap_exceeded',
        'group',
        cluster_id,
        report_date,
        base_id,
        NULL::text,
        NULL::text,
        CONCAT(
            'Reversal - Recharge Out Cluster FEE kredit total exceeds 20; found ',
            reversal_recharge_out_cluster_fee_kredit
        )
    FROM grouped
    WHERE reversal_recharge_out_cluster_fee_kredit > 20
)
SELECT *
FROM violations
ORDER BY
    cluster_id,
    report_date,
    base_id NULLS FIRST,
    transaction_id NULLS FIRST,
    rule_name;
