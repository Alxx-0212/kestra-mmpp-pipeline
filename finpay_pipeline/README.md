# FinPay Workflow and Calculation Guide

This is the canonical technical and operational guide for the FinPay domain.
Repository-wide setup and service commands remain in the root `README.md`.

## Project Shape

This repo contains a Kestra workflow for daily FinPay files. The workflow reads
one uploaded CSV/XLSX file, validates it, relabels transaction groups,
flags unusual rows, deduplicates calculation rows, writes selected outputs to
Postgres, and writes reports to Google Sheets.

The active flow is:

```text
finpay_pipeline.yml
id: finpay_daily_pipeline_v5
namespace: finance.finpay
```

Kestra tasks run the Docker image `finpay-pipeline:3.11` and import Python with:

```python
from pipeline import ...
```

`pipeline.py` re-exports `finpay_pipeline`. Keep this compatibility layer unless
every Kestra import is updated. `pipeline_refactored.py` is also a compatibility
shim for older local scripts and notebooks.

### Current Invocation And Inputs

Despite `daily` in the flow ID, the active workflow has no Kestra trigger or
schedule. An operator or API client starts it and supplies the file. Website
scraping, Hermes, and the existing external acquisition cron are outside this
workflow; this flow begins at the immutable uploaded file boundary.

| Input | Type | Default | Contract |
|---|---|---|---|
| `csv_file` | `FILE` | required | One FinPay `.csv` or `.xlsx` export matching the filename contract below. |
| `dry_run` | `BOOLEAN` | `false` | When `true`, execute validation and calculation but skip PostgreSQL, Google Sheets, Telegram, and the final sleep. |

The filename parser uses this exact shape:

```text
finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).<csv|xlsx>
```

The parser also accepts `_` in place of the opening parenthesis and an optional
closing `)` or `_`. The first date becomes `report_date`; the second date is
matched syntactically but is not parsed or otherwise used. The previous
month-end relative to the first date becomes the opening-balance initialization
date.
An unconfigured cluster, non-matching filename, or legacy `.xls` file fails
before processing. Convert `.xls` exports to CSV or XLSX first.

Current cluster routing is embedded in `finpay_pipeline.yml`:

| Cluster ID | Base worksheet | Spreadsheet | Default starting balance |
|---|---|---|---:|
| `411311` | `PKY` | `Salinan dari MONITORING FINPAY` | `0` |
| `421306` | `MRT` | `Salinan dari MONITORING FINPAY` | `0` |
| `421307` | `TDR` | `Salinan dari MONITORING FINPAY` | `0` |
| `421315` | `BGI` | `Salinan dari MONITORING FINPAY` | `0` |
| `421318` | `MRW` | `Salinan dari MONITORING FINPAY` | `0` |
| `421320` | `TNT` | `Salinan dari MONITORING FINPAY` | `0` |

Runtime metadata uses `Asia/Makassar`. The report date used by calculations,
database replacement, and sheet replacement comes from the filename, not the
execution date.

## Editing Rules

- Keep changes aligned with the workflow task boundaries in `finpay_pipeline.yml`.
- Do not put secrets, emails, spreadsheet IDs, or credentials in committed code.
  Runtime workflow values are passed through Kestra secrets, commonly from
  base64 values in local `.env_encoded`.
- Preserve rerun idempotency: DB writes replace `cluster_id + report_date`, and
  sheet writers replace existing rows for the same report date.
- Do not reintroduce parallel Google Sheets writes unless backoff/batching is
  redesigned. The workflow is intentionally sequential around sheet writes to
  avoid Sheets API rate limits.
- Use structured DataFrame operations. Avoid ad hoc string mutation for schema
  and table writes when helper functions already exist.
- If a Kestra task import changes, update both `finpay_pipeline/__init__.py` and
  the task script that imports it.
- Keep public imports stable unless the user explicitly approves a contract
  change.

## Module Map

