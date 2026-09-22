"""PostgreSQL runtime statements for the migrated LinkAja schema."""


CREATE_STAGE_STATEMENT = """
CREATE TEMP TABLE linkaja_raw_stage
(LIKE linkaja_raw_transactions INCLUDING DEFAULTS)
ON COMMIT DROP
"""


STAGE_CONFLICTS_STATEMENT = """
SELECT
    cluster_id,
    transaction_id,
    COUNT(DISTINCT finalized_date) AS finalized_dates,
    COUNT(DISTINCT finalized_time) AS finalized_times,
    COUNT(DISTINCT transaction_scenario) AS scenarios,
    COUNT(DISTINCT LOWER(BTRIM(COALESCE(transaction_status, '')))) AS statuses,
    COUNT(DISTINCT COALESCE(transaction_type, '<NULL>')) AS transaction_types,
    COUNT(DISTINCT COALESCE(NULLIF(BTRIM(original_transaction_id), ''), '<NULL>'))
        AS original_ids,
    COUNT(DISTINCT fee) FILTER (WHERE fee IS NOT NULL) AS fee_values,
    COUNT(DISTINCT account) FILTER (
        WHERE organization = top_organization
          AND POSITION('organization mfs purchase account' IN LOWER(account)) > 0
    ) AS company_accounts
FROM linkaja_raw_stage
GROUP BY cluster_id, transaction_id
HAVING COUNT(DISTINCT finalized_date) > 1
    OR COUNT(DISTINCT finalized_time) > 1
    OR COUNT(DISTINCT transaction_scenario) > 1
    OR COUNT(DISTINCT LOWER(BTRIM(COALESCE(transaction_status, '')))) > 1
    OR COUNT(DISTINCT COALESCE(transaction_type, '<NULL>')) > 1
    OR COUNT(DISTINCT COALESCE(NULLIF(BTRIM(original_transaction_id), ''), '<NULL>')) > 1
    OR COUNT(DISTINCT fee) FILTER (WHERE fee IS NOT NULL) > 1
    OR COUNT(DISTINCT account) FILTER (
        WHERE organization = top_organization
          AND POSITION('organization mfs purchase account' IN LOWER(account)) > 0
    ) > 1
ORDER BY cluster_id, transaction_id
LIMIT 20
"""


CREATE_REFRESH_SCOPE_STATEMENTS = [
    """
    CREATE TEMP TABLE linkaja_impacted_ids (
        cluster_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        PRIMARY KEY (cluster_id, transaction_id)
    ) ON COMMIT DROP
    """,
    """
    CREATE TEMP TABLE linkaja_affected_dates (
        cluster_id TEXT NOT NULL,
        report_date DATE NOT NULL,
        PRIMARY KEY (cluster_id, report_date)
    ) ON COMMIT DROP
    """,
    """
    INSERT INTO linkaja_impacted_ids (cluster_id, transaction_id)
    SELECT DISTINCT cluster_id, transaction_id
    FROM linkaja_raw_stage
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO linkaja_impacted_ids (cluster_id, transaction_id)
    SELECT DISTINCT existing.cluster_id, existing.original_transaction_id
    FROM linkaja_raw_transactions AS existing
    JOIN linkaja_raw_stage AS incoming
      ON incoming.cluster_id = existing.cluster_id
     AND incoming.transaction_id = existing.transaction_id
    WHERE existing.original_transaction_id IS NOT NULL
      AND BTRIM(existing.original_transaction_id) <> ''
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO linkaja_impacted_ids (cluster_id, transaction_id)
    SELECT DISTINCT cluster_id, original_transaction_id
    FROM linkaja_raw_stage
    WHERE original_transaction_id IS NOT NULL
      AND BTRIM(original_transaction_id) <> ''
    ON CONFLICT DO NOTHING
    """,
]


