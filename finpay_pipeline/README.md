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
| Cell running SALDO | Starts at zero on October 1, 2026, then carries forward; no opening transaction or separate balance table is created. |
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
| Cell format | `finpay_cell_range_v1` | `finpay_cell_day_v1`, with four explicit stages. |
| Bulk cell | `finpay_cell_bulk_v1` | Range acquisition/preprocessing, one DB commit and Sheet publication per cluster. |

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
unpartitioned multi-date CSV uploads. Existing daily/range workflows remain
unchanged. The October 1-19 live canary used two workers; acquisition still has
per-cell/day portal login overhead, so do not extrapolate an unmeasured
single-worker speedup.

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

The cell day task graph is:

```text
acquire_cell_cluster_day -> cell_sources_v2.zip
preprocess_cell_cluster_day -> cell_prepared_v1.json
persist_cell_sources_and_calculations -> cell_handoff_v1.json
publish_cell_four_tabs -> guarded Summary/Invoice/Unusual/Reversal publication
```

Artifacts are bounded and checksummed. Persistence does not create a Google
client; publication reacquires locks and rejects changed selected history before
writing, without re-importing sources. Spreadsheet name/optional ID, distinct
legacy ID, writer, and Finance editors must be configured; missing configuration
fails preflight before acquisition or DB writes. `write_gsheet=false` keeps the
bulk acquisition/preprocessing/DB path independent from Google writes.
Persisted cell history must stay contiguous; a range separated from existing
coverage by missing dates fails rather than assuming zero activity in the gap.
The new cell history starts on `2026-10-01`; later first persistence without
October 1 coverage is rejected. Per-day offline preprocessing remains available.

Existing IDs remain compatibility interfaces, including their original inputs
and artifact contracts:

| Path | Acquisition | Reporting |
|---|---|---|
| Previous format | `finpay_digipos_acquisition_v1` | `finpay_daily_pipeline_v5` |
| Earlier cell interface | `finpay_digipos_cell_acquisition_v2` | `finpay_cell_reporting_v1` |
| Four-tab cell interface | `finpay_digipos_cell_acquisition_v3` | `finpay_cell_reporting_v2` |
| Monthly | Not applicable | `finpay_monthly_materialization_v1` |

YAML lives in [`workflows/`](workflows/). The runtime pin is Kestra `v1.3.26`;
use its `ForEach`/`taskrun.value` syntax, not the newer `Loop` syntax. Declared
`outputFiles` and downstream `inputFiles` carry transaction artifacts through
Kestra internal storage. Use task outputs for small metadata, not source rows.

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

Four-tab publication uses one epoch, full-history fingerprints, target/cluster
locks, temporary guards, and value/formula/style/protection readback before
completion. A green tab/A1 note confirms Sheet verification only; success also
requires the database completion record. A failed or ambiguous DB confirmation
requires operator reconciliation even when the tabs look complete.
A checksum proves integrity, not source authority: deployment must
restrict non-dry source imports to approved callers. Existing managed or design
preview tabs are not fresh targets and cannot be adopted by inventing a baseline.

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
when the new Sheet running SALDO starts at zero.

- Preserve `raw_transaction_label`, source IDs, amounts, and `Remarks`; diagnostics belong in `unusual_reason`.
- Evaluate a transaction family consistently for unusual output and summary eligibility. Unknown labels/remarks quarantine the complete family.
- Run unusual detection before calculation deduplication; some unusual families remain included for Finance review.
- Dedup compares all relevant columns except `No`, with timestamps at minute precision; it does not delete immutable evidence.
- Raw `RECHARGE` with the complete normalized remark `transaksi sellthru` is the no-companion SELLTHRU exception. Direct SELLTHRU does not inherit it; companions quarantine the standalone family.
- Recharge and reversal-recharge fee expectation/cap is Rp20. Normal SELLTHRU platform fees total Rp100; missing/incorrect ST fees remain flagged but included, while excess fee rows are excluded.
- Before `2026-09-01`, normal ST requires `SELLTHRUSALESFEE`. September retires that companion and quarantines incoming SLSFEE families. From `2026-10-01`, it is required again; normalize source `SELLTHRUSALESHOLD` and terminal `SLSHLD` to the existing sales-fee family, preserving raw data and amounts. Cross-policy families quarantine; no inferred hold rate or month-end expiry is added.
- Recharge fee-only rows remain unusual but included. ST fee-only and unsupported `Reversal - ST*` families are unusual and excluded.
- Out-cluster relabeling and exact reversal linkage follow [`classification.py`](classification.py); downstream code trusts prepared labels instead of relabeling twice.
- Characterize new labels and remark structures from source before extending allowlists. The legacy October preprocessing change does not itself change legacy Sheet/invoice or monthly release contracts.

Legacy invoices include `BIAYA FEE BAR A ST (HOLD)` and its MANDIRI ST contribution
only before `2026-09-01`; September onward omits them. The October classification
restoration does not reinstate that legacy presentation. See
[`summary_sheets.py`](summary_sheets.py) for this separate invoice cutoff.

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

## Publication Safety

- Keep all Sheet writes sequential. Flow concurrency limits apply only to the same flow ID; database locks protect cell writers across entry points.
- Queue or reject overlapping legacy writes to the same spreadsheet, worksheet, and report date across entry points; per-flow limits are not a shared lock.
- Reacquire locks and validate persisted state before publishing across a task boundary. Reject stale handoffs before any Google mutation.
- Never retry or clear `IN_FLIGHT`/`UNKNOWN` publications blindly. Lost ownership or an ambiguous mutation requires approved operator reconciliation; acknowledged writes with failed readback remain pending.
- Preserve manual MANDIRI inputs except controlled, audited source-refresh resets. Missing history must fail, not become zero-activity days.
- Existing cell snapshots/layouts remain recovery inputs, not automatic rollback commands. Protection checks include read-only owner metadata; no sharing mutation is implied.
- Previous-format writers replace the requested date, grow row capacity, and repair the stable `LinkAja` reference worksheet before invoice formulas use it. LinkAja source population remains outside this package.
- Previous-format sheets retain A:F cash flow and H:K invoice panels, DETAIL/MANDIRI/Cash order, uppercase literal text, date separators, and editable protected MANDIRI inputs. The expected pembelian fee is count times Rp200, distinct from the Rp20 transaction-fee cap.

## Run And Validate

Use Kestra UI or `POST /api/v1/{tenant}/executions/finance.finpay/{flow_id}`.
Only terminal `SUCCESS` is success; inspect task logs otherwise. Process backfill
dates in ascending order and stop on failure. Keep schedules disabled until
separately approved commissioning.

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
Acquisition dry runs still contact DigiPOS; use synthetic bundles for offline
reporting checks. `write_gsheet=false` permits DB-only processing, not Sheet writes.

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
