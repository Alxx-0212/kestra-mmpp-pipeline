# DigiPOS Backfill Reliability Lessons

## Durable rules from June 2026 backfill work

1. Do not trust initial `0 entries` after Search.
   - DigiPOS sometimes shows `Showing 0 to 0 of 0 entries` on first search even when transactions exist.
   - Refresh/retry Search, then relogin/fresh page if still 0.
   - Business rule for this workflow: at least one transaction exists every day, so 0 rows is a web/export failure, not valid no-data.

2. Use table footer total as source of truth.
   - Read `#dataInput_info`, e.g. `Showing 1 to 10 of 3,111 entries`.
   - Download is valid only when CSV data row count equals footer total entries exactly.
   - Header-only CSV (139 B, 0 data rows) must be deleted and treated as failure.

3. Avoid stale file reuse.
   - Remove expected CSV path before each download attempt.
   - If search/export fails, ensure no stale file remains at the expected path.
   - Backfill must validate row count again before posting to Kestra.

4. Chronological Kestra posting must wait for completion, not creation.
   - A Kestra API `HTTP 200` / `state=CREATED` only means submitted.
   - Poll `GET /api/v1/executions/{execution_id}` until terminal state before posting next file.
   - Later executions can finish first if submitted concurrently; this breaks sheet order and balance formulas.

5. No skipped dates inside a cluster.
   - For each cluster, download every expected date first.
   - If any required date is missing, header-only, or failed, do not post later dates for that cluster.
   - Compare downloaded date list to expected date list before posting.

6. Recovery pattern.
   - If one cluster/date fails after previous dates posted, fix/download that exact date, then continue from that date forward for that cluster only.
   - If sheets were cleared, rerun full backfill from clean state with strict no-skip checks.

## Known observed flaky examples

- `421306_A / 2026-06-09`: first run produced header-only CSV and Kestra loaded `rows=0`; retry downloaded `3,111` rows.
- `421320_A / 2026-06-04`: first Search showed 0 entries; second Search showed `2,535` entries and valid CSV row count `2,535`.

## Commands

Strict single-date scraper verification:

```bash
python3 scripts/download_digipos.py \
  --users 421320_A \
  --date 2026-06-04 \
  --output-dir /tmp/digipos-check \
  --max-attempts 5
```

Strict backfill for one cluster/date range:

```bash
python3 scripts/finpay_backfill.py \
  --start 2026-06-04 \
  --end 2026-06-10 \
  --users 421320_A \
  --poll-seconds 10 \
  --timeout-seconds 1800
```