| Module | Responsibility |
|---|---|
| `finpay_pipeline.loading` | Load CSV/XLSX, detect the true header row, coerce data types, validate `FINPAY_SCHEMA`. |
| `finpay_pipeline.integrity` | Enforce debit/credit integrity after schema validation. |
| `finpay_pipeline.classification` | Preprocess transaction labels, relabel out-cluster, standalone ST, and reversal rows, validate known remark structures and fee groups, build unusual rows, decide summary exclusion. |
| `finpay_pipeline.dedup` | Drop duplicate calculation rows and create duplicate-row reports. |
| `finpay_pipeline.summary` | Aggregate summary-ready rows by processed transaction label. |
| `finpay_pipeline.database` | Define/reconcile Postgres schemas, serialize rows, and replace table batches by `cluster_id + report_date`. |
| `finpay_pipeline.sheets_common` | Build gspread clients, create/open spreadsheets, manage protection, row capacity, A1 ranges, value formatting, and shared formatting helpers. |
| `finpay_pipeline.summary_sheets` | Write the main daily summary sheet, compact invoice panel, and the prerequisite shared `LinkAja` reference-sheet layout used by invoice formulas. |
| `finpay_pipeline.detail_exports` | Build and write QRISDUWIT and Reversal detail worksheets. |
| `finpay_pipeline.unusual_sheets` | Write unusual transaction rows to the per-cluster unusual worksheet. |

The read-only database rule check is owned by
`finpay_pipeline/queries/check_finpay_transactions_rules.sql`.

## Workflow Data Flow

Current high-level flow:

```text
uploaded file
-> parse_and_resolve
-> determine_current_date
-> load_and_validate -> validated.parquet
-> validate_integrity -> integrity_checked.parquet
-> persist_raw_transactions_to_db -> finpay_raw_transactions
-> preprocess_transaction_labels -> preprocessed.parquet
-> flag_unusual_transactions -> unusual.parquet
-> persist_unusual_to_db -> finpay_unusual_transactions
-> upload_unusual_to_sheets
-> notify_unusual_telegram when unusual rows exist
-> deduplicate_transactions -> deduplicated.parquet
-> filter_qrisduwit_rows -> qrisduwit.parquet
   -> persist_qrisduwit_to_db
   -> upload_qrisduwit_to_sheets
-> filter_reversal_rows -> reversal.parquet
   -> persist_reversal_to_db
   -> upload_reversal_to_sheets
-> prepare_summary_transactions -> summary_ready.parquet
-> persist_transactions_to_db -> finpay_transactions
-> summarize -> summary.parquet
-> upload_to_sheets (summary.parquet + qrisduwit.parquet)
```

Important ordering:

- `persist_raw_transactions_to_db` runs before relabeling and dedup.
- `flag_unusual_transactions` runs after preprocessing but before calculation
  dedup, so duplicate rows can still be reported as unusual.
- Dedup happens before detail exports and summary preparation.
- `finpay_transactions` is written from `summary_ready.parquet`, so it only
  contains rows included in summary calculation.
- Summary preparation re-evaluates fee-cap, unknown-label, unknown-remark,
  reversal, and fee-only exclusion conditions against the explicitly
  deduplicated dataframe.
  The returned summary-unusual dataframe is used for task counts only; the
  workflow does not persist it or append it to `unusual.parquet` or the unusual
  sheet.

`dry_run=true` still runs the compute path. Production side effects are skipped:
Postgres writes, Google Sheets uploads, and Telegram alerts.

### Persisted Task Artifacts

Artifact names are workflow contracts because later tasks reference them
directly:

| Artifact | Meaning |
|---|---|
| `validated.parquet` | Strictly schema-validated source rows. |
| `integrity_checked.parquet` | Validated rows after debit/credit integrity checks. |
| `preprocessed.parquet` | Rows with preserved raw labels and processed transaction labels. |
| `unusual.parquet` | Pre-summary unusual rows, including calculation duplicates. |
| `deduplicated.parquet` | Calculation rows after minute-level duplicate removal. |
| `qrisduwit.parquet` | QRISDUWIT detail rows, including extracted disbursement date. |
| `reversal.parquet` | Reversal detail rows. |
| `summary_ready.parquet` | Deduplicated rows retained for summary calculation. |
| `summary.parquet` | Totals grouped by processed transaction label. |

## Input File Contract

