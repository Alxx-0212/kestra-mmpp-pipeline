ALTER TABLE linkaja_transactions
ADD COLUMN reversal_resolution_status TEXT
    GENERATED ALWAYS AS (
        CASE
            WHEN original_transaction_id IS NULL THEN 'NOT_REVERSAL'
            WHEN original_is_resolved THEN 'COMPLETE'
            ELSE 'UNRESOLVED'
        END
    ) STORED,
ADD COLUMN is_unusual BOOLEAN
    GENERATED ALWAYS AS (
        original_transaction_id IS NOT NULL
        AND NOT original_is_resolved
    ) STORED,
ADD COLUMN unusual_reason_codes TEXT[]
    GENERATED ALWAYS AS (
        CASE
            WHEN original_transaction_id IS NOT NULL
             AND NOT original_is_resolved
                THEN ARRAY['UNRESOLVED_REVERSAL']::TEXT[]
            ELSE ARRAY[]::TEXT[]
        END
    ) STORED;

ALTER TABLE linkaja_transactions
ADD CONSTRAINT linkaja_transactions_reversal_resolution_status_check
    CHECK (
        reversal_resolution_status
            IN ('NOT_REVERSAL', 'COMPLETE', 'UNRESOLVED')
    ),
ADD CONSTRAINT linkaja_transactions_unusual_consistency_check
    CHECK (
        is_unusual = (CARDINALITY(unusual_reason_codes) > 0)
        AND (
            NOT is_unusual
            OR unusual_reason_codes = ARRAY['UNRESOLVED_REVERSAL']::TEXT[]
        )
    );

CREATE INDEX linkaja_transactions_unusual_cluster_date_idx
ON linkaja_transactions (cluster_id, report_date)
WHERE is_unusual;
