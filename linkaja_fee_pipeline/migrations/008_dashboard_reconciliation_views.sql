/*
Add read-only reporting surfaces for the LinkAja Taipy dashboard.

Daily fee reconciliation uses the current reversal state. Mandiri settlement
cycles use gross Purchase Account movements on their actual timestamps so an
original and its later reversal are never applied twice in the same balance
equation.
*/

CREATE VIEW linkaja_daily_fee_reconciliation_v AS
WITH fee_types AS (
    SELECT fee_type
    FROM (VALUES
        ('EXPECTED_OUT_CLUSTER_RP200'::TEXT),
        ('IN_CLUSTER_RP20'::TEXT),
        ('POSTED_DIGIPOS_FEE'::TEXT),
        ('PPOB_AGENT_TELCO_FEE'::TEXT)
    ) AS configured(fee_type)
), targets AS (
    SELECT
        transaction.cluster_id,
        transaction.report_date,
        transaction.transaction_id,
        transaction.is_reversed,
        transaction.source_fee,
        transaction.source_files,
        transaction.updated_load_id,
        transaction.updated_at,
        CASE
            WHEN transaction.transaction_scenario = 'Digipos B2B Transfer'
                THEN 'EXPECTED_OUT_CLUSTER_RP200'
            WHEN transaction.transaction_scenario =
                    'Digipos B2B Transfer In Cluster'
                THEN 'IN_CLUSTER_RP20'
            WHEN transaction.transaction_scenario =
                    'Digipos B2B Transfer Fee'
                THEN 'POSTED_DIGIPOS_FEE'
            ELSE 'PPOB_AGENT_TELCO_FEE'
        END AS fee_type,
        CASE
            WHEN transaction.transaction_scenario = 'Digipos B2B Transfer'
                THEN transaction.company_credit > 0
            WHEN transaction.transaction_scenario =
                    'Digipos B2B Transfer Fee'
                THEN transaction.source_fee = 200::NUMERIC
            ELSE TRUE
        END AS is_eligible,
        CASE
            WHEN transaction.transaction_scenario = 'Digipos B2B Transfer'
                THEN 200::NUMERIC
            WHEN transaction.transaction_scenario =
                    'Digipos B2B Transfer In Cluster'
                THEN 20::NUMERIC
            WHEN transaction.transaction_scenario =
                    'Digipos B2B Transfer Fee'
                THEN CASE
                    WHEN transaction.source_fee = 200::NUMERIC
                        THEN 200::NUMERIC
                    ELSE NULL
                END
            ELSE transaction.source_fee
        END AS fee_amount,
        (
            transaction.transaction_scenario IN (
                'Digipos B2B Transfer Fee',
                'General to Purchase B2B Transfer Agent Telco'
            )
            AND transaction.source_fee IS NULL
        ) AS is_fee_missing,
        (
            transaction.transaction_scenario = 'Digipos B2B Transfer Fee'
            AND transaction.source_fee IS NOT NULL
            AND transaction.source_fee <> 200::NUMERIC
        ) AS is_fee_invalid
    FROM linkaja_transactions_current_v AS transaction
    WHERE NOT transaction.is_reversal
      AND transaction.transaction_scenario IN (
          'Digipos B2B Transfer',
          'Digipos B2B Transfer In Cluster',
          'Digipos B2B Transfer Fee',
          'General to Purchase B2B Transfer Agent Telco'
      )
), target_aggregates AS (
    SELECT
        cluster_id,
        report_date,
        fee_type,
        COUNT(*) FILTER (WHERE is_eligible)::BIGINT
            AS gross_transaction_count,
        COUNT(*) FILTER (WHERE is_eligible AND is_reversed)::BIGINT
            AS reversed_transaction_count,
        COUNT(*) FILTER (WHERE is_eligible AND NOT is_reversed)::BIGINT
            AS active_transaction_count,
        SUM(fee_amount) FILTER (WHERE is_eligible) AS calculated_gross_fee,
        SUM(fee_amount) FILTER (WHERE is_eligible AND is_reversed)
            AS calculated_reversed_fee,
        SUM(fee_amount) FILTER (WHERE is_eligible AND NOT is_reversed)
            AS calculated_active_fee,
        COUNT(*) FILTER (WHERE is_fee_missing)::BIGINT
            AS missing_fee_count,
        COUNT(*) FILTER (WHERE is_fee_missing AND NOT is_reversed)::BIGINT
            AS active_missing_fee_count,
        COUNT(*) FILTER (WHERE is_fee_invalid)::BIGINT
            AS invalid_fee_count,
        COUNT(*) FILTER (WHERE is_fee_invalid AND NOT is_reversed)::BIGINT
            AS active_invalid_fee_count
    FROM targets
    GROUP BY cluster_id, report_date, fee_type
), resolved_events AS (
    SELECT
        reversal.cluster_id,
        reversal.report_date,
        CASE
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer'
                THEN 'EXPECTED_OUT_CLUSTER_RP200'
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer In Cluster'
                THEN 'IN_CLUSTER_RP20'
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer Fee'
                THEN 'POSTED_DIGIPOS_FEE'
            WHEN reversal.original_transaction_scenario =
                    'General to Purchase B2B Transfer Agent Telco'
                THEN 'PPOB_AGENT_TELCO_FEE'
            ELSE NULL
        END AS fee_type,
        COUNT(*)::BIGINT AS resolved_reversal_event_count
    FROM linkaja_transactions_current_v AS reversal
    WHERE reversal.is_reversal
      AND reversal.reversal_resolution_status = 'COMPLETE'
    GROUP BY
        reversal.cluster_id,
        reversal.report_date,
        CASE
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer'
                THEN 'EXPECTED_OUT_CLUSTER_RP200'
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer In Cluster'
                THEN 'IN_CLUSTER_RP20'
            WHEN reversal.original_transaction_scenario =
                    'Digipos B2B Transfer Fee'
                THEN 'POSTED_DIGIPOS_FEE'
            WHEN reversal.original_transaction_scenario =
                    'General to Purchase B2B Transfer Agent Telco'
                THEN 'PPOB_AGENT_TELCO_FEE'
            ELSE NULL
        END
), unresolved_events AS (
    SELECT
        reversal.cluster_id,
        reversal.report_date,
        COUNT(*)::BIGINT AS unresolved_reversal_count
    FROM linkaja_transactions_current_v AS reversal
    WHERE reversal.reversal_resolution_status = 'UNRESOLVED'
    GROUP BY reversal.cluster_id, reversal.report_date
), activity_dates AS (
    SELECT cluster_id, report_date FROM targets
    UNION
    SELECT cluster_id, report_date
    FROM linkaja_transactions_current_v
    WHERE is_reversal
), daily_provenance AS (
    SELECT
        transaction.cluster_id,
        transaction.report_date,
        STRING_AGG(
            DISTINCT transaction.source_files,
            ', '
            ORDER BY transaction.source_files
        ) FILTER (
            WHERE transaction.source_files IS NOT NULL
              AND transaction.source_files <> ''
        ) AS source_files,
        (
            ARRAY_AGG(
                transaction.updated_load_id
                ORDER BY transaction.updated_at DESC NULLS LAST,
                         transaction.transaction_id DESC
            ) FILTER (WHERE transaction.updated_load_id IS NOT NULL)
        )[1] AS latest_load_id,
        MAX(transaction.updated_at) AS latest_updated_at
    FROM linkaja_transactions_current_v AS transaction
    GROUP BY transaction.cluster_id, transaction.report_date
)
SELECT
    date.cluster_id,
    date.report_date,
    type.fee_type,
    COALESCE(aggregate.gross_transaction_count, 0)
        AS gross_transaction_count,
    COALESCE(aggregate.reversed_transaction_count, 0)
        AS reversed_transaction_count,
    COALESCE(aggregate.active_transaction_count, 0)
        AS active_transaction_count,
    CASE
        WHEN COALESCE(aggregate.missing_fee_count, 0) > 0 THEN NULL
        WHEN COALESCE(aggregate.invalid_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_gross_fee, 0)
    END AS gross_fee,
    CASE
        WHEN COALESCE(
            aggregate.missing_fee_count
                - aggregate.active_missing_fee_count,
            0
        ) > 0 THEN NULL
        WHEN COALESCE(
            aggregate.invalid_fee_count
                - aggregate.active_invalid_fee_count,
            0
        ) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_reversed_fee, 0)
    END AS reversed_fee,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0 THEN NULL
        WHEN COALESCE(aggregate.active_invalid_fee_count, 0) > 0 THEN NULL
        ELSE COALESCE(aggregate.calculated_active_fee, 0)
    END AS active_fee,
    COALESCE(aggregate.missing_fee_count, 0) AS missing_fee_count,
    COALESCE(aggregate.active_missing_fee_count, 0)
        AS active_missing_fee_count,
    COALESCE(aggregate.invalid_fee_count, 0) AS invalid_fee_count,
    COALESCE(event.resolved_reversal_event_count, 0)
        AS resolved_reversal_event_count,
    COALESCE(unresolved.unresolved_reversal_count, 0)
        AS unresolved_reversal_count,
    CASE
        WHEN COALESCE(aggregate.active_missing_fee_count, 0) > 0
            THEN 'INCOMPLETE_MISSING_FEE'
        WHEN COALESCE(aggregate.active_invalid_fee_count, 0) > 0
            THEN 'INCOMPLETE_INVALID_FEE'
        ELSE 'COMPLETE'
    END AS calculation_status,
    provenance.source_files,
    provenance.latest_load_id,
    provenance.latest_updated_at