The loader detects the real header within the first ten rows. CSV parsing tries
semicolon, comma, pipe, and tab delimiters using UTF-8; Excel uses `.xlsx`.
Legacy `.xls` is explicitly rejected. The Pandera schema is strict, so missing
or additional columns fail validation. Column order may vary, but these 11 names
are required:

```text
No
Transaction Date
Transaction ID
Saldo Awal
Kredit
Debet
Saldo Akhir
Transaction Type
Transaction
Nomor RS
Remarks
```

`No`, the three balance fields, `Kredit`, and `Debet` must coerce to integers.
`Transaction Date` must parse as `DD-MM-YYYY HH:MM:SS`. After schema validation,
integrity validation rejects a row only when both `Kredit` and `Debet` are
non-zero; a row with both values zero is allowed by the current rule.

## Public API Used By Kestra

The public import surface is controlled by `finpay_pipeline/__init__.py`.
Common task imports:

| Task | Public functions |
|---|---|
| `load_and_validate` | `load_and_validate_schema` |
| `validate_integrity` | `validate_debit_credit_integrity` |
| `preprocess_transaction_labels` | `preprocess_transaction_labels` |
| `flag_unusual_transactions` | `flag_unusual_transactions` |
| `deduplicate_transactions` | `drop_duplicate_rows_by_minute` |
| `filter_qrisduwit_rows` | `prepare_transaction_detail_export` |
| `filter_reversal_rows` | `prepare_reversal_detail_export` |
| `prepare_summary_transactions` | `prepare_reversal_summary_transactions` |
| `summarize` | `summarize_by_transaction` |
| DB tasks | `postgres_dsn_from_env`, `write_finpay_dataframe_to_postgres` |
| Sheet tasks | `make_gspread_client`, `process_daily_upload`, `process_unusual_upload`, `process_transaction_detail_upload` |

## Transaction Label Rules

Preprocessing preserves the original source label in `raw_transaction_label`,
then updates `Transaction` and `processed_transaction_label` for downstream
outputs.

Downstream reversal summary/detail helpers call an internal
`ensure_reversal_labels_prepared(...)` helper. If `processed_transaction_label`
exists, they trust the preprocessed labels and do not relabel again. Raw legacy
callers without that marker still fall back to the reversal remark classifier.

Out-cluster relabeling:

- A `RECHARGE` group whose main row remarks contain
  `fee pembelian recharge out cluster` becomes `RECHARGE OUT CLUSTER`.
- Its matching `RECHARGEFEE` rows become `RECHARGE OUT CLUSTER FEE`.
- A `RECHARGE` row whose remarks contain
  `biaya pembelian recharge out cluster` becomes
  `PEMBELIAN RECHARGE OUT CLUSTER`.

Standalone Sellthru relabeling:

- A source `RECHARGE` row whose complete normalized remark is
  `transaksi sellthru` becomes `SELLTHRU` and is included in the ST summary.
- This is a characterized standalone main-row case, so it does not require
  `SELLTHRUFEE` or `SELLTHRUSALESFEE` companions.
- `raw_transaction_label` remains `RECHARGE`; only `Transaction` and
  `processed_transaction_label` become `SELLTHRU`. Source `Remarks` remains
  unchanged.

Reversal relabeling:

- `biaya pembelian recharge` -> `Reversal - NGRS`
- `platform fee recharge rp. 20,-` -> `Reversal - NGRS FEE`
- `biaya pembelian recharge out cluster` -> `Reversal - PEMBELIAN RECHARGE OUT CLUSTER`
- `fee pembelian recharge out cluster` main/fee groups become
  `Reversal - Recharge Out Cluster` and
  `Reversal - Recharge Out Cluster FEE`
- ST reversal labels still exist for detection, but all `Reversal - ST*`
  categories are intentionally unsupported for summary and should be
  unusual/excluded.

Fee validation:

- `RECHARGE` expects `RECHARGEFEE` debet total of `20`.
- `SELLTHRU` expects `SELLTHRUFEE` debet total of `100` and at least one
  `SELLTHRUSALESFEE` row.
- `Reversal - NGRS` expects `Reversal - NGRS FEE` kredit total of `20`.
- `Reversal - Recharge Out Cluster` expects its fee row kredit total of `20`.