EXPAND_IMPACTED_IDS_STATEMENT = """
WITH RECURSIVE reversal_edges AS (
    SELECT DISTINCT
        cluster_id,
        transaction_id AS from_id,
        original_transaction_id AS to_id
    FROM linkaja_raw_transactions
    WHERE original_transaction_id IS NOT NULL
      AND BTRIM(original_transaction_id) <> ''

    UNION

    SELECT DISTINCT
        cluster_id,
        original_transaction_id AS from_id,
        transaction_id AS to_id
    FROM linkaja_raw_transactions
    WHERE original_transaction_id IS NOT NULL
      AND BTRIM(original_transaction_id) <> ''
), related_ids AS (
    SELECT cluster_id, transaction_id
    FROM linkaja_impacted_ids

    UNION

    SELECT edges.cluster_id, edges.to_id
    FROM related_ids
    JOIN reversal_edges AS edges
      ON edges.cluster_id = related_ids.cluster_id
     AND edges.from_id = related_ids.transaction_id
)
INSERT INTO linkaja_impacted_ids (cluster_id, transaction_id)
SELECT DISTINCT cluster_id, transaction_id
FROM related_ids
ON CONFLICT DO NOTHING
"""


CAPTURE_AFFECTED_DATES_STATEMENT = """
INSERT INTO linkaja_affected_dates (cluster_id, report_date)
SELECT DISTINCT raw.cluster_id, raw.finalized_date
FROM linkaja_raw_transactions AS raw
JOIN linkaja_impacted_ids AS impacted
  ON impacted.cluster_id = raw.cluster_id
 AND impacted.transaction_id = raw.transaction_id
ON CONFLICT DO NOTHING
"""


REPLACE_RAW_STATEMENTS = [
    """
    DELETE FROM linkaja_raw_transactions AS existing
    USING (
        SELECT DISTINCT cluster_id, transaction_id
        FROM linkaja_raw_stage
    ) AS incoming
    WHERE existing.cluster_id = incoming.cluster_id
      AND existing.transaction_id = incoming.transaction_id
    """,
    """
    INSERT INTO linkaja_raw_transactions (
        cluster_id, source_file, source_row_number, load_id, source_no,
        top_organization, parent_organization, organization, transaction_id,
        original_transaction_id, partner_reference_number, invoice_id,
        finalized_date, finalized_time, initiate_date, initiate_time,
        transaction_type, transaction_scenario, transaction_status,
        transaction_statement, account, counter_party, debit, credit,
        balance, fee
    )
    SELECT
        cluster_id, source_file, source_row_number, load_id, source_no,
        top_organization, parent_organization, organization, transaction_id,
        original_transaction_id, partner_reference_number, invoice_id,
        finalized_date, finalized_time, initiate_date, initiate_time,
        transaction_type, transaction_scenario, transaction_status,
        transaction_statement, account, counter_party, debit, credit,
        balance, fee
    FROM linkaja_raw_stage
    """,
]


REPLACE_SOURCE_LOAD_STATEMENTS = [
    """
    UPDATE linkaja_source_loads
    SET is_current = FALSE,
        valid_to = CURRENT_TIMESTAMP
    WHERE cluster_id = %(cluster_id)s
      AND is_current
      AND COALESCE(source_end_date, source_start_date) >= %(source_start_date)s
      AND COALESCE(source_start_date, source_end_date) <= %(source_end_date)s
    """,
    """
    INSERT INTO linkaja_source_loads (
        load_id, cluster_id, source_file, source_sha256,
        source_start_date, source_end_date, row_count, valid_from,
        is_current, supersedes_load_id
    )
    SELECT
        %(load_id)s,
        %(cluster_id)s,
        %(source_file)s,
        %(source_sha256)s,
        %(source_start_date)s,
        %(source_end_date)s,
        %(source_rows)s,
        CURRENT_TIMESTAMP,
        TRUE,
        (
        SELECT load_id
        FROM linkaja_source_loads
        WHERE cluster_id = %(cluster_id)s
          AND NOT is_current
        ORDER BY valid_to DESC NULLS LAST, load_id DESC
        LIMIT 1
        )
    WHERE NOT EXISTS (
        SELECT 1
        FROM linkaja_source_loads AS existing
        WHERE existing.load_id = %(load_id)s
    )
    """,
]