FROM activity_dates AS date
CROSS JOIN fee_types AS type
LEFT JOIN target_aggregates AS aggregate
  ON aggregate.cluster_id = date.cluster_id
 AND aggregate.report_date = date.report_date
 AND aggregate.fee_type = type.fee_type
LEFT JOIN resolved_events AS event
  ON event.cluster_id = date.cluster_id
 AND event.report_date = date.report_date
 AND event.fee_type = type.fee_type
LEFT JOIN unresolved_events AS unresolved
  ON unresolved.cluster_id = date.cluster_id
 AND unresolved.report_date = date.report_date
LEFT JOIN daily_provenance AS provenance
  ON provenance.cluster_id = date.cluster_id
 AND provenance.report_date = date.report_date;

CREATE VIEW linkaja_withdrawal_events_v AS
SELECT
    transaction.cluster_id,
    transaction.transaction_id AS withdrawal_transaction_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.company_organization,
    transaction.company_account,
    transaction.company_debit AS withdrawal_amount,
    transaction.balance_before,
    transaction.company_balance AS closing_balance,
    transaction.source_ledger_row_count,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND transaction.transaction_scenario =
        'Organization Withdraw of Funds with Next Working Day Payment'
  AND transaction.company_account IS NOT NULL
  AND transaction.company_debit > 0;