Fee caps:

- Recharge-style fees are included only up to `20` per base transaction.
- Sellthru fee is included only up to `100` per base transaction.
- Fee rows beyond those caps are flagged unusual and excluded from summary.
- Fee totals below the expected amount are still included but flagged unusual.

Fee-only behavior:

- `RECHARGEFEE` without `RECHARGE`, including reversal recharge-style fee-only
  cases, is flagged unusual but included in summary.
- `SELLTHRUFEE` / `SELLTHRUSALESFEE` without `SELLTHRU` is flagged unusual and
  excluded from summary.

Unknown transaction and remark behavior:

- Any non-reversal transaction label outside the known FinPay summary
  categories is flagged with an `unknown transaction label` reason.
- Every known processed transaction label must also match one characterized
  remark structure. A mismatch is flagged with an
  `unknown remarks pattern for transaction` reason.
- Both cases are excluded from `summary_ready.parquet`; they cannot affect the
  persisted calculation rows or summary sheet.
- Remark matching is case-insensitive and replaces changing numeric values and
  `DD-MM-YYYY` dates with placeholders. Static words, punctuation, and order
  must still match. This permits different amounts, phone/cluster numbers, and
  dates without weakening the transaction-to-remark contract.

The current normalized remark vocabulary was characterized from all 60 local
cluster `411311` exports (74,532 rows):

| Processed transaction | Accepted normalized remark structure |
|---|---|
| `CASHOUT APOLLO` | `apollo mrc<value> <date>` |
| `QRISDUWIT` | `disburse qris duwit atas transaksi pembayaran qris pada tanggal <date> sejumlah rp <value>` |
| `DISBURSEMENT` | `sales fee payment` for voucher and/or perdana counts, followed by `sejumlah <value> rupiah` |
| `FeeTransaksi` | `fee digipos rp.<value> \| fee sbp rp.<value> \| dari nomor :<value>` |
| `RECHARGE` / `Reversal - NGRS` | `biaya pembelian recharge sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGEFEE` / `Reversal - NGRS FEE` | `platform fee recharge rp. <value>,-` |
| `PEMBELIAN RECHARGE OUT CLUSTER` / its reversal | `biaya pembelian recharge out cluster sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGE OUT CLUSTER` / its reversal | `fee pembelian recharge out cluster sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGE OUT CLUSTER FEE` / its reversal | `platform fee recharge rp. <value>,-` |
| `SELLTHRU` | `sellthru sales fee` or the standalone `transaksi sellthru` case |
| `SELLTHRUFEE` | `platform fee sellthru rp. <value>,-` or the observed `fee transaksi sellthru ... fee tsel ... fee finnet ...` structure |
| `SELLTHRUSALESFEE` | `sales hold transaksi sellthru sejumlah <value> rupiah, dari <value>` |

Do not silently map another new label or remark structure into an existing
category; characterize it from source data and add tests first.

Unusual rows must keep transaction `Remarks` unchanged. Extra explanation
belongs in `unusual_reason`.

## Dedup Rules

Dedup removes calculation duplicates after unusual detection. The duplicate
report should still show the original duplicate rows in unusual output.

`No` is not a stable duplicate key because duplicate transactions can have
different source row numbers. Existing dedup logic compares all dataframe
columns except `No`, with transaction date compared at minute precision.

## Postgres Rules

There are five pipeline-owned tables:

| Table | Source | Scope |
|---|---|---|
| `finpay_raw_transactions` | `integrity_checked.parquet` | Raw validated rows before relabeling and dedup. Duplicate source rows are retained. |
| `finpay_transactions` | `summary_ready.parquet` | Final calculation rows included in summary. Excluded unusual rows are not here. |
| `finpay_unusual_transactions` | `unusual.parquet` | Unusual rows from the pre-summary unusual detection phase and `unusual_reason`, including rows still included in summary. |
| `finpay_reversal_transactions` | `reversal.parquet` | Reversal detail output from the deduplicated dataframe. |
| `finpay_qrisduwit_transactions` | `qrisduwit.parquet` | QRISDUWIT detail output with `disbursement_date`. |