INSERT_LEDGER_VERSION_STATEMENT = """
INSERT INTO linkaja_ledger_versions (
    cluster_id, source_file, source_row_number, load_id, ingested_at,
    source_no, top_organization, parent_organization, organization,
    transaction_id, original_transaction_id, partner_reference_number,
    invoice_id, finalized_date, finalized_time, initiate_date, initiate_time,
    transaction_type, transaction_scenario, transaction_status,
    transaction_statement, account, counter_party, debit, credit, balance,
    fee, source_row_hash
)
SELECT
    stage.cluster_id, stage.source_file, stage.source_row_number, stage.load_id,
    CURRENT_TIMESTAMP, stage.source_no, stage.top_organization,
    stage.parent_organization, stage.organization, stage.transaction_id,
    stage.original_transaction_id, stage.partner_reference_number,
    stage.invoice_id, stage.finalized_date, stage.finalized_time,
    stage.initiate_date, stage.initiate_time, stage.transaction_type,
    stage.transaction_scenario, stage.transaction_status,
    stage.transaction_statement, stage.account, stage.counter_party,
    stage.debit, stage.credit, stage.balance, stage.fee,
    md5(
        CONCAT_WS('|', stage.cluster_id, stage.transaction_id,
                  stage.finalized_date, stage.finalized_time,
                  stage.transaction_scenario, stage.debit, stage.credit,
                  stage.fee)
    )
FROM linkaja_raw_stage AS stage
"""


INSERT_MONTHLY_IMPACTS_STATEMENT = """
INSERT INTO linkaja_monthly_impacts (
    cluster_id, report_month, transaction_id, new_load_id, impact_reason
)
SELECT DISTINCT
    impacted.cluster_id,
    DATE_TRUNC('month', affected.report_date)::DATE,
    impacted.transaction_id,
    %(load_id)s,
    'SOURCE_LOAD_REPLACED'
FROM linkaja_impacted_ids AS impacted
JOIN linkaja_affected_dates AS affected
  ON affected.cluster_id = impacted.cluster_id
ON CONFLICT DO NOTHING
"""


AFFECTED_DATES_STATEMENT = """
SELECT report_date
FROM linkaja_affected_dates
WHERE cluster_id = %(cluster_id)s
ORDER BY report_date
"""


LOAD_COUNTS_STATEMENT = """
SELECT
    (SELECT COUNT(*) FROM linkaja_raw_transactions WHERE load_id = %(load_id)s)
        AS raw_rows,
    (SELECT COUNT(*) FROM linkaja_impacted_ids WHERE cluster_id = %(cluster_id)s)
        AS impacted_ids,
    (
        SELECT COUNT(*)
        FROM linkaja_transactions_current_v AS transactions
        JOIN linkaja_impacted_ids AS impacted
          ON impacted.cluster_id = transactions.cluster_id
         AND impacted.transaction_id = transactions.transaction_id
        WHERE transactions.cluster_id = %(cluster_id)s
    ) AS transaction_rows
"""


