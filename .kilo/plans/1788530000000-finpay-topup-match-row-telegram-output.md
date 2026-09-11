# FinPay Top-Up Match Row Output

## Source Of Truth

This plan implements the latest request:

- Keep the existing complete running-saldo reconciliation.
- Report the exact transaction row where the running saldo matches DigiPOS CMS BUCKET TOP UP.
- Use `transaction_date ASC` as the only transaction ordering rule. Do not add `txn_id` or another secondary sort key.
- Do not send all fetched rows and do not add pagination.
- Make Telegram output readable with standard HTML formatting, bold labels, separators, and code blocks. Telegram Premium is not required.
- Show the downloaded date range, raw downloaded row count, calculation row count, matching row number, exact transaction date/time, full matching source row, CMS snapshot time, running saldo, BUCKET, and difference.

Current repository/runtime facts:

- `verify.py` calculates the full saldo from the trusted checkpoint through either the CMS timestamp or the closed-day cutoff.
- The calculation source is committed ledger rows plus the current refresh staging rows.
- `saldo.py` already exposes source min/max timestamps, but it does not produce a per-row running-saldo trace.
- `refresh_cluster` already persists `source_row_count`, `source_transaction_min_at`, `source_transaction_max_at`, `bucket_snapshot_at`, `calculation_cutoff_at`, and `calculated_at`.
- `notify.py` currently sends plain text without a Telegram `parse_mode`.
- The deployed Kestra flow already passes verification JSON to `notify_review`; the new match fields can flow through that existing output.
- Existing `r:a:<id>`, `r:r:<id>`, and `r:d:<id>` callback contracts remain unchanged.

## Scope

### Included

- Per-row running-saldo trace using the same opening checkpoint, source union, cutoff, and Kredit/Debit arithmetic as the existing verifier.
- Persisted match-row audit evidence.
- Readable HTML review/failure/detail output.
- One compact matching-row code block, not the full downloaded dataset.
- Tests for ordering, match selection, formatting, escaping, and audit mapping.
- Updated FinPay workflow documentation and deployment validation.

### Excluded

- No change to `row_hash` deduplication semantics.
- No change to finance approval/rejection behavior.
- No pagination or multi-message dump of all transactions.
- No new Telegram Premium dependency.
- No database reset or live refresh during implementation validation.
- No secondary ordering key beyond `transaction_date ASC`.

## Behavior Contract

1. Extraction and staging continue to report the raw source count as `source_row_count`/`downloaded_rows`.
2. Verification builds the exact ordered calculation input after checkpoint/cutoff filtering and committed-plus-current-staging union.
3. The trace starts from the selected opening checkpoint and applies:
   - `Kredit`: add amount.
   - `Debit`: subtract amount.
4. Trace rows are ordered only by `transaction_date ASC`.
5. The displayed `matching_row_number` is the 1-based position in the calculation input, not necessarily the raw CSV position when checkpoint filtering or deduplication removed rows.
6. The message displays both counts to avoid ambiguity:
   - `Data diunduh: 200 baris`.
   - `Baris dihitung: N`.
   - `Saldo cocok pada baris: X dari N baris dihitung`.
7. When several trace rows equal the CMS value within the existing tolerance, select the last matching row encountered during the ascending scan, which is the closest matching transaction to the CMS cutoff. Persist the count of matching rows if it is available without complicating the contract.
8. If the opening checkpoint itself equals the CMS value before the first transaction, record `matching_row_number=0`, `matching_transaction_id=NULL`, `matching_transaction_at=NULL`, and `matching_type=CHECKPOINT`.
9. If the final saldo matches CMS but no individual transaction row equals it, report `matching_type=CUTOFF` and the exact CMS/calculation cutoff instead of inventing a transaction row.
10. For a mismatch, retain the full calculation evidence but render `Tidak ada baris yang cocok` and the closest/final cutoff evidence; do not change the existing `MISMATCH` review state.
11. Current-day matching time is `bucket_snapshot_at`/`calculation_cutoff_at`, which are the exact CMS capture time used by `current_saldo_at`. Historical closed-day matching continues to show the CMS capture time plus the explicit date cutoff.

## Implementation Steps

### 1. Add Row-Level Trace

