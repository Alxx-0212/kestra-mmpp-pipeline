# FinPay Pipelines

FinPay acquisition, validation, daily reporting, and guarded monthly settlement.
The previous cluster format and the new cell format are separate reporting paths;
cell exports must never enter the previous-format cluster/date replacement writer.
Shared infrastructure setup belongs in the [repository README](../README.md).
Build, CI/CD, commissioning, and recovery steps are in
[production replication](PRODUCTION_REPLICATION.md).

## Status

| Capability | State |
|---|---|
| Previous-format acquisition and daily reporting | Implemented; separate acquisition, compute, database, and Sheet tasks. |
| Cell acquisition and four-tab reporting | Implemented through the compatibility flows below. |
| Independent subpackages, date-range acquisition, and staged cell tasks | Implemented; range inputs and task boundaries below. |
| Cell running SALDO | Daily reports leave cumulative SALDO unverified; range/bulk balance calculations retain the October 1, 2026 boundary. No opening transaction or separate balance table is created. |
| Cell publication targets | Package defaults fail closed. Configure a destination name (optionally pin its ID), legacy ID, writer and Finance editors through Kestra environment secrets or per-flow variables. Missing spreadsheet files/tabs can be created on a non-dry publish when enabled. |
| Monthly settlement | Preview and gated publication implemented; Finance release requirements remain separate. |
| Production commissioning and schedules | Not authorized by a local test or this refactor; acquisition schedules remain disabled. |

This guide describes operating contracts, not a test-results ledger. Keep temporary
execution results out of the README and remove scratch artifacts after review.

## Workflows

All flows use namespace `finance.finpay`. The new independently runnable paths
are [`legacy/`](legacy/) and [`cell_format/`](cell_format/), sharing existing
validation/calculation utilities without routing cell sources into legacy tables.

| Pipeline | Range entry point | Per-day processing |
|---|---|---|
| Previous format | `finpay_legacy_range_v1` | `finpay_legacy_day_v1`, then retained daily v5 tasks. |
| Cell format | `finpay_cell_range_v1` | `finpay_cell_day_v1` in full-history scope, with four explicit stages. Standalone day runs are date-scoped. |
| Bulk cell | `finpay_cell_bulk_v1` | Range acquisition/preprocessing, one DB commit and Sheet publication per cluster. |

`finpay_cell_range_v1` defines a disabled daily schedule for 05:00
`Asia/Makassar`. If separately commissioned and enabled, it processes the previous
day for `ALL` clusters serially through the per-cluster/day flow. Database persistence
and guarded Sheet publication retry up to three total attempts with exponential
backoff, reusing the checked cohort artifacts. `PENDING`, `IN_FLIGHT`, or `UNKNOWN`
publication states still require operator reconciliation; retries do not issue
blind Sheet writes or automatically clear those states.
The schedule is dormant by default; local workflow edits do not authorize production
activation.

The daily cell acquisition task also copies validated XLSX files to the Docker
host bind mount `/tmp/finpay inbox/<cluster>/<date>/<inbox_generation_sha256>/<cell>/`. The
filename inside each cell directory retains the previous convention:
`finpay-<cluster>(<DD-MM-YYYY>to<DD-MM-YYYY>).xlsx`. Each cohort is
checksum-verified and promoted as one content-addressed directory; changed sources
create a new generation rather than mixing versions. The key is derived from the
cluster/date/cell identities and source checksums, and is exposed as the acquisition
task's `inbox_generation_sha256` output. No inbox cleanup is scheduled.
Create the host directory before running the flow, and keep it restricted because
it contains raw financial source data.

The cell Sheet title scheme is `cluster_alias`, not the DigiPOS account code:

| DigiPOS account | Sheet prefix |
|---|---|
| `412110_B` | `PKY` |
| `422090_B` | `MRT` |
| `422100_B` | `TDR` |
| `422010_B` | `BGI` |
| `422180_B` | `MRW` |
| `422220_B` | `TNT` |
| `422140_B` | `KOTAMOB` |
| `422040_B` | `BOLMONG` |

