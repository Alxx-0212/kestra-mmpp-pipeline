select
    report_month,
    cluster_id,
    transaction_id,
    count(*) as duplicate_count
from {{ ref('stg_linkaja_monthly_transactions') }}
group by report_month, cluster_id, transaction_id
having count(*) > 1
