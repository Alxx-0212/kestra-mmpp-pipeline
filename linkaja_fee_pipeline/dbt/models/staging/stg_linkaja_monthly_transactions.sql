{{ config(tags=["monthly", "staging"]) }}

select
    report_month,
    calculation_cutoff,
    materialization_id,
    cluster_id,
    transaction_id,
    report_date,
    finalized_at_local,
    original_transaction_id,
    transaction_scenario,
    source_fee,
    source_fee_value_count,
    company_debit,
    company_credit,
    is_reversal,
    is_reversed,
    reversal_resolution_status,
    is_unusual,
    unusual_reason_codes,
    source_files
from {{ source('linkaja', 'monthly_transactions') }}