Update `finpay_topup_pipeline/saldo.py` with one small helper that reuses the existing source and checkpoint rules:

- Query the same committed-plus-staged projection used by `_compute_running_saldo`.
- Apply the same opening checkpoint and inclusive/exclusive cutoff filters.
- Select source fields needed for the matching row: transaction ID, timestamp, type, amount, sender, receiver, currency, remarks, source file, and row hash where available.
- Order only with `ORDER BY transaction_date ASC`.
- Iterate once from the opening balance and return:
  - final computed saldo;
  - calculation row count;
  - matching row number/type;
  - matching transaction ID and timestamp when the match is a real row;
  - running saldo at the selected match;
  - full matching row payload.
- Keep the helper pure with respect to database state; it must not insert, update, or commit.

Do not duplicate a second arithmetic implementation that can drift from `_compute_running_saldo`. Either make the existing saldo helper share the trace implementation or explicitly test that both produce the same final saldo.

### 2. Persist Match Evidence

Extend the existing `finpay_topup_refresh_cluster` audit contract with nullable fields and matching `ADD COLUMN IF NOT EXISTS` alters:

- `calculation_row_count INTEGER`
- `matching_row_number INTEGER`
- `matching_type TEXT`
- `matching_transaction_id BIGINT`
- `matching_transaction_at TIMESTAMPTZ`
- `matching_running_saldo NUMERIC(18,2)`

Update `audit.upsert_cluster_status` and its conflict update clause. New fields must be nullable so old audit rows remain readable.

Update `verify.verify_bucket_topup_values` to persist and return these fields for every cluster. Preserve existing `computed_saldo`, bucket, diff, source range, CMS timestamp, cutoff, and status behavior.

Update `services/telegram_bot/store.py` refresh-detail selection/mapping so the `Lihat detail` callback can render the new fields.

### 3. Make Review Output Readable

Update `finpay_topup_pipeline/notify.py`:

- Add an optional `parse_mode` argument to `send_telegram_message`; use `HTML` for top-up review/detail alerts only.
- Escape all dynamic values with `html.escape` before inserting them into HTML.
- Keep amounts formatted as Indonesian Rupiah, for example `<code>Rp 65.645.685</code>`.
- Render the compact alert with:
  - `<b>FINPAY TOP-UP</b>` header;
  - a separator line;
  - bold field labels;
  - code formatting for counts, IDs, timestamps, and amounts;
  - `Cocok dengan CMS pada` using the exact CMS/calculation timestamp;
  - `Transaksi terakhir dihitung` using the source maximum timestamp;
  - `Saldo cocok pada baris` using the trace result.
- Render only the matching transaction in a `<pre>` block, with all source-row fields and the exact timestamp. Do not render the other 199 rows.
- Render explicit fallback text for `CHECKPOINT`, `CUTOFF`, and no-match/mismatch cases.
- Keep technical failure messages escaped and readable without exposing credentials or raw exception payloads.

Update the Telegram bot detail formatter and service send/edit calls only as needed to pass HTML mode for top-up audit details. Existing non-HTML bot menus and classification cards must not break because of unescaped user/database text.

### 4. Keep Kestra Separation Explicit

The existing separated flow remains the source of orchestration boundaries:

- `prepare_refresh`: checkpoint/window/audit reservation.
- `extract_topup`: source download and retry.
- `ingest_topup`: parse, validate, checkpoint filter, batch dedupe, and staging.
- `refresh_saldo`: complete running-saldo calculation and CMS comparison, including match trace output.
- `reextract_topup`, `restage_topup`, `reverify_saldo`: technical recovery only.
- `finalize_refresh`: purge/controlled commit decision.
- `notify_review`: formatted review alert only.
- `summary_topup`: read-only summary.

Add the match row number/type and match timestamp to the structured `event=topup_verification` and `event=topup_review_alert` summaries. Do not move deduplication, validation, or arithmetic into the notification task.

If the flow YAML only changes expressions/structured fields, preserve the existing task IDs and trigger IDs. Use the existing `WorkingDirectory`/runtime `WORKING_DIR` fix; do not reintroduce `{{ workingDir }}` in child task `env` properties.

### 5. Update Tests

Add or update focused tests:

