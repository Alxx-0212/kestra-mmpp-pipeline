CREATE OR REPLACE VIEW linkaja_transaction_base_v AS
WITH completed_raw AS (
    SELECT
        raw.*,
        (
            (
                raw.organization = raw.top_organization
                OR SUBSTRING(
                    BTRIM(raw.parent_organization)
                    FROM '^([0-9]+)[[:space:]]*-'
                ) = SUBSTRING(
                    BTRIM(raw.top_organization)
                    FROM '^([0-9]+)[[:space:]]*-'
                )
            )
            AND POSITION(
                'organization mfs purchase account' IN LOWER(raw.account)
            ) > 0
        ) AS is_company_purchase_row
    FROM linkaja_raw_transactions AS raw
    WHERE LOWER(BTRIM(COALESCE(raw.transaction_status, ''))) = 'completed'
)
SELECT
    raw.cluster_id,
    raw.transaction_id,
    raw.finalized_date AS report_date,
    MIN(raw.finalized_date + raw.finalized_time) AS finalized_at_local,
    MAX(NULLIF(BTRIM(raw.original_transaction_id), ''))
        AS original_transaction_id,
    MAX(NULLIF(BTRIM(raw.partner_reference_number), ''))
        AS partner_reference_number,
    MAX(NULLIF(BTRIM(raw.invoice_id), '')) AS invoice_id,
    MAX(raw.transaction_type) AS transaction_type,
    MAX(raw.transaction_scenario) AS transaction_scenario,
    MAX(raw.transaction_status) AS transaction_status,
    COALESCE(
        MAX(raw.counter_party) FILTER (WHERE raw.is_company_purchase_row),
        MAX(raw.counter_party)
    ) AS counter_party,
    COUNT(*)::INTEGER AS source_ledger_row_count,
    COALESCE(SUM(raw.debit), 0) AS ledger_debit_total,
    COALESCE(SUM(raw.credit), 0) AS ledger_credit_total,
    MAX(raw.fee) FILTER (WHERE raw.fee IS NOT NULL) AS source_fee,
    (
        COUNT(DISTINCT raw.fee) FILTER (WHERE raw.fee IS NOT NULL)
    )::INTEGER AS source_fee_value_count,
    MAX(raw.organization) FILTER (WHERE raw.is_company_purchase_row)
        AS company_organization,
    MAX(raw.account) FILTER (WHERE raw.is_company_purchase_row)
        AS company_account,
    COALESCE(
        SUM(raw.debit) FILTER (WHERE raw.is_company_purchase_row),
        0
    ) AS company_debit,
    COALESCE(
        SUM(raw.credit) FILTER (WHERE raw.is_company_purchase_row),
        0
    ) AS company_credit,
    MAX(raw.balance) FILTER (WHERE raw.is_company_purchase_row)
        AS company_balance,
    STRING_AGG(DISTINCT raw.source_file, ', ' ORDER BY raw.source_file)
        AS source_files,
    (
        ARRAY_AGG(
            raw.load_id
            ORDER BY raw.ingested_at DESC, raw.source_row_number DESC
        )
    )[1] AS updated_load_id,
    MAX(raw.ingested_at) AS updated_at
FROM completed_raw AS raw
GROUP BY raw.cluster_id, raw.transaction_id, raw.finalized_date;