CREATE VIEW linkaja_mandiri_settlement_cycles_v AS
WITH current_transactions AS (
    SELECT
        transaction.*,
        (
            NOT transaction.is_reversal
            AND transaction.transaction_scenario =
                'Organization Withdraw of Funds with Next Working Day Payment'
            AND transaction.company_debit > 0
        ) AS is_withdrawal_event
    FROM linkaja_transactions_current_v AS transaction
    WHERE transaction.company_account IS NOT NULL
), sequenced_transactions AS MATERIALIZED (
    SELECT
        transaction.*,
        1 + COUNT(*) FILTER (
            WHERE transaction.is_withdrawal_event
        ) OVER (
            PARTITION BY transaction.cluster_id, transaction.company_account
            ORDER BY
                transaction.finalized_at_local,
                CASE WHEN transaction.is_withdrawal_event THEN 1 ELSE 0 END,
                transaction.transaction_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS settlement_cycle_number
    FROM current_transactions AS transaction
), ordered_withdrawals AS MATERIALIZED (
    SELECT
        withdrawal.*,
        LAG(withdrawal.transaction_id) OVER account_order
            AS previous_withdrawal_transaction_id,
        LAG(withdrawal.finalized_at_local) OVER account_order
            AS interval_start_exclusive,
        LAG(withdrawal.company_balance) OVER account_order
            AS previous_closing_balance
    FROM sequenced_transactions AS withdrawal
    WHERE withdrawal.is_withdrawal_event
    WINDOW account_order AS (
        PARTITION BY withdrawal.cluster_id, withdrawal.company_account
        ORDER BY withdrawal.settlement_cycle_number
    )
), cycle_movements AS MATERIALIZED (
    SELECT
        transaction.cluster_id,
        transaction.company_account,
        transaction.settlement_cycle_number,
        COALESCE(SUM(transaction.company_credit) FILTER (
            WHERE NOT transaction.is_withdrawal_event
        ), 0) AS non_withdrawal_company_credit,
        COALESCE(SUM(transaction.company_debit) FILTER (
            WHERE NOT transaction.is_withdrawal_event
        ), 0) AS non_withdrawal_company_debit,
        COALESCE(SUM(transaction.company_credit), 0)
            AS total_company_credit,
        COALESCE(SUM(transaction.company_debit), 0)
            AS total_company_debit,
        COUNT(*) FILTER (
            WHERE transaction.is_withdrawal_event
        )::BIGINT AS withdrawal_transaction_count,
        COUNT(*)::BIGINT AS source_transaction_count,
        COALESCE(SUM(transaction.source_ledger_row_count), 0)::BIGINT
            AS source_ledger_row_count,
        COUNT(*) FILTER (
            WHERE transaction.reversal_resolution_status = 'COMPLETE'
        )::BIGINT AS resolved_reversal_count,
        COUNT(*) FILTER (
            WHERE transaction.reversal_resolution_status = 'UNRESOLVED'
        )::BIGINT AS unresolved_reversal_count,
        COUNT(*) FILTER (
            WHERE NOT transaction.is_reversal
              AND NOT transaction.is_reversed
              AND transaction.transaction_scenario =
                'General to Purchase B2B Transfer Agent Telco'
              AND transaction.source_fee IS NULL
        )::BIGINT AS source_fee_missing_count,
        COUNT(*) FILTER (
            WHERE NOT transaction.is_reversal
              AND transaction.reversal_count > 1
        )::BIGINT AS multiple_reversal_count,
        (
            ARRAY_AGG(
                transaction.balance_before
                ORDER BY transaction.finalized_at_local,
                         transaction.transaction_id
            ) FILTER (WHERE transaction.balance_before IS NOT NULL)
        )[1] AS first_balance_before,
        (
            ARRAY_AGG(
                transaction.company_balance
                ORDER BY transaction.finalized_at_local DESC,
                         transaction.transaction_id DESC
            ) FILTER (WHERE transaction.company_balance IS NOT NULL)
        )[1] AS latest_company_balance,
        STRING_AGG(
            DISTINCT transaction.source_files,
            ', '
            ORDER BY transaction.source_files
        ) FILTER (
            WHERE transaction.source_files IS NOT NULL
              AND transaction.source_files <> ''
        ) AS source_files,
        (
            ARRAY_AGG(
                transaction.updated_load_id
                ORDER BY transaction.updated_at DESC NULLS LAST,
                         transaction.transaction_id DESC
            ) FILTER (WHERE transaction.updated_load_id IS NOT NULL)
        )[1] AS latest_load_id,
        MAX(transaction.updated_at) AS latest_updated_at
    FROM sequenced_transactions AS transaction
    GROUP BY
        transaction.cluster_id,
        transaction.company_account,
        transaction.settlement_cycle_number
), closed_cycles AS (
    SELECT
        withdrawal.cluster_id,
        withdrawal.company_account,
        CONCAT(
            withdrawal.cluster_id,
            '|',
            withdrawal.company_account,
            '|',
            withdrawal.transaction_id
        ) AS settlement_cycle_id,
        withdrawal.previous_withdrawal_transaction_id,
        withdrawal.transaction_id AS withdrawal_transaction_id,
        withdrawal.interval_start_exclusive,
        withdrawal.finalized_at_local AS interval_end_inclusive,
        COALESCE(
            withdrawal.previous_closing_balance,
            movement.first_balance_before
        ) AS opening_balance,
        movement.non_withdrawal_company_credit,
        movement.non_withdrawal_company_debit,
        withdrawal.company_debit AS withdrawal_company_debit,
        withdrawal.company_balance AS closing_balance,
        CASE
            WHEN COALESCE(
                withdrawal.previous_closing_balance,
                movement.first_balance_before
            ) IS NULL THEN NULL
            ELSE COALESCE(
                withdrawal.previous_closing_balance,
                movement.first_balance_before
            )
                + movement.total_company_credit
                - movement.total_company_debit
        END AS calculated_closing_balance,
        CASE
            WHEN withdrawal.company_balance IS NULL
              OR COALESCE(
                    withdrawal.previous_closing_balance,
                    movement.first_balance_before
                 ) IS NULL
                THEN NULL
            ELSE withdrawal.company_balance
                - (
                    COALESCE(
                        withdrawal.previous_closing_balance,
                        movement.first_balance_before
                    )
                    + movement.total_company_credit
                    - movement.total_company_debit
                )
        END AS balance_variance,
        movement.withdrawal_transaction_count,
        movement.source_transaction_count,
        movement.source_ledger_row_count,
        movement.resolved_reversal_count,
        movement.unresolved_reversal_count,
        movement.source_fee_missing_count,
        movement.source_files,
        movement.latest_load_id,
        movement.latest_updated_at,
        CASE
            WHEN withdrawal.interval_start_exclusive IS NULL
                THEN 'PARTIAL_OPENING_HISTORY'
            ELSE 'COMPLETE'
        END AS data_status,
        CASE
            WHEN movement.unresolved_reversal_count > 0
                THEN 'UNRESOLVED_REVERSAL'
            WHEN movement.multiple_reversal_count > 0
                THEN 'MULTIPLE_REVERSALS'
            ELSE 'CLEAR'
        END AS reversal_status,
        CASE
            WHEN withdrawal.interval_start_exclusive IS NULL
                THEN 'NOT_EVALUATED'
            WHEN withdrawal.company_balance IS NULL
              OR withdrawal.previous_closing_balance IS NULL
                THEN 'NOT_EVALUATED'
            WHEN withdrawal.company_balance
                    - (
                        withdrawal.previous_closing_balance
                        + movement.total_company_credit
                        - movement.total_company_debit
                    ) = 0::NUMERIC
                THEN 'BALANCED'
            ELSE 'BALANCE_MISMATCH'
        END AS linkaja_reconciliation_status,
        'PENDING_BANK_EVIDENCE'::TEXT AS mandiri_confirmation_status
    FROM ordered_withdrawals AS withdrawal
    JOIN cycle_movements AS movement
      ON movement.cluster_id = withdrawal.cluster_id
     AND movement.company_account = withdrawal.company_account
     AND movement.settlement_cycle_number =
            withdrawal.settlement_cycle_number
), accounts AS (
    SELECT
        transaction.cluster_id,
        transaction.company_account,
        MAX(transaction.finalized_at_local) AS latest_transaction_at,
        COUNT(*) FILTER (
            WHERE transaction.is_withdrawal_event
        ) + 1 AS open_cycle_number
    FROM sequenced_transactions AS transaction
    GROUP BY transaction.cluster_id, transaction.company_account
), last_withdrawals AS (
    SELECT DISTINCT ON (cluster_id, company_account)
        cluster_id,
        company_account,
        transaction_id AS withdrawal_transaction_id,
        finalized_at_local,
        company_balance AS closing_balance
    FROM ordered_withdrawals
    ORDER BY
        cluster_id,
        company_account,
        finalized_at_local DESC,
        transaction_id DESC
), open_cycles AS (
    SELECT
        account.cluster_id,
        account.company_account,
        CONCAT(
            account.cluster_id,
            '|',
            account.company_account,
            '|OPEN'
        ) AS settlement_cycle_id,
        last_withdrawal.withdrawal_transaction_id
            AS previous_withdrawal_transaction_id,
        NULL::TEXT AS withdrawal_transaction_id,
        last_withdrawal.finalized_at_local AS interval_start_exclusive,
        account.latest_transaction_at AS interval_end_inclusive,
        COALESCE(
            last_withdrawal.closing_balance,
            movement.first_balance_before
        ) AS opening_balance,
        COALESCE(movement.non_withdrawal_company_credit, 0)
            AS non_withdrawal_company_credit,
        COALESCE(movement.non_withdrawal_company_debit, 0)
            AS non_withdrawal_company_debit,
        0::NUMERIC AS withdrawal_company_debit,
        movement.latest_company_balance AS closing_balance,
        CASE
            WHEN COALESCE(
                last_withdrawal.closing_balance,
                movement.first_balance_before
            ) IS NULL THEN NULL
            ELSE COALESCE(
                last_withdrawal.closing_balance,
                movement.first_balance_before
            )
                + COALESCE(movement.total_company_credit, 0)
                - COALESCE(movement.total_company_debit, 0)
        END AS calculated_closing_balance,
        CASE
            WHEN movement.latest_company_balance IS NULL
              OR COALESCE(
                    last_withdrawal.closing_balance,
                    movement.first_balance_before
                 ) IS NULL
                THEN NULL
            ELSE movement.latest_company_balance
                - (
                    COALESCE(
                        last_withdrawal.closing_balance,
                        movement.first_balance_before
                    )
                    + COALESCE(movement.total_company_credit, 0)
                    - COALESCE(movement.total_company_debit, 0)
                )
        END AS balance_variance,
        0::BIGINT AS withdrawal_transaction_count,
        COALESCE(movement.source_transaction_count, 0)::BIGINT
            AS source_transaction_count,
        COALESCE(movement.source_ledger_row_count, 0)::BIGINT
            AS source_ledger_row_count,
        COALESCE(movement.resolved_reversal_count, 0)::BIGINT
            AS resolved_reversal_count,
        COALESCE(movement.unresolved_reversal_count, 0)::BIGINT
            AS unresolved_reversal_count,
        COALESCE(movement.source_fee_missing_count, 0)::BIGINT
            AS source_fee_missing_count,
        movement.source_files,
        movement.latest_load_id,
        movement.latest_updated_at,
        CASE
            WHEN last_withdrawal.withdrawal_transaction_id IS NULL
                THEN 'PARTIAL_OPENING_HISTORY'
            ELSE 'COMPLETE'
        END AS data_status,
        CASE
            WHEN COALESCE(movement.unresolved_reversal_count, 0) > 0
                THEN 'UNRESOLVED_REVERSAL'
            WHEN COALESCE(movement.multiple_reversal_count, 0) > 0
                THEN 'MULTIPLE_REVERSALS'
            ELSE 'CLEAR'
        END AS reversal_status,
        'PENDING_WITHDRAWAL'::TEXT AS linkaja_reconciliation_status,
        'PENDING_BANK_EVIDENCE'::TEXT AS mandiri_confirmation_status
    FROM accounts AS account
    LEFT JOIN last_withdrawals AS last_withdrawal
      ON last_withdrawal.cluster_id = account.cluster_id
     AND last_withdrawal.company_account = account.company_account
    LEFT JOIN cycle_movements AS movement
      ON movement.cluster_id = account.cluster_id
     AND movement.company_account = account.company_account
     AND movement.settlement_cycle_number = account.open_cycle_number
)
SELECT * FROM closed_cycles
UNION ALL
SELECT * FROM open_cycles;

CREATE VIEW linkaja_reconciliation_exceptions_v AS
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'UNRESOLVED_REVERSAL'::TEXT AS exception_code,
    'BLOCKING'::TEXT AS severity,
    CONCAT(
        'Original transaction ',
        COALESCE(transaction.original_transaction_id, '<missing>'),
        ' is not available as one completed same-cluster transaction.'
    ) AS detail,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE transaction.reversal_resolution_status = 'UNRESOLVED'
UNION ALL
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'MULTIPLE_REVERSALS'::TEXT,
    'REVIEW'::TEXT,
    CONCAT('Original has ', transaction.reversal_count, ' reversal events.'),
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND transaction.reversal_count > 1
UNION ALL
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'INVALID_REVERSAL_CHRONOLOGY'::TEXT,
    'BLOCKING'::TEXT,
    'A linked reversal is finalized before its original transaction.'::TEXT,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND transaction.first_reversal_at_local < transaction.finalized_at_local
UNION ALL
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'ACTIVE_MISSING_PPOB_FEE'::TEXT,
    'BLOCKING'::TEXT,
    'Active PPOB/agent-telco transaction has no source Fee.'::TEXT,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND NOT transaction.is_reversed
  AND transaction.transaction_scenario =
        'General to Purchase B2B Transfer Agent Telco'
  AND transaction.source_fee IS NULL
UNION ALL
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'INVALID_DIGIPOS_FEE'::TEXT,
    'BLOCKING'::TEXT,
    CONCAT(
        'Posted Digipos fee source Fee is ',
        COALESCE(transaction.source_fee::TEXT, '<missing>'),
        '; expected exactly 200.'
    ),
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND NOT transaction.is_reversed
  AND transaction.transaction_scenario = 'Digipos B2B Transfer Fee'
  AND transaction.source_fee IS DISTINCT FROM 200::NUMERIC
UNION ALL
SELECT
    transaction.cluster_id,
    transaction.report_date,
    transaction.finalized_at_local,
    transaction.transaction_id,
    'WITHDRAWAL_ACCOUNT_MISSING'::TEXT,
    'BLOCKING'::TEXT,
    'Withdrawal has no identifiable company Purchase Account row.'::TEXT,
    transaction.source_files,
    transaction.updated_load_id,
    transaction.updated_at
FROM linkaja_transactions_current_v AS transaction
WHERE NOT transaction.is_reversal
  AND transaction.transaction_scenario =
        'Organization Withdraw of Funds with Next Working Day Payment'
  AND (
      transaction.company_account IS NULL
      OR transaction.company_debit <= 0
  );

CREATE VIEW linkaja_load_freshness_v AS
SELECT
    raw.cluster_id,
    raw.load_id,
    raw.source_file,
    COUNT(*)::BIGINT AS source_row_count,
    COUNT(DISTINCT raw.transaction_id)::BIGINT AS transaction_count,
    MIN(raw.finalized_date) AS min_report_date,
    MAX(raw.finalized_date) AS max_report_date,
    MIN(raw.ingested_at) AS ingestion_started_at,
    MAX(raw.ingested_at) AS ingestion_completed_at,
    'SUCCESS'::TEXT AS load_status
FROM linkaja_raw_transactions AS raw
GROUP BY raw.cluster_id, raw.load_id, raw.source_file;