DAILY_SUMMARY_STATEMENT = """
WITH classified AS (
    SELECT
        transactions.*,
        CASE
            WHEN NOT transactions.is_reversal
                THEN transactions.transaction_scenario
            WHEN transactions.reversal_resolution_status = 'COMPLETE'
                THEN 'Complete Reversal - '
                    || transactions.original_transaction_scenario
            ELSE 'Unusual - Unresolved Reversal'
        END AS ledger_label
    FROM linkaja_transactions_current_v AS transactions
    WHERE transactions.cluster_id = %(cluster_id)s
      AND transactions.report_date = ANY(%(report_dates)s)
)
SELECT
    transactions.report_date,
    transactions.ledger_label,
    COALESCE(SUM(transactions.company_debit), 0) AS debit,
    COALESCE(SUM(transactions.company_credit), 0) AS credit,
    COALESCE(SUM(transactions.source_fee), 0) AS source_fee,
    COUNT(*) FILTER (
        WHERE transactions.transaction_scenario =
                'General to Purchase B2B Transfer Agent Telco'
          AND transactions.source_fee IS NULL
    ) AS source_fee_missing_count,
    COUNT(*) FILTER (
        WHERE NOT transactions.is_reversal
          AND NOT transactions.is_reversed
          AND transactions.transaction_scenario = 'Digipos B2B Transfer'
          AND transactions.company_credit > 0
    ) * %(expected_fee_per_transaction)s AS expected_fee,
    COUNT(*) FILTER (
        WHERE NOT transactions.is_reversal
          AND NOT transactions.is_reversed
          AND transactions.transaction_scenario =
              'Digipos B2B Transfer In Cluster'
    ) * %(in_cluster_fee_per_transaction)s AS in_cluster_fee,
    COUNT(*) FILTER (
        WHERE transactions.reversal_resolution_status = 'COMPLETE'
          AND transactions.original_transaction_scenario =
              'Digipos B2B Transfer In Cluster'
    ) AS reversal_in_cluster_count,
    COUNT(*) FILTER (
        WHERE transactions.reversal_resolution_status = 'COMPLETE'
          AND transactions.original_transaction_scenario =
              'Digipos B2B Transfer In Cluster'
    ) * %(reversal_in_cluster_fee_per_transaction)s
        AS reversal_in_cluster_fee,
    COUNT(*) FILTER (
        WHERE transactions.reversal_resolution_status = 'UNRESOLVED'
    ) AS unresolved_reversal_count
FROM classified AS transactions
GROUP BY transactions.report_date, transactions.ledger_label
ORDER BY transactions.report_date, transactions.ledger_label
"""


FEE_SUMMARY_STATEMENT = """
WITH transaction_totals AS (
    SELECT
        report_date,
        COUNT(*) FILTER (
            WHERE transaction_scenario = 'Digipos B2B Transfer'
              AND company_credit > 0
              AND NOT is_reversal
              AND NOT is_reversed
        ) * %(expected_fee_per_transaction)s AS expected_fee,
        COUNT(*) FILTER (
            WHERE transaction_scenario =
                'Digipos B2B Transfer In Cluster'
              AND NOT is_reversal
              AND NOT is_reversed
        ) * %(in_cluster_fee_per_transaction)s AS in_cluster_fee
    FROM linkaja_transactions_current_v
    WHERE cluster_id = %(cluster_id)s
      AND report_date = ANY(%(report_dates)s)
    GROUP BY report_date
), raw_totals AS (
    SELECT
        finalized_date AS report_date,
        COUNT(*) AS source_rows,
        STRING_AGG(DISTINCT source_file, ', ' ORDER BY source_file) AS source_file
    FROM linkaja_raw_transactions
    WHERE cluster_id = %(cluster_id)s
      AND finalized_date = ANY(%(report_dates)s)
    GROUP BY finalized_date
)
SELECT
    dates.report_date,
    %(cluster_id)s AS cluster_id,
    COALESCE(transaction_totals.expected_fee, 0) AS expected_fee,
    COALESCE(transaction_totals.in_cluster_fee, 0) AS in_cluster_fee,
    COALESCE(raw_totals.source_rows, 0) AS source_rows,
    COALESCE(raw_totals.source_file, '') AS source_file
FROM UNNEST(%(report_dates)s::DATE[]) AS dates(report_date)
LEFT JOIN transaction_totals
  ON transaction_totals.report_date = dates.report_date
LEFT JOIN raw_totals
  ON raw_totals.report_date = dates.report_date
ORDER BY dates.report_date
"""


CREATE_MONTHLY_STAGE_STATEMENT = """
CREATE TEMP TABLE linkaja_month_snapshot_stage
(LIKE linkaja_transactions
    INCLUDING DEFAULTS
    INCLUDING GENERATED
    INCLUDING CONSTRAINTS)
ON COMMIT DROP
"""