- `test_saldo.py`
  - trace uses the opening checkpoint;
  - Kredit/Debit arithmetic matches the existing final saldo;
  - ordering is exactly `transaction_date ASC` and has no secondary key;
  - cutoff exclusion/inclusion remains correct;
  - checkpoint and cutoff match types are handled.
- `test_verify.py`
  - verified result includes calculation count and matching row evidence;
  - CMS match timestamp is retained;
  - mismatch does not fabricate a matching row;
  - existing tolerance and review-state contracts remain unchanged.
- `test_notify.py`
  - HTML header/separators/labels/code blocks are present;
  - Indonesian Rupiah formatting is correct;
  - exact CMS match and transaction timestamps are rendered;
  - dynamic values containing `&`, `<`, `>`, and quotes are escaped;
  - only the matching row is rendered.
- Telegram bot tests
  - detail mapping exposes match fields;
  - review/detail output uses HTML safely;
  - existing callback data remains unchanged.
- Flow contract tests
  - structured verification output includes match fields;
  - notification formats the verification payload but does not calculate/dedupe rows;
  - task IDs and separated task boundaries remain present.
- Disposable PostgreSQL integration tests
  - one exact matching row is persisted and displayed;
  - duplicate hashes remain deduplicated;
  - current-day and historical cutoff cases retain the correct matching metadata.

### 6. Documentation

Update `finpay_topup_pipeline/README.md` and the root workflow section with:

- complete calculation definition: checkpoint through CMS cutoff over committed plus staged rows;
- raw downloaded count versus calculation row count;
- exact `transaction_date ASC` ordering rule;
- matching row/type semantics;
- current-day CMS timestamp versus historical cutoff semantics;
- readable Telegram HTML example;
- note that HTML/code formatting is standard Telegram Bot API functionality and needs no Premium account;
- explicit statement that only the matching row is shown, not all fetched rows or pagination.

### 7. Validation and Deployment

Run the narrow checks first:

```bash
python3.11 -m unittest -v \
  finpay_topup_pipeline.tests.test_saldo \
  finpay_topup_pipeline.tests.test_verify \
  finpay_topup_pipeline.tests.test_notify \
  finpay_topup_pipeline.tests.test_flow_contract
```

Run the complete FinPay and Telegram suites in the pinned Python 3.11 images and record executed/skipped counts. Run the disposable PostgreSQL integration suite with a DSN that cannot target the live `finpay` database. Run:

```bash
docker run --rm \
  -v "$PWD/finpay_topup_pipeline/workflows/finpay_topup_pipeline.yml:/flows/finpay_topup_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml

docker compose config --quiet
git diff --check
git diff --cached --check
```

Build the affected top-up and Telegram bot images. Deploy the reviewed flow only if its YAML changes; preserve the current flow and task contracts. Restart the local bot only if its formatter/service code changes. Do not reset live data for this feature.

For live E2E, use a new or existing verified refresh without approving/rejecting it automatically. Confirm the alert shows the range, counts, exact CMS match time, row number, and only the matching full row.

## Acceptance Criteria

- A verified refresh reports the complete running-saldo calculation, not a single arbitrary row.
- The message clearly shows the CMS match timestamp and the matching transaction timestamp/row when applicable.
- For 200 downloaded rows, Telegram shows `Data diunduh: 200 baris` and one matching row such as `Saldo cocok pada baris: 137 dari 200`, not all 200 rows.
- Matching row content includes the full source data and exact date/time.
- All dynamic text is HTML-escaped and Telegram accepts the message with standard Bot API HTML mode.
- Amounts use Indonesian Rupiah formatting such as `Rp 65.645.685`.
- No pagination or Telegram Premium dependency is introduced.
- Existing deduplication, checkpoint, verification tolerance, approval, rejection, and failure-cleanup behavior remains unchanged.
- Existing callback, workflow, and task IDs remain stable.

## Rollback

- Revert the formatter/trace code and redeploy the previous flow/image.
- New audit columns are nullable and can remain unused during rollback; no destructive schema rollback is required.
- Do not delete ledger or staging data as part of rollback.
- If HTML rendering is rejected by Telegram, temporarily send the same escaped content as plain text while retaining the persisted match evidence.