All DB writes replace the current `cluster_id + report_date` batch before
insert. This is the primary rerun/idempotency mechanism.

Schema rules:

- No table has a surrogate `id`.
- Source `No` is not persisted.
- Processed tables do not persist a `transaction` column because it duplicates
  `processed_transaction_label`.
- `raw_transaction_label` and `processed_transaction_label` values are
  lowercased only at the DB write boundary; in-memory workflow labels keep
  their original case.
- Every table includes `base_id`.
- Every table except `finpay_qrisduwit_transactions` includes
  `transaction_id_type`.
- `transaction_id_type` is `MAIN` for base transaction IDs and otherwise the
  trailing suffix such as `FEE`, `SLSFEE`, or `SALESFEE`.
- Existing pipeline-owned tables are reconciled on write, so obsolete columns
  from old schemas are removed.
- Treat that automatic reconciliation as legacy runtime behavior. Do not add new
  implicit schema changes to ordinary writes; plan new tables or columns as an
  explicit migration with compatibility, backfill, and rollback decisions.

## Google Sheets Outputs

Target spreadsheet name from the active workflow cluster config:
`Salinan dari MONITORING FINPAY`.

Each configured cluster has four stable FinPay sheets:

```text
<base worksheet>
<base worksheet> - Unusual
<base worksheet> - QRISDUWIT
<base worksheet> - Reversal
```

The same spreadsheet also has one stable shared worksheet named `LinkAja`.
FinPay owns the invoice-side integration: `process_daily_upload` creates the
worksheet when missing and initializes or repairs its two-row, per-cluster
reference headers before appending the invoice report. This ordering is a
required contract because invoice formulas referencing a missing `LinkAja`
worksheet fail. LinkAja business calculations and source population remain
outside the FinPay calculation modules.

All sheet writers use `sheets_common.open_or_create_finpay_spreadsheet`. If the
service account creates the spreadsheet, code attempts to set locale/timezone,
disable editor sharing, share configured writer emails, and remove public/domain
permissions. If the service account does not own an existing spreadsheet, Drive
permission sync is intentionally limited.

The workflow appends daily summary blocks to the same cluster summary sheet
across month boundaries. It does not create month-suffixed sheets and does not
auto-hide older worksheets.

All Google Sheets writers uppercase literal text cells immediately before
`ws.update(...)`. Formulas and numeric values are preserved; formula-generated
status text should be authored uppercase.

### Main Summary Sheet

Current summary columns are:

```text
A REPORT DATE
B SECTION
C KETERANGAN
D DEBET
E KREDIT
F SALDO
G spacer
H REPORT DATE
I INVOICE REPORT
J DEBET
K KREDIT
```

Opening balance uses the previous-month-end date from the uploaded file date.
Its `SECTION` cell is intentionally blank.

Daily cash-flow sections are written in columns `A:F` in this order:

1. `DETAIL`
2. `MANDIRI`
3. `Cash`

Invoice reports are written as one compact panel in columns `H:K`, starting on
the same row as the first `DETAIL` row:

- Column `H` repeats the invoice report date for filterability.
- `CASH IN - NGRS` is the first mini-table.
- QRISDUWIT disbursement-date rows are a subpart inside
  `CASH IN - NGRS`, not a separate mini-table.
- `SELLTHRU` is below Cash In.
- `ACCOUNTING` is the final mini-table.
- Invoice header and data rows stay unmerged so date filtering still works.
- Contiguous fully blank invoice-side `H:K` blocks are merged vertically and
  bordered as blank space.
- The opening balance/saldo initialization row is not merged in the invoice
  area; invoice-side merges are limited to inserted daily date blocks.

Cash In / Invoice NGRS report rows:

- `NGRS`
- `Recharge Fee`
- `Pembelian Recharge Out Cluster`
- `Expected Biaya Pembelian Recharge Out Cluster FEE`
- `LINKAJA EXPECTED RECHARGE OUT CLUSTER FEE`
- `LINKAJA DIGIPOS B2B TRANSFER IN CLUSTER FEE`
- `QRISDUWIT` parent row when QRISDUWIT rows exist

QRISDUWIT Cash In subrows:

- One row per `DISBURSEMENT DATE`, sorted ascending.
- The row label is `QRISDUWIT - <DISBURSEMENT DATE>`.
- Column `J` uses the grouped source `KREDIT` amount as invoice debit from
  the company perspective; column `K` is blank.
- Missing/blank disbursement dates are grouped under
  `QRISDUWIT - MISSING DISBURSEMENT DATE`.
- The `QRISDUWIT` parent row uses the same fill, bold text, and centered
  alignment as the `CASH IN - NGRS` header row.

Additional rows currently live at the bottom of the `DETAIL` section:

- `Jumlah Pembelian Recharge Out Cluster`
- `Expected Biaya Pembelian Recharge Out Cluster FEE`
- `Jumlah Reversal - PEMBELIAN RECHARGE OUT CLUSTER`

Sellthru rows:

- `ST`
- `BIAYA FEE ST`
- `BIAYA FEE BAR A ST (HOLD)`

Accounting rows:

- `PPOB`
- `DISBURSEMENT`
- `Recharge Out Cluster`
- `Recharge Out Cluster FEE`
- `Reversal - Recharge Out Cluster`
- `Reversal - Recharge Out Cluster FEE`

`Jumlah Pembelian Recharge Out Cluster` and
`Jumlah Reversal - PEMBELIAN RECHARGE OUT CLUSTER` are counts. The matching
`Expected Biaya Pembelian Recharge Out Cluster FEE` row is calculated as the
normal pembelian count multiplied by `200`. This is intentionally different
from the recharge-style fee validation cap of `20`.

`MANDIRI` footer rows include `PEMBELIAN RECHARGE OUT CLUSTER` in the `NGRS`
formula and include a separate `Reversal - Pembelian Recharge Out Cluster` row.

`MANDIRI` rows compute the final net summary totals. `Cash` rows contain:

- `RUNNING TOTAL`
- `MANDIRI`
- `SELISIH`

`MANDIRI` input is column `D` (`DEBET`). The formula defaults from the next
`TRANSFER MASUK DARI FINPAY` value, but the cell can be manually overwritten.
Protection leaves only Mandiri input cells editable unless
`FINPAY_MANDIRI_EDITOR_EMAILS` restricts them to a subset.

### Sheet Formatting

- Header row is frozen on every output worksheet.
- `KETERANGAN` column `C` and invoice label column `I` are set to `410px`,
  wide enough for `EXPECTED BIAYA PEMBELIAN RECHARGE OUT CLUSTER FEE`.
- Invoice date column `H` is compact. Invoice label column `I` is intentionally
  wide and clipped instead of wrapped so long labels stay on one row.
- The workflow clears shared basic filters; users should use temporary filter
  views for personal filtering.
- Summary sheet uses full cell borders across the written range. Detail and
  unusual output sheets use horizontal borders plus zebra-striped data rows.
- Light grid borders are applied across normal summary rows.
- Summary `DETAIL` rows use subtle alternating row backgrounds. On the first
  detail row, `A:B` uses the stronger section header fill while `C:F` keeps the
  light stripe.
- Summary sheet keeps strong top borders at starts of `DETAIL` and `MANDIRI`.
  The `DETAIL` start border is reapplied on both cash-flow columns `A:F` and
  invoice columns `H:K` because later appends can otherwise weaken the invoice
  report top edge.
- Each summary append reapplies strong top borders for current and existing
  daily `DETAIL` starts, because normal grid borders on later appends can
  otherwise overwrite the first block's top border.
- `Cash` section start intentionally has no full-row strong top border.
- `Total` and `SELISIH` strong top borders apply only to column `D`.
- QRISDUWIT, Reversal, and Unusual sheets keep a strong top border on the first
  data row of each report-date block.
- QRISDUWIT, Reversal, and Unusual sheets use one Google Sheets banded range for
  zebra striping with a stronger pale-blue stripe than the summary detail
  section. Writers delete existing banded ranges before adding the current one
  to avoid stacking banding rules on reruns.
- Avoid merged cells in data rows because they make filtering and reruns harder.
  The summary invoice panel intentionally merges only contiguous fully blank
  invoice-side `H:K` blocks; invoice header and data rows stay unmerged.