INSERT_MONTHLY_STAGE_STATEMENT = """
WITH target_base AS (
    SELECT base.*
    FROM linkaja_transaction_base_v AS base
    WHERE base.cluster_id = %(cluster_id)s
      AND base.report_date >= %(month_start)s
      AND base.report_date < %(month_end)s
      AND base.finalized_at_local < %(calculation_cutoff)s
)
INSERT INTO linkaja_month_snapshot_stage (
    cluster_id,
    transaction_id,
    report_date,
    finalized_at_local,
    original_transaction_id,
    partner_reference_number,
    invoice_id,
    transaction_type,
    transaction_scenario,
    transaction_status,
    counter_party,
    source_ledger_row_count,
    ledger_debit_total,
    ledger_credit_total,
    source_fee,
    source_fee_value_count,
    company_organization,
    company_account,
    company_debit,
    company_credit,
    company_balance,
    transfer_scope,
    original_transaction_scenario,
    original_is_resolved,
    reversal_count,
    reversal_transaction_ids,
    first_reversal_at_local,
    latest_reversal_at_local,
    source_files,
    updated_load_id,
    updated_at,
    report_month,
    calculation_cutoff,
    materialization_id
)
SELECT
    base.cluster_id,
    base.transaction_id,
    base.report_date,
    base.finalized_at_local,
    base.original_transaction_id,
    base.partner_reference_number,
    base.invoice_id,
    base.transaction_type,
    base.transaction_scenario,
    base.transaction_status,
    base.counter_party,
    base.source_ledger_row_count,
    base.ledger_debit_total,
    base.ledger_credit_total,
    base.source_fee,
    base.source_fee_value_count,
    base.company_organization,
    base.company_account,
    base.company_debit,
    base.company_credit,
    base.company_balance,
    CASE
        WHEN base.transaction_scenario = 'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN base.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario =
                'Digipos B2B Transfer In Cluster'
            THEN 'IN_CLUSTER'
        WHEN original_info.scenario_count = 1
         AND original_info.transaction_scenario = 'Digipos B2B Transfer'
            THEN 'OUT_CLUSTER'
        ELSE 'NOT_APPLICABLE'
    END,
    CASE
        WHEN original_info.scenario_count = 1
            THEN original_info.transaction_scenario
        ELSE NULL
    END,
    COALESCE(original_info.scenario_count = 1, FALSE),
    COALESCE(reversal_info.reversal_count, 0),
    COALESCE(
        reversal_info.reversal_transaction_ids,
        ARRAY[]::TEXT[]
    ),
    reversal_info.first_reversal_at_local,
    reversal_info.latest_reversal_at_local,
    base.source_files,
    base.updated_load_id,
    base.updated_at,
    %(month_start)s,
    %(calculation_cutoff)s,
    %(materialization_id)s
FROM target_base AS base
LEFT JOIN LATERAL (
    SELECT
        COUNT(DISTINCT original.transaction_scenario) AS scenario_count,
        MAX(original.transaction_scenario) AS transaction_scenario
    FROM linkaja_raw_transactions AS original
    WHERE original.cluster_id = base.cluster_id
      AND original.transaction_id = base.original_transaction_id
      AND LOWER(BTRIM(COALESCE(original.transaction_status, ''))) =
            'completed'
      AND original.finalized_date + original.finalized_time <
            %(calculation_cutoff)s
) AS original_info
  ON base.original_transaction_id IS NOT NULL
LEFT JOIN LATERAL (
    SELECT
        COUNT(DISTINCT reversal.transaction_id)::INTEGER AS reversal_count,
        ARRAY_AGG(
            DISTINCT reversal.transaction_id
            ORDER BY reversal.transaction_id
        ) AS reversal_transaction_ids,
        MIN(reversal.finalized_date + reversal.finalized_time)
            AS first_reversal_at_local,
        MAX(reversal.finalized_date + reversal.finalized_time)
            AS latest_reversal_at_local
    FROM linkaja_raw_transactions AS reversal
    WHERE reversal.cluster_id = base.cluster_id
      AND reversal.original_transaction_id = base.transaction_id
      AND LOWER(BTRIM(COALESCE(reversal.transaction_status, ''))) =
            'completed'
      AND reversal.finalized_date + reversal.finalized_time <
            %(calculation_cutoff)s
) AS reversal_info
  ON TRUE
"""


