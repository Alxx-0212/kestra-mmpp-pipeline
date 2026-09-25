/*
   Source-load currentness is exact-file scoped. linkaja_raw_transactions stays
   the current state; linkaja_ledger_versions is append-only audit evidence.
   Do not rewrite the multi-million-row ledger table during a daily migration.
*/

WITH ranked_loads AS (
    SELECT
        load_id,
        ROW_NUMBER() OVER (
            PARTITION BY cluster_id, source_file, source_start_date, source_end_date
            ORDER BY ingested_at DESC, load_id DESC
        ) AS generation_rank,
        LAG(load_id) OVER (
            PARTITION BY cluster_id, source_file, source_start_date, source_end_date
            ORDER BY ingested_at ASC, load_id ASC
        ) AS previous_generation_id,
        LEAD(ingested_at) OVER (
            PARTITION BY cluster_id, source_file, source_start_date, source_end_date
            ORDER BY ingested_at ASC, load_id ASC
        ) AS next_generation_at
    FROM linkaja_source_loads
)
UPDATE linkaja_source_loads AS loads
SET is_current = ranked.generation_rank = 1,
    valid_to = CASE
        WHEN ranked.generation_rank = 1 THEN NULL
        ELSE COALESCE(loads.valid_to, ranked.next_generation_at)
    END,
    supersedes_load_id = ranked.previous_generation_id
FROM ranked_loads AS ranked
WHERE ranked.load_id = loads.load_id;

CREATE UNIQUE INDEX linkaja_source_loads_current_identity_idx
ON linkaja_source_loads (
    cluster_id, source_file, source_start_date, source_end_date
)
WHERE is_current;