Each prefix has `Summary`, `Invoice`, `Unusual`, and `Reversal` tabs (for example
`PKY`, `PKY Invoice`, `PKY Unusual`, `PKY Reversal`). Configure a copied
`finpay_cell_bulk_v1` flow's `cell_spreadsheet_name` and `cell_cluster_group`;
the group accepts one code, comma-separated codes, or `ALL`. The workbook ID is
optional if the exact name resolves to one file; duplicate names fail closed. A
pinned ID must already exist; name-only targets can create a missing workbook.
On non-dry publication, `create_spreadsheet_if_missing=true` creates a missing
workbook and `create_worksheets_if_missing=true` creates missing alias tabs. Only
a newly created workbook is shared with configured Finance editors; sharing on
existing files is unchanged. Keep
tab IDs stable across title-only migrations; arbitrary retargeting to another
workbook or role is rejected. Assign each cluster to only one destination flow;
cross-workbook routing for a cluster with publication history is blocked. Give
each copied flow a unique ID.

Flow defaults read `FINPAY_CELL_SPREADSHEET_NAME` and
`FINPAY_LEGACY_SPREADSHEET_ID` from Kestra secrets; optional
`FINPAY_CELL_SPREADSHEET_ID` pins the destination. `FINPAY_CELL_WRITER_EMAIL`
and `FINPAY_CELL_FINANCE_EMAILS` configure the writer/protection identity.
A private deployment flow may set these values directly in its `variables` block.

`finpay_cell_bulk_v1` accepts the inclusive date range and cluster selector plus
optional `scrape_workers` (default 2, maximum 4). It validates the complete
indexed account/date bundle before database writes, retries only failed
acquisition chunks, and publishes once per cluster. Scraping is parallel across
accounts with dates sequential per account; processing, PostgreSQL, and Sheet
writes are serial. It consumes validated daily exports, not arbitrary
unpartitioned multi-date CSV uploads. Day and range remain separate flow entry
points. Acquisition retains per-cell/day portal login overhead; worker bounds
do not remove that cost.

Range flows take `start_date`, `end_date` (inclusive `YYYY-MM-DD`,
`Asia/Makassar`), and `cluster_id` (one configured numeric cluster, a
comma-separated group, or `ALL`). For example, cell `cluster_id=422180` selects
DigiPOS account `422180_B`; a cloned bulk flow can default it to `412110,422010`.
They process date/cluster work sequentially through awaited child flows and stop
on failure. Submit large backfills in smaller consecutive ranges: sequential
iteration limits active work, not the total Kestra task-run count.
No configuration upload or checksum input is required. Safety
controls default to `dry_run=true`, `write_gsheet=false`; legacy also defaults
`send_telegram=false`. These controls are optional, not extra business inputs.

### Progress And Telegram

Current cell day, range, and bulk flows stream INFO progress into Kestra's Logs
tab. Acquisition reports the current cell, portal phase, attempt and completed
count; reporting logs validation, lock waits, commits, and Sheets chunk/readback
phases. Long operations emit a heartbeat every 30 seconds, identifying the current
phase and its elapsed time alongside full-stage elapsed time. Remaining-time
estimates reflect completed-count progress for the current role/total work scope;
they are approximate, not guaranteed. The range parent logs cohort `i/N`; open its
child execution for detailed progress. Heartbeats indicate a live process, not a
successful portal response or database commit. Source rows and credentials are
never included in these progress messages.