MONTHLY_DAILY_RAW_STATEMENT = """
WITH classified AS (
    SELECT
        transaction.*,
        CASE
            WHEN NOT transaction.is_reversal
                THEN transaction.transaction_scenario
            WHEN transaction.reversal_resolution_status = 'COMPLETE'
                THEN 'Complete Reversal - '
                    || transaction.original_transaction_scenario
            ELSE 'Unusual - Unresolved Reversal'
        END AS ledger_label
    FROM linkaja_month_snapshot_stage AS transaction
)
SELECT
    transaction.report_date,
    transaction.ledger_label,
    COUNT(*) AS transaction_count,
    SUM(transaction.source_ledger_row_count) AS source_ledger_rows,
    COALESCE(SUM(transaction.ledger_debit_total), 0) AS ledger_debit,
    COALESCE(SUM(transaction.ledger_credit_total), 0) AS ledger_credit,
    COALESCE(SUM(transaction.signed_amount), 0) AS signed_amount,
    COALESCE(SUM(transaction.company_debit), 0) AS company_debit,
    COALESCE(SUM(transaction.company_credit), 0) AS company_credit,
    COALESCE(SUM(transaction.source_fee), 0) AS source_fee,
    COUNT(*) FILTER (
        WHERE transaction.transaction_scenario =
                'General to Purchase B2B Transfer Agent Telco'
          AND transaction.source_fee IS NULL
    ) AS ppob_source_fee_missing_count,
    COUNT(*) FILTER (
        WHERE NOT transaction.is_reversal
          AND transaction.is_reversed
    ) AS reversed_original_transaction_count,
    COUNT(*) FILTER (
        WHERE transaction.reversal_resolution_status = 'COMPLETE'
    ) AS complete_reversal_count,
    COUNT(*) FILTER (
        WHERE transaction.reversal_resolution_status = 'UNRESOLVED'
    ) AS unresolved_reversal_count
FROM classified AS transaction
GROUP BY transaction.report_date, transaction.ledger_label
ORDER BY transaction.report_date, transaction.ledger_label
"""


MONTHLY_UNRESOLVED_REVERSALS_STATEMENT = """
SELECT
    reversal.report_date,
    reversal.finalized_at_local,
    reversal.transaction_id AS reversal_transaction_id,
    reversal.original_transaction_id,
    reversal.transaction_scenario AS reversal_scenario,
    reversal.source_ledger_row_count,
    reversal.ledger_debit_total,
    reversal.ledger_credit_total,
    reversal.ledger_debit_total - reversal.ledger_credit_total
        AS signed_amount,
    reversal.company_debit,
    reversal.company_credit,
    reversal.source_fee,
    reversal.company_balance,
    CASE
        WHEN reversal.company_balance IS NULL THEN NULL
        ELSE reversal.company_balance
            + reversal.company_debit
            - reversal.company_credit
    END AS balance_before,
    reversal.source_files,
    reversal.updated_load_id,
    edge.resolution_status AS unusual_reason_code
FROM linkaja_transaction_base_v AS reversal
JOIN linkaja_reversal_edges_current_v AS edge
  ON edge.cluster_id = reversal.cluster_id
 AND edge.reversal_transaction_id = reversal.transaction_id
WHERE reversal.cluster_id = %(cluster_id)s
  AND edge.resolution_status <> 'COMPLETE'
  AND reversal.finalized_at_local >= %(month_start)s
  AND reversal.finalized_at_local < %(calculation_cutoff)s
ORDER BY reversal.finalized_at_local, reversal.transaction_id
"""