### Detail And Unusual Sheets

QRISDUWIT/Reversal detail sheets are written by the generic detail writer.
QRISDUWIT includes `DISBURSEMENT DATE`; Reversal does not.

`Nomor RS` is written/formatted as text to avoid integer conversion.

Unusual sheet columns include `Remarks` and separate `UNUSUAL REASON`; both are
rendered uppercase by the sheet writer. Do not overwrite transaction remarks
with diagnostic text before the sheet write.

## Google Sheets Protection And Access

Generated worksheet ranges are protected after each write. Protection editors
come from `FINPAY_PROTECTION_EDITOR_EMAILS` plus the authenticated service
account. `FINPAY_MANDIRI_EDITOR_EMAILS` controls who can edit Mandiri cells
when set.

## Row Replacement And Capacity

Sheet writers replace existing rows for the same report date instead of
skipping. This matters for reruns after logic changes.

Before writing near the grid boundary, writers call `ensure_row_capacity`.
Summary writes must account for the future-transfer lookup range used by
Mandiri formulas. Detail and unusual writers add buffer rows around their
explicit output range.

## Current Workflow Concurrency

The workflow uses `Sequential` blocks around sheet writes. DB writes and sheet
writes are near each other, but do not assume Google Sheets tasks are safe to
parallelize. The current design prioritizes reliability over runtime.

That sequencing applies only inside one execution. Until a tested external lock
or Kestra concurrency policy exists, queue or reject overlapping executions for
the same spreadsheet, worksheet, and report date.

Telegram unusual notification uses an HTTP request task with exponential retry.
Google Sheets upload tasks also use exponential retry.

For both categories the current retry policy starts at 15 seconds, caps at 60
seconds, and allows three attempts. PostgreSQL tasks do not declare an explicit
Kestra retry policy. The flow-level error task currently writes execution details
to logs only; it does not send a production failure alert. Treat retry,
notification, and same-date concurrency controls as required release work before
increasing execution frequency or automating file ingestion.

## Testing Checklist

Run the dependency-free workflow contract checks on any host Python. Run FinPay
behavior tests with Python 3.11 and all dependencies from `requirements.txt`:

```bash
python3 -m unittest -v tests.test_finpay_workflow_contracts
docker run --rm \
  -e PYTHONPYCACHEPREFIX=/tmp/finpay-pycache \
  -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 \
  python -m unittest -v tests.test_refactor_contracts
git diff --check
git diff --cached --check
```

The FinPay behavior suite intentionally fails import instead of skipping when
`pandas`, `gspread`, `psycopg`, or `pandera` is unavailable. A missing runtime
dependency or zero executed FinPay behavior tests is a failed validation.

When dependencies or Docker-copied source files change, rebuild:

```bash
docker build -t finpay-pipeline:3.11 .
```

Keep the root `.dockerignore` protections in place before building. When the
workflow changes, also validate it against the same Kestra version deployed
on-premises and smoke-test all `from pipeline import ...` symbols in the image.
The repository provides a secret-free, in-memory local validator configuration:

```bash
docker run --rm \
  -v "$PWD/finpay_pipeline.yml:/flows/finpay_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml
```

When behavior changes, prefer testing with real files from `data/` or local
sample files already used in this project. Avoid committing sample data or
secrets.

## Common Pitfalls

- Updating a helper but not exporting it from `finpay_pipeline/__init__.py` when
  Kestra imports it.
- Changing sheet columns without updating formulas, protection ranges, DB
  schema docs, and replacement logic.
- Adding shared basic filters back into sheets. Temporary filter views are the
  intended user workflow.
- Treating all unusual rows as excluded. Some unusual rows are intentionally
  included in summary.
- Assuming `finpay_transactions` contains all processed rows. It only contains
  summary-included rows.
- Persisting source `No` or adding surrogate IDs to FinPay tables.
- Replacing transaction `Remarks` with diagnostic text. Use `unusual_reason`.
- Relabeling reversal rows again in downstream paths when preprocessed labels
  are already present.
- Reintroducing parallel sheet writes and hitting Sheets API rate limits.