Set `send_telegram=true` to enable terminal cell alerts using the existing
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` Kestra secrets; it defaults to false,
and dry runs never notify. A successful DB/Sheets run sends a completion message
and a separate unusual alert when the requested cohort contains unusual rows.
Range runs notify per cluster/date child; bulk runs send one requested-range
summary. Unusual counts exclude unrelated stored history. DB-only successful runs
explicitly report Sheets as disabled. Failed/warning/killed runs send a status
alert when Kestra executes their terminal handler. Range failures always notify
from the parent, including orchestration failures before child launch. If a child
also fails, its alert and the parent status identify their distinct execution IDs.

Notification delivery is separate from financial success: inspect the
`notify_cell_result_telegram`, `notify_cell_bulk_result_telegram`, or the range's
`notify_cell_range_failure_telegram` post-run task
for delivery failures. A failed post-run notification does not change Kestra's
already-final execution state. Messages use plain text and one send attempt each;
an unconfirmed timeout is not automatically retried because Telegram may already
have accepted it. No Telegram plugin upgrade or bot-service change is required.

The cell day task graph is:

```text
preflight_cell_database (non-dry runs only, before acquisition)
  -> read-only target/schema/table-privilege check
acquire_cell_cluster_day -> cell_sources_v2.zip
preprocess_cell_cluster_day -> cell_prepared_v1.json
persist_cell_sources_and_calculations -> cell_handoff_v1.json
publish_cell_four_tabs -> guarded Summary/Invoice/Unusual/Reversal publication
```

Both day and bulk workflows run the read-only database preflight before DigiPOS
acquisition when `dry_run=false`, using the existing `FINPAY_DB_*` secrets. Dry
runs skip the task and do not connect to PostgreSQL. The preflight checks the
configured target and runtime role's schema/table privileges; it creates no
schema or tables and applies no DDL or migration. Cell schema setup remains an
explicit operation.

Bulk reporting tasks explicitly use the `cell_reporting_network` flow variable
(default `bridge`). For a Compose-local database hostname such as `postgres`, set
this variable to the exact shared Docker network name before running the flow.
`kestra.plugins.configurations` does not set this runner-instance property;
`networkMode` must be explicit on each nested `taskRunner`. Acquisition retains
its separate host network. The selected
reporting network must resolve the database host and allow Google HTTPS access.

Cell artifacts are size-bounded, with SHA-256 checks performed by application
code at specific task boundaries. Persistence does not create a Google client;
publication reacquires locks and rejects changed selected history before writing,
without re-importing sources. Spreadsheet name/optional ID, distinct
legacy ID, writer, and Finance editors must be configured; missing configuration
fails preflight before acquisition or DB writes. `write_gsheet=false` keeps the
bulk acquisition/preprocessing/DB path independent from Google writes.
Daily persistence validates and atomically replaces only the requested complete
cell/date cohort. It does not require other dates or clear the current cohort if
acquisition or validation fails; immutable source generations and row hashes are
retained. A standalone daily Sheet publication patches only that date's block
in each tab, resizing variable-height blocks as needed while preserving MANDIRI
inputs and all other dates. The range flow passes full-history scope to each
sequential day child and retains its complete-coverage checks and full-report
publication. Before deleting rows, the daily writer refuses a block shrink if
the affected rows contain values/formulas outside the governed matrix; resolve
that extra content before retrying the persisted handoff. Daily database results
never store cumulative SALDO. If the corrected Sheet date already has an SALDO
formula chain, preserve it so later dependent SALDO formulas may recalculate.
Without an existing chain, the date's Summary block marks
`CUMULATIVE SALDO UNVERIFIED` and leaves SALDO values blank; earlier dates are
not required. The configured boundary remains
`2026-10-01`; source balance evidence remains in the ledger.
Full-range/range/bulk balance calculations retain their historical-coverage
checks; the date-scoped behavior applies to standalone day runs only.

Bulk persistence and Sheet publication retry the whole task up to three times.
Persistence reuses the same prepared bundle without duplicating evidence. A daily
execution changes only its requested date block; range/bulk publication retains
its full-report behavior. Finance MANDIRI inputs and unrelated date blocks remain
intact during daily updates. The Kestra execution ID identifies the request:
retries verify already-completed clusters without rewriting them. An uncertain
write remains blocked for reconciliation; it is never blindly retried.

Existing IDs remain compatibility interfaces, including their original inputs
and artifact contracts:

| Path | Acquisition | Reporting |
|---|---|---|
| Previous format | `finpay_digipos_acquisition_v1` | `finpay_daily_pipeline_v5` |
| Earlier cell interface | `finpay_digipos_cell_acquisition_v2` | `finpay_cell_reporting_v1` |
| Four-tab cell interface | `finpay_digipos_cell_acquisition_v3` | `finpay_cell_reporting_v2` |
| Monthly | Not applicable | `finpay_monthly_materialization_v1` |

YAML lives in [`workflows/`](workflows/). The runtime pin is Kestra `v1.3.26`;
use its `ForEach`/`taskrun.value` syntax, not the newer `Loop` syntax. Kestra
stores declared `outputFiles` and makes them available as downstream
`inputFiles`; those declarations do not establish automatic checksum validation.
Application code checks artifact hashes at task boundaries where implemented,
while database freshness checks separately protect selected persisted history.
Use task outputs for small metadata, not source rows.

### Previous Format

The daily reporting flow has **no Kestra trigger or schedule**. Upload one file
or let acquisition call it through an awaited Subflow. Acquisition v1 validates
all six exports before starting any daily child, then processes them sequentially.
Its 05:00 `Asia/Makassar` previous-day schedule is disabled.

| Daily input | Meaning |
|---|---|
| `csv_file` | Required CSV/XLSX FILE. |
| `source_filename` | Original basename when Kestra stores the upload as `.upl`. |
| `dry_run` | Default `false`; compute without DB, Sheets, or notification writes. |
| `write_gsheet` | Default `true`; `false` retains DB persistence but skips Sheets and Telegram. |
| `send_telegram` | Default `true`; disable unusual alerts independently. |

Filename: `finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).csv|xlsx`.
The first date determines reporting; the second is only matched syntactically.
The parser also accepts `_` instead of the opening parenthesis and an optional
closing `)` or `_`. Legacy `.xls` is explicitly rejected.

The legacy workbook is `Salinan dari Monitoring Finpay & LinkAja`:

| Cluster | Base worksheet |
|---|---|
| `411311` | `PKY` |
| `421306` | `MRT` |
| `421307` | `TDR` |
| `421315` | `BGI` |
| `421318` | `MRW` |
| `421320` | `TNT` |

Acquisition v1 takes `report_date`, defaults to `dry_run=true`, and passes
`write_gsheet`/`send_telegram` to daily children. `scrape_workers` is bounded to
1-6 (default 3); each browser has its own thread/session. Native XLSX download
and correlated DataTables fallback enforce the same source validation.

### Cell Format

The current roster is defined in [`cell_contracts.py`](cell_contracts.py):
`422040_B`, `422140_B`, `412110_B`, `422010_B`, `422090_B`, `422100_B`,
`422180_B`, and `422220_B`. Morowali is `422180_B`, not legacy `421318`.
Select each concrete cell, never `Semua Cell`, and retain a separate manifest
entry and source artifact per account/cell/date. Preserve exact portal identities;
download order and source `No` are not cell identifiers.

New cell tabs use cluster aliases, not numeric cluster codes:

| DigiPOS account | Tab prefix |
|---|---|
| `412110_B` | `PKY` |
| `422090_B` | `MRT` |
| `422100_B` | `TDR` |
| `422010_B` | `BGI` |
| `422180_B` | `MRW` |
| `422220_B` | `TNT` |
| `422140_B` | `KOTAMOB` |
| `422040_B` | `BOLMONG` |

Each prefix has `Summary`, `Invoice`, `Unusual`, and `Reversal` worksheets.
Tab-title migration is allowed only for the same worksheet IDs and the exact
code-to-alias mapping; it creates a new publication epoch and retains prior
history. Arbitrary workbook, worksheet ID, role, or account changes remain blocked.
Workflows may be copied per destination: set the copied flow's `cell_spreadsheet_name`
and `cell_cluster_group` (a single code, comma-separated group, or `ALL`).
If a file name resolves to no spreadsheet and creation is enabled, publication
creates it and shares the new file with configured Finance editors. Existing
file sharing is unchanged. Missing role tabs are added by alias; populated
unmanaged tabs or missing tabs with prior publication history fail closed.

The existing v3/v2 compatibility pair requires a schema-v2 configuration FILE,
its checksum, and the independent `FINPAY_CELL_CONFIG_SHA256` deployment pin.
Those old interfaces retain their explicit balance and target rules. The new
no-config entry points must not require that upload or pin.

Cell evidence uses the separate `finpay_cell_*_v1` relations. Each cluster has
four outputs: Summary, Invoice, Unusual, and Reversal, all in a dedicated workbook
distinct from the legacy target. Invoice is FinPay-only: separate C1/C2/optional
C3 panels with CASH IN - NGRS, SELLTHRU, and ACCOUNTING sections. QRISDUWIT
disbursement dates appear inside Cash In, not in another cell detail worksheet.
LinkAja calculations and fee rows are not included.

Current Summary presentation uses shared A:C and cell blocks D:F, H:J, L:N,
with editable bank inputs D/H/L and next-day cashout references E/I/M. Invoice
titles merge each panel as `C1 | CELL 412111`, report-date columns are 140px,
and panels have three blank separator columns. Invoice days retain two blank
rows and continuous panel side rails. Unusual/Reversal display validated relative
CELL numbers 1-3, preserve semantic widths and muted stripes, and separate each
visible date with a top border. Source full cell codes remain in evidence.

Four-tab publication uses one epoch, target/cluster locks, temporary guards, and
value/formula/style/protection readback before completion. Daily publication
merges one date's Summary, Invoice, Unusual, and Reversal blocks into the saved
workbook snapshot and writes only those rows; range/bulk publication remains a
full-report write. Missing initial tabs are created only
when `create_worksheets_if_missing` is enabled. A missing worksheet ID with
previous publication history fails closed rather than being rebound. A green
tab/A1 note confirms Sheet verification only; success also requires the database
completion record. A failed or ambiguous DB confirmation requires operator
reconciliation even when the tabs look complete.
The current source configures the dedicated cell Google client with a 30-second
connection timeout and a 300-second response-read timeout (the prior source values
were 10/120 seconds). This source change does not update a deployed image
automatically; rebuild and deploy the image before relying on the new values in
Kestra. Google can spend up to 180 seconds processing one Sheets request, so the
client must not give up earlier. The timeout is a socket inactivity limit, not a
total job deadline, and cannot fix a broken VPN or network route.

Clusters and writes remain sequential. A bulk publication lazily shares one
client across its clusters so pacing does not reset between them. HTTPS request
attempts within each cell client are spaced at least 1.25 seconds apart, including
metadata, paged style readback, and writes. Fast calls sleep for the remaining interval; slow calls
already satisfy it. This limits one serial client to approximately 48 requests
per minute, below Sheets' 60-per-user-per-project read and write quotas. It does
not coordinate quota across other client instances, even in the same process,
or across separate processes/stacks using the same service account. Do not run
independent publishers concurrently against the same workbook without shared
quota coordination. A `ReadTimeout` is not proof of a quota rejection; HTTP 429 is the
explicit rate-limit response. The cell HTTP transport sets `max_retries=0`,
including for mutations; Kestra task retries are separate and cannot clear
unresolved `PENDING` or `UNKNOWN` publication states.

Read-only metadata, value, and style fetches retry HTTP 408/429/500/502/503/504,
Drive 403 rate/quota errors explicitly identified by `usageLimits`, and
request timeout/connection errors for at most six attempts with 2/4/8/16/32-second
backoff. Numeric `Retry-After` is honored only when finite and at most 60 seconds;
invalid or longer values fail closed. Authentication, permission, daily-limit,
disabled-API, and validation errors propagate immediately,
and `batch_update` or any other mutation is never retried.
Worksheet creation is one-shot: bounded exact-title readback confirms only a
single matching tab with the exact returned ID when present and configured ID
when pinned. A failed or ambiguous readback leaves the target-operation journal
`UNKNOWN` for operator reconciliation.
A checksum proves integrity, not source authority: deployment must
restrict non-dry source imports to approved callers. Existing managed or design
preview tabs are not fresh targets and cannot be adopted by inventing a baseline.
Date, stale-handoff, and publication-state guards remain in force for corrections.
Stored-coverage checks apply to full-range cumulative calculations; a date-scoped
daily run validates its complete cell/date cohort without requiring earlier dates
or persisting a cumulative balance. Existing Sheet formula chains are preserved
for dependent recalculation. Ambiguous `IN_FLIGHT`/`UNKNOWN` state is never
bypassed.

A byte-different XLSX creates a new source generation because generation identity
includes the source-file SHA-256. If its normalized-row fingerprint and row count
are unchanged, that generation change alone does not require a manual bank-input
reset. Date-scoped daily updates preserve MANDIRI inputs on the corrected date and
all other dates. Full-range correction retains its existing audited reset policy.

## Data Contracts

### Validation And Calculation

Source exports have exactly these 11 columns (order may vary):

```text
No, Transaction Date, Transaction ID, Saldo Awal, Kredit, Debet,
Saldo Akhir, Transaction Type, Transaction, Nomor RS, Remarks
```

The loader detects headers in the first ten rows. CSV accepts semicolon, comma,
pipe, or tab delimiters; XLSX preserves exact IDs, phone numbers, and literal
remarks. Required monetary values are integers, not manufactured zeroes.
`Transaction Date` uses `DD-MM-YYYY HH:MM:SS`. Integrity rejects rows with both
Kredit and Debet nonzero; both zero is allowed. Keep raw DigiPOS balances even
though the date-scoped daily Sheet leaves cumulative SALDO unverified.

- Preserve `raw_transaction_label`, source IDs, amounts, and `Remarks`; diagnostics belong in `unusual_reason`.
- Evaluate a transaction family consistently for unusual output and summary eligibility. Unknown labels/remarks quarantine the complete family.
- Run unusual detection before calculation deduplication; some unusual families remain included for Finance review.
- Dedup compares all relevant columns except `No`, with timestamps at minute precision; it does not delete immutable evidence.
- Raw `RECHARGE` with the complete normalized remark `transaksi sellthru` is the no-companion SELLTHRU exception. Direct SELLTHRU does not inherit it; companions quarantine the standalone family.
- Recharge and reversal-recharge fee expectation/cap is Rp20. Normal SELLTHRU platform fees total Rp100; missing/incorrect ST fees remain flagged but included, while excess fee rows are excluded.
- The previous-format family policy is effective-dated: before `2026-09-01`, normal ST requires `SELLTHRUSALESFEE`; the temporary legacy September rule retires that companion and quarantines incoming SLSFEE families; from `2026-10-01`, the shared classifier recognizes the existing family again. Legacy invoice presentation remains separately governed below.
- The new cell reporting boundary is `2026-10-01`. For every supported cell day, normalize source `SELLTHRUSALESHOLD`/terminal `SLSHLD` to the separate `SELLTHRUSALESFEE` category, preserving raw labels, IDs, amounts, and `Remarks`. Normal ST requires platform fee companions totaling Rp100 and separately accounts the source SLSHOLD amount. The cell policy has no September retirement or annual expiry: both fees contribute to ST expected settlement and full-range balance calculations, and the hold line remains in Summary/Invoice even when zero. A daily date-scoped Summary leaves cumulative SALDO blank and unverified. A missing hold is not fabricated as a source row and remains Unusual; an observed zero-valued hold is present and remains zero.
- Recharge fee-only rows remain unusual but included. ST fee-only and unsupported `Reversal - ST*` families are unusual and excluded.
- Out-cluster relabeling and exact reversal linkage follow [`classification.py`](classification.py); downstream code trusts prepared labels instead of relabeling twice.
- Characterize new labels and remark structures from source before extending allowlists. The legacy October preprocessing change does not itself change legacy Sheet/invoice or monthly release contracts.

Legacy invoices include `BIAYA FEE BAR A ST (HOLD)` and its MANDIRI ST contribution
only before `2026-09-01`; September onward omits them. The October classification
restoration does not reinstate that legacy presentation. This is separate from the
permanent cell Summary/Invoice rule above. See
[`summary_sheets.py`](summary_sheets.py) for this separate invoice cutoff.

Cell Summary, SALDO/ST expected settlement, and Invoice always include the
SLSHOLD line; empty/missing fee data projects as zero, while the Unusual rules
still distinguish an absent companion from an observed zero source row. This
cell policy must not change the legacy September behavior.

See [`classification.py`](classification.py), [`dedup.py`](dedup.py),
[`summary.py`](summary.py), and [`cell_calculation.py`](cell_calculation.py)
for the executable rules and their regression tests under [`tests/`](tests/).

### Artifacts And Persistence

The previous-format flow retains these task artifacts:
`validated.parquet`, `integrity_checked.parquet`, `preprocessed.parquet`,
`unusual.parquet`, `deduplicated.parquet`, `qrisduwit.parquet`,
`reversal.parquet`, `summary_ready.parquet`, and `summary.parquet`.
Raw persistence precedes preprocessing. Summary persistence uses only
summary-eligible rows, not every processed row.

| Previous-format table | Contents |
|---|---|
| `finpay_raw_transactions` | Validated source rows, including duplicates. |
| `finpay_transactions` | Summary-eligible calculation rows. |
| `finpay_unusual_transactions` | Unusual rows and separate diagnostics. |
| `finpay_qrisduwit_transactions` | QRISDUWIT details and disbursement dates. |
| `finpay_reversal_transactions` | Deduplicated reversal details. |
| `finpay_source_loads`, `finpay_ledger_events` | Append-only source generations and rows. |

Current compatibility batches replace `cluster_id + report_date`; corrected
sources supersede rather than delete evidence. Do not persist source `No` or
introduce surrogate IDs. Existing automatic compatibility-schema reconciliation
is legacy behavior, not permission to add implicit migrations.

Daily initialization applies only `001_finpay_daily_source_evidence.sql`;
monthly initialization additionally applies `002_finpay_monthly_model.sql`.
Retain `001_finpay_transaction_model_legacy_compat.sql` for compatibility/rollback.
Cell migration `003_finpay_cell_reporting_v1.sql` is explicit and never applies
as an ordinary write side effect. Empty verified cell-days may supersede current
data; incomplete or failed downloads must not clear it.

Cell database state is split by purpose:

- Evidence: `finpay_cell_configs_v1`, `finpay_cell_sources_v1`,
  `finpay_cell_ledger_v1`, and `finpay_cell_calculations_v1` retain configuration,
  source generations/rows, and calculated results without replacing prior evidence.
- Current/recalculation state: `finpay_cell_current_v1` selects each cell/date's
  generation and calculation; `finpay_cell_current_results_v1` is its result view;
  `finpay_cell_dirty_dates_v1` tracks changed calculations and reset requirements.
- Publication/recovery state: `finpay_cell_publications_v1`,
  `finpay_cell_publication_dates_v1`, and `finpay_cell_target_operations_v1` track
  publication snapshots/statuses and ambiguous target operations;
  `finpay_cell_manual_resets_v1` records manual bank-input resets.

Exact reruns reuse evidence rows rather than duplicating them, but still update
`finpay_cell_current_v1.selected_at`. Fingerprints use canonical JSON: dictionary
keys are sorted, while list order is preserved.

## Publication Safety

- Keep all Sheet writes sequential. Flow concurrency limits apply only to the same flow ID; database locks protect cell writers across entry points.
- Queue or reject overlapping legacy writes to the same spreadsheet, worksheet, and report date across entry points; per-flow limits are not a shared lock.
- Reacquire locks and validate persisted state before publishing across a task boundary. Reject stale handoffs before any Google mutation.
- Never retry or clear `IN_FLIGHT`/`UNKNOWN` publications blindly. Lost ownership or an ambiguous mutation requires approved operator reconciliation; acknowledged writes with failed readback remain pending.
- Daily date-block writes preserve manual MANDIRI inputs and unrelated dates. Daily cohorts do not require earlier dates; full-range cumulative calculations still require configured coverage and never treat missing history as zero-activity days.
- Existing cell snapshots/layouts remain recovery inputs, not automatic rollback commands. Protection checks include read-only owner metadata; no sharing mutation is implied.
- Previous-format writers replace the requested date, grow row capacity, and repair the stable `LinkAja` reference worksheet before invoice formulas use it. LinkAja source population remains outside this package.
- Previous-format sheets retain A:F cash flow and H:K invoice panels, DETAIL/MANDIRI/Cash order, uppercase literal text, date separators, and editable protected MANDIRI inputs. The expected pembelian fee is count times Rp200, distinct from the Rp20 transaction-fee cap.

## Run And Validate

Use Kestra UI or `POST /api/v1/{tenant}/executions/finance.finpay/{flow_id}`.
Only terminal `SUCCESS` is success; inspect task logs otherwise. Bulk/range
backfills process dates in ascending order and stop on failure; date-scoped day
requests can run independently. Keep schedules disabled until separately
approved commissioning.

Before a production backfill, take a verified external backup and baseline
counts, verify the pipeline role's schema/table privileges, and initialize only
the daily evidence schema. Run a dry-run canary, then one approved DB/Sheet
canary with parity and rerun checks before the chronological batch. Verify source
coverage and retained generations afterward; keep monthly publication disabled.

Credentials remain in Kestra secrets: `DIGIPOS_PASSWORD`, `FINPAY_DB_*`, and
`GCP_SA_KEY`; legacy alerts also use Telegram secrets. Never place passwords,
tokens, service-account keys, or source rows in inputs, logs, code, or Git.
Rotate the historically exposed standalone scraper credential before enabling
production acquisition. Browser dependencies belong in the scraper images,
not a local Chromium installation.

Reporting `dry_run=true` never constructs DB/Google clients or sends alerts.
Acquisition dry runs still contact DigiPOS; the daily cell flow may also copy
validated XLSX files into its inbox when the bind mount is configured. Use
synthetic bundles for offline reporting checks. `write_gsheet=false` permits
DB-only processing, not Sheet writes.

From repository root, with the cached `finpay-pipeline:3.11` runtime:

```bash
docker run --rm --pull never --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 python -m unittest discover -s finpay_pipeline/tests
git diff --check
```

Validate workflow properties with the pinned native validator, not YAML alone:

```bash
docker run --rm --pull never --network none \
  -v "$PWD/finpay_pipeline/workflows:/flows:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 flow validate /flows --local --config /validation.yml
```

Missing dependencies, skipped suites, or historical results are not fresh
verification. See [validation](../docs/validation.md),
[security](../docs/security.md), and [dependencies](../docs/dependencies.md)
before checks, live access, migrations, or builds. Rebuild affected images after
source/package changes only when authorized; source-mounted tests do not verify
image packaging.

## Monthly Settlement

The monthly flow previews by default. Final publication requires complete
expected dates, an exclusive `Asia/Makassar` cutoff, exact-ID reversal resolution,
the approved preview fingerprint, approver identity, and the release gate.
Preview must not mutate frozen monthly snapshots. Daily reruns do not settle
Odoo or authorize a monthly close. Monthly artifacts are
`finpay_monthly_result.json`, `finpay_monthly_transactions.csv`,
`finpay_monthly_categories.csv`, and `finpay_monthly_exceptions.json`.

Finance decisions, remaining gates, and rollback requirements remain in the
[monthly implementation plan](FINPAY_REVERSAL_MONTHLY_IMPLEMENTATION_PLAN.md).