MONTHLY_FEE_SUMMARY_STATEMENT = """
WITH fee_categories AS (
    SELECT fee_category
    FROM (VALUES
        ('Digipos B2B Transfer Fee'::TEXT),
        ('General to Purchase B2B Transfer Agent Telco'::TEXT)
    ) AS categories(fee_category)
), monthly_targets AS (
    SELECT
        transaction.report_month AS fee_month,
        transaction.cluster_id,
        transaction.transaction_scenario AS fee_category,
        transaction.is_reversed,
        transaction.source_fee AS fee_amount
    FROM linkaja_month_snapshot_stage AS transaction
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
        fee_month,
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
    GROUP BY fee_month, cluster_id, fee_category
)
SELECT
    %(month_start)s::DATE AS fee_month,
    %(cluster_id)s::TEXT AS cluster_id,
    category.fee_category,
    COALESCE(aggregate.gross_transaction_count, 0) AS gross_transaction_count,
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
    COALESCE(aggregate.gross_missing_fee_count, 0) AS gross_missing_fee_count,
    COALESCE(aggregate.reversed_missing_fee_count, 0)
        AS reversed_missing_fee_count,
    COALESCE(aggregate.active_missing_fee_count, 0)
        AS active_missing_fee_count,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0
            THEN 'INCOMPLETE_MISSING_FEE'
        ELSE 'COMPLETE'
    END AS calculation_status
FROM fee_categories AS category
LEFT JOIN monthly_aggregates AS aggregate
  ON aggregate.fee_category = category.fee_category
ORDER BY category.fee_category
"""


DELETE_MONTH_SNAPSHOT_STATEMENT = """
DELETE FROM linkaja_transactions
WHERE cluster_id = %(cluster_id)s
  AND report_month = %(month_start)s
"""


PUBLISH_MONTH_SNAPSHOT_STATEMENT = """
INSERT INTO linkaja_transactions (
    cluster_id,
    transaction_id,
    report_date,
    finalized_at_local,
    original_transaction_id,
    partner_reference_number,
    invoice_id,
    transaction_type,
    transaction_scenario,
    transaction_status,
    counter_party,
    source_ledger_row_count,
    ledger_debit_total,
    ledger_credit_total,
    source_fee,
    source_fee_value_count,
    company_organization,
    company_account,
    company_debit,
    company_credit,
    company_balance,
    transfer_scope,
    original_transaction_scenario,
    original_is_resolved,
    reversal_count,
    reversal_transaction_ids,
    first_reversal_at_local,
    latest_reversal_at_local,
    source_files,
    updated_load_id,
    updated_at,
    report_month,
    calculation_cutoff,
    materialization_id,
    materialized_at
)
SELECT
    cluster_id,
    transaction_id,
    report_date,
    finalized_at_local,
    original_transaction_id,
    partner_reference_number,
    invoice_id,
    transaction_type,
    transaction_scenario,
    transaction_status,
    counter_party,
    source_ledger_row_count,
    ledger_debit_total,
    ledger_credit_total,
    source_fee,
    source_fee_value_count,
    company_organization,
    company_account,
    company_debit,
    company_credit,
    company_balance,
    transfer_scope,
    original_transaction_scenario,
    original_is_resolved,
    reversal_count,
    reversal_transaction_ids,
    first_reversal_at_local,
    latest_reversal_at_local,
    source_files,
    updated_load_id,
    updated_at,
    report_month,
    calculation_cutoff,
    materialization_id,
    materialized_at
FROM linkaja_month_snapshot_stage
"""


UPSERT_MONTHLY_REFRESH_STATEMENT = """
INSERT INTO linkaja_monthly_refreshes (
    materialization_id,
    cluster_id,
    report_month,
    calculation_cutoff,
    transaction_rows,
    unresolved_reversal_count
)
VALUES (
    %(materialization_id)s,
    %(cluster_id)s,
    %(month_start)s,
    %(calculation_cutoff)s,
    %(transaction_rows)s,
    %(unresolved_reversal_count)s
)
ON CONFLICT (materialization_id) DO UPDATE
SET cluster_id = EXCLUDED.cluster_id,
    report_month = EXCLUDED.report_month,
    calculation_cutoff = EXCLUDED.calculation_cutoff,
    transaction_rows = EXCLUDED.transaction_rows,
    unresolved_reversal_count = EXCLUDED.unresolved_reversal_count,
    refreshed_at = CURRENT_TIMESTAMP
"""
