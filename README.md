# FinPay Daily Pipeline

Kestra flow for processing daily FinPay exports. The pipeline loads a CSV/XLS/XLSX file, validates the FinPay schema, flags unusual fee groups, summarizes transactions, and writes results to Google Sheets.

The flow is currently designed for manual runs with a file upload. Runtime dates use the `Asia/Makassar` timezone.

---

## Current Flow

```text
Upload CSV/XLS/XLSX
      |
      v
[1] parse_and_resolve
      - extract cluster_id and file date from filename
      - resolve spreadsheet and cluster base worksheet
      - compute starting_balance_date as the last day of the previous month
      |
      v
[2] determine_current_date
      - compute current run date
      - use stable cluster summary worksheet, e.g. PKY
      |
      v
[3] load_and_validate
      - load CSV/XLS/XLSX
      - auto-detect header row
      - coerce types
      - validate against FINPAY_SCHEMA
      |
      v
[4] validate_integrity
      - enforce Debet/Kredit mutual exclusivity
      |
      v
[5] preprocess_transaction_labels
      - relabel Reversal rows from Remarks
      - relabel Recharge Out Cluster groups
      - write preprocessed.parquet
      |
      v
[6] flag_unusual_transactions
      - run on preprocessed, pre-dedup data
      - flag duplicate rows before they are removed from calculations
      - validate fee rules and reversal rules on a deduped view
      - write unusual.parquet
      |
      v
[7] branch_after_unusual_flag
      +-- upload_unusual_to_sheets
      |     - writes to one unusual worksheet per cluster
      |
      +-- downstream_processing_branch
            - deduplicate rows for calculations/details
            - export QRISDUWIT and REVERSAL detail rows
            - remove reversal rows excluded from summary
            - summarize and upload the daily summary block
```

`dry_run=true` still runs validation, integrity checks, unusual detection, relabeling, and summary generation. Google Sheets uploads are skipped.

---

## Project Structure

```text
kestra-mmpp-pipeline/
├── pipeline.py              # Kestra compatibility shim: re-exports finpay_pipeline
├── pipeline_refactored.py   # Backward-compatible shim for older local imports
├── finpay_pipeline/         # Split Python implementation modules
├── finpay_pipeline.yml      # Kestra flow definition
├── Dockerfile               # Builds finpay-pipeline:3.11 and copies Python runtime files into /app
├── docker-compose.yml       # Kestra + PostgreSQL + pgAdmin local stack
├── requirements.txt         # Python dependencies for the Docker image
├── README.md
├── LLM_CONTEXT.md           # Code/module map for future LLM-assisted changes
├── .gitignore
├── .env.example             # Safe local environment template
├── .env                     # Local Docker Compose env file, ignored by git
├── .env_encoded.example     # Safe Kestra secret template
├── .env_encoded             # Local Kestra env/secret file, ignored by git
└── data/                    # Local sample/input data, ignored by git
```

---

## Prerequisites

- Docker and Docker Compose
- A GCP service account with Google Sheets API and Google Drive API enabled
- The service account shared as Editor on the target spreadsheet
- The local Docker image `finpay-pipeline:3.11`
- A Kestra secret named `GCP_SA_KEY` containing the full service-account JSON
- Optional Telegram alerting secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

---

## Setup

### 1. Configure local environment

Copy the safe template and fill in local-only values:

```bash
cd kestra-mmpp-pipeline
cp .env.example .env
```

The `.env` file is ignored by git. It provides Docker Compose values for:

- Kestra metadata database and basic-auth credentials
- FinPay Postgres container settings
- pgAdmin login

Kestra workflow values must be configured as base64-encoded secrets in
`.env_encoded`. Copy the safe template and replace each value with a real
base64-encoded value:

```bash
cp .env_encoded.example .env_encoded
printf '%s' 'real-secret-value' | base64 -w0
```

For local Compose, Kestra reads `.env_encoded` as environment variables with
the `SECRET_` prefix. The workflow references them with `secret(...)`, for
example `{{ secret('FINPAY_DB_PASSWORD') }}`.

### 2. Start Kestra

```bash
docker compose up -d
```

Kestra UI:

```text
http://localhost:8080
```

Use the Kestra basic-auth credentials configured in `.env`.

FinPay pgAdmin UI:

```text
http://localhost:5050
```

Use the pgAdmin credentials configured in `.env`.

Register the FinPay database server in pgAdmin with:

```text
Host: finpay-postgres
Port: 5432
Database: value of FINPAY_DB_NAME
Username: value of FINPAY_DB_USER
Password: value of FINPAY_DB_PASSWORD
```

From the host machine, FinPay Postgres is exposed at `localhost:5433`.

### 3. Build the pipeline image

```bash
docker build -t finpay-pipeline:3.11 .
```

The image installs `requirements.txt`, including `psycopg`, and copies `pipeline.py`, `pipeline_refactored.py`, and the `finpay_pipeline/` package into `/app`. Kestra tasks continue to import through `from pipeline import ...`.

### 4. Configure Google Sheets credentials

Create a Kestra secret named:

```text
GCP_SA_KEY
```

The value must be the full JSON body of the GCP service account key. Keep local env/secret files out of git; `.env_encoded` is already ignored.

For the local open-source Docker Compose setup, `.env_encoded` must store the base64-encoded value with Kestra's environment prefix:

```text
SECRET_GCP_SA_KEY=<base64-encoded-service-account-json>
```

The flow still references it as `{{ secret('GCP_SA_KEY') }}`. Do not include the `SECRET_` prefix inside `secret(...)`; the prefix is only used in the environment variable name.

The same pattern is used for FinPay database and Google Sheets workflow values:

```text
SECRET_FINPAY_DB_HOST=<base64-encoded-value>
SECRET_FINPAY_DB_PORT=<base64-encoded-value>
SECRET_FINPAY_DB_NAME=<base64-encoded-value>
SECRET_FINPAY_DB_USER=<base64-encoded-value>
SECRET_FINPAY_DB_PASSWORD=<base64-encoded-value>
SECRET_FINPAY_SPREADSHEET_WRITER_EMAILS=<base64-encoded-value>
SECRET_FINPAY_SPREADSHEET_LOCALE=<base64-encoded-value>
SECRET_FINPAY_SPREADSHEET_TIMEZONE=<base64-encoded-value>
SECRET_FINPAY_PROTECTION_EDITOR_EMAILS=<base64-encoded-value>
SECRET_FINPAY_MANDIRI_EDITOR_EMAILS=<base64-encoded-value>
```

### 3b. Configure Telegram unusual-row alerts

Telegram alerts are sent only when `dry_run=false` and `flag_unusual_transactions` finds at least one unusual row.

Create these Kestra secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

In local `.env_encoded`, store them as `SECRET_TELEGRAM_BOT_TOKEN` and `SECRET_TELEGRAM_CHAT_ID`. The flow references them as `{{ secret('TELEGRAM_BOT_TOKEN') }}` and `{{ secret('TELEGRAM_CHAT_ID') }}`.

The alert uses Telegram Bot API `sendMessage` through Kestra's `io.kestra.plugin.core.http.Request` task. The bot must be allowed to send messages to the target chat, group, or channel.

### 4. Deploy the flow

In the Kestra UI:

1. Go to `Flows`.
2. Create or update flow `finance.finpay.finpay_daily_pipeline_v4`.
3. Paste the contents of `finpay_pipeline.yml`.

---

## Running

### Via Kestra UI

1. Open `http://localhost:8080`.
2. Go to `Flows -> finance.finpay -> finpay_daily_pipeline_v4`.
3. Click `Execute`.
4. Upload the FinPay export file.
5. Set `dry_run=true` if you only want validation, unusual detection, and summary logs without sheet writes.

### Input filename format

The filename must match:

```text
finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).<csv|xlsx|xls>
```

Example:

```text
finpay-411311(04-06-2026to04-06-2026).csv
```

The first date in the filename becomes the file `iso_date` output. The summary worksheet is stable per cluster, while the starting balance date for a newly created summary sheet is the last day of the month before the uploaded file date.

---

## Cluster Configuration

All clusters write to the spreadsheet:

```text
MONITORING FINPAY
```

| Cluster ID | Summary worksheet | Unusual worksheet | QRISDUWIT worksheet | Reversal worksheet | Default starting balance |
|---|---|---|---|---|-------------------------:|
| 421306 | MRT | MRT - Unusual | MRT - QRISDUWIT | MRT - Reversal |                        0 |
| 421307 | TDR | TDR - Unusual | TDR - QRISDUWIT | TDR - Reversal |                        0 |
| 411311 | PKY | PKY - Unusual | PKY - QRISDUWIT | PKY - Reversal |                        0 |
| 421315 | BGI | BGI - Unusual | BGI - QRISDUWIT | BGI - Reversal |                        0 |
| 421318 | MRW | MRW - Unusual | MRW - QRISDUWIT | MRW - Reversal |                        0 |
| 421320 | TNT | TNT - Unusual | TNT - QRISDUWIT | TNT - Reversal |                        0 |

Each cluster uses exactly four stable worksheets:

```text
<base worksheet>
<base worksheet> - Unusual
<base worksheet> - QRISDUWIT
<base worksheet> - Reversal
```

The pipeline continues appending daily summary blocks to the same summary worksheet across month boundaries. The initial balance date is only used when creating or initializing a blank summary worksheet, and is computed as the last day of the previous month. For a first run on `2026-06-17`, the starting balance date is `2026-05-31`.

---

## Pipeline Tasks

| # | Task ID | Description |
|---|---|---|
| 1 | `parse_and_resolve` | Parses the filename, resolves cluster config, computes the previous-month-end `starting_balance_date`, and sets the unusual worksheet name. |
| 2 | `determine_current_date` | Computes the `Asia/Makassar` run date and stable summary worksheet name. |
| 3 | `load_and_validate` | Loads CSV/XLS/XLSX, auto-detects the header row, coerces dtypes, and validates `FINPAY_SCHEMA`. |
| 4 | `validate_integrity` | Ensures each row does not have both `Debet` and `Kredit` non-zero. |
| 5 | `persist_raw_transactions_to_db` | Replaces the `cluster_id + report_date` batch in `finpay_raw_transactions` before transaction labels are relabeled. Skipped on dry run. |
| 6 | `preprocess_transaction_labels` | Preserves `raw_transaction_label`, relabels Reversal rows and Recharge Out Cluster groups, adds `processed_transaction_label`, then writes `preprocessed.parquet`. |
| 7 | `flag_unusual_transactions` | Runs before calculation dedup on preprocessed rows, flags duplicates, fee-rule issues, and invalid reversal groups, then writes `unusual.parquet`. |
| 9 | `branch_after_unusual_flag` | Runs unusual DB/sheet/alerting work in parallel with the downstream calculation/detail path. |
| 9a | `persist_unusual_to_db` | Replaces the `cluster_id + report_date` batch in `finpay_unusual_transactions`. Skipped on dry run. |
| 9b | `upload_unusual_to_sheets` | Uploads unusual rows to the per-cluster unusual worksheet. Skipped on dry run. |
| 9c | `notify_unusual_telegram` | Sends a Telegram alert only when unusual rows exist and the run is not a dry run. |
| 9d | `downstream_processing_branch` | Deduplicates preprocessed rows, then runs QRISDUWIT detail, Reversal detail, and summary branches. |
| 9d.1 | `deduplicate_transactions` | Writes `deduplicated.parquet` for calculation/detail outputs using all columns except `No`, with `Transaction Date` compared at minute precision. |
| 9d.2 | `qrisduwit_upload_branch` | Filters `QRISDUWIT` rows, extracts `Disbursement Date`, persists them to `finpay_qrisduwit_transactions`, and uploads the detail rows. |
| 9d.3 | `reversal_upload_branch` | Exports Reversal detail rows, persists them to `finpay_reversal_transactions`, and uploads the detail rows. |
| 9d.4 | `summary_upload_branch` | Removes reversal rows excluded from summary, summarizes, and uploads summary. |
| 9d.4.1 | `prepare_summary_transactions` | Keeps invalid NGRS, Recharge Out Cluster, and Recharge-type fee-only groups in summary with unusual flags, and excludes unsupported ST, ST fee-only, ambiguous, or unclassified groups. |
| 9d.4.2 | `persist_transactions_to_db` | Replaces the `cluster_id + report_date` batch in `finpay_transactions` using `summary_ready.parquet`, after relabeling, deduplication, and summary-exclusion rules. Skipped on dry run. |
| 9d.4.3 | `summarize` | Aggregates `Sum_of_Kredit`, `Sum_of_Debet`, and `Transaction_Date` by `Transaction`. |
| 9d.4.4 | `upload_to_sheets` | Appends the formatted daily summary block to the stable summary worksheet. Skipped on dry run. |

On failure, `notify_on_failure` currently logs the flow ID, execution ID, and UI log path.

---

## Postgres Persistence

The workflow writes to a separate FinPay Postgres database, not Kestra's metadata database. All tables are shared across clusters and include:

```text
cluster_id
report_date
```

The pipeline replaces rows by `cluster_id + report_date` for each table before inserting the current batch. This keeps duplicate transaction rows in the raw table while making reruns idempotent.

Tables:

| Table | Written from | Notes |
|---|---|---|
| `finpay_raw_transactions` | `integrity_checked.parquet` | True raw FinPay rows after schema and debit/credit validation, before relabeling. Stores `raw_transaction_label`, not the processed `transaction_label`. |
| `finpay_transactions` | `summary_ready.parquet` | Stores the workflow dataframe after relabeling, deduplication, and summary-exclusion rules, excluding `No`. |
| `finpay_unusual_transactions` | `unusual.parquet` | Stores the workflow unusual dataframe, excluding `No`. |
| `finpay_reversal_transactions` | `reversal.parquet` | Stores the workflow Reversal detail dataframe, excluding `No`. |
| `finpay_qrisduwit_transactions` | `qrisduwit.parquet` | Stores the workflow QRISDUWIT detail dataframe, excluding `No`. |

For non-raw tables, database columns are generated from the workflow parquet schema by normalizing names for SQL, for example `Transaction Date` becomes `transaction_date`, `Nomor RS` becomes `nomor_rs`, and `Disbursement Date` becomes `disbursement_date`. The source `Transaction` column is not persisted because it duplicates `processed_transaction_label`. Every table includes derived `base_id`; all tables except QRISDUWIT also include `transaction_id_type`, which is `MAIN` for the main transaction ID and otherwise the suffix such as `FEE`, `SLSFEE`, or `SALESFEE`. The database schema intentionally does not create a surrogate `id` column and does not persist FinPay `No`, because `transaction_id` is the business identifier and duplicate rows can have different `No` values. Existing pipeline-owned tables are reconciled on write, so obsolete columns from older schemas such as `id`, `no`, `transaction`, and `created_at` are removed.

Current schema definitions:

```text
finpay_raw_transactions
cluster_id TEXT NOT NULL
report_date DATE NOT NULL
transaction_date TIMESTAMP
transaction_id TEXT
base_id TEXT
transaction_id_type TEXT
saldo_awal NUMERIC(18, 2)
kredit NUMERIC(18, 2)
debet NUMERIC(18, 2)
saldo_akhir NUMERIC(18, 2)
transaction_type TEXT
raw_transaction_label TEXT
nomor_rs TEXT
remarks TEXT

finpay_transactions
cluster_id TEXT NOT NULL
report_date DATE NOT NULL
transaction_date TIMESTAMP
transaction_id TEXT
base_id TEXT
transaction_id_type TEXT
saldo_awal NUMERIC(18, 2)
kredit NUMERIC(18, 2)
debet NUMERIC(18, 2)
saldo_akhir NUMERIC(18, 2)
transaction_type TEXT
raw_transaction_label TEXT
processed_transaction_label TEXT
nomor_rs TEXT
remarks TEXT

finpay_unusual_transactions
cluster_id TEXT NOT NULL
report_date DATE NOT NULL
transaction_date TIMESTAMP
transaction_id TEXT
base_id TEXT
transaction_id_type TEXT
saldo_awal NUMERIC(18, 2)
kredit NUMERIC(18, 2)
debet NUMERIC(18, 2)
saldo_akhir NUMERIC(18, 2)
transaction_type TEXT
raw_transaction_label TEXT
processed_transaction_label TEXT
nomor_rs TEXT
remarks TEXT
unusual_reason TEXT

finpay_reversal_transactions
cluster_id TEXT NOT NULL
report_date DATE NOT NULL
transaction_date TIMESTAMP
transaction_id TEXT
base_id TEXT
transaction_id_type TEXT
saldo_awal NUMERIC(18, 2)
kredit NUMERIC(18, 2)
debet NUMERIC(18, 2)
saldo_akhir NUMERIC(18, 2)
transaction_type TEXT
raw_transaction_label TEXT
processed_transaction_label TEXT
nomor_rs TEXT
remarks TEXT

finpay_qrisduwit_transactions
cluster_id TEXT NOT NULL
report_date DATE NOT NULL
transaction_date TIMESTAMP
transaction_id TEXT
base_id TEXT
saldo_awal NUMERIC(18, 2)
kredit NUMERIC(18, 2)
debet NUMERIC(18, 2)
saldo_akhir NUMERIC(18, 2)
transaction_type TEXT
raw_transaction_label TEXT
processed_transaction_label TEXT
nomor_rs TEXT
remarks TEXT
disbursement_date DATE
```

---

## Out-Cluster and Unusual Rules

The pipeline intentionally uses two different remark phrases for two different purposes:

| Purpose | Phrase | Behavior |
|---|---|---|
| Preprocessing relabeling | `fee pembelian recharge out cluster` | Before unusual detection, matching `RECHARGE` groups are relabeled to `RECHARGE OUT CLUSTER`, and matching fee rows are relabeled to `RECHARGE OUT CLUSTER FEE`. |
| Unusual exemption | `biaya pembelian recharge out cluster` | In unusual detection, matching `RECHARGE` rows are exempt from the missing `RECHARGEFEE` rule. |

Fee validation currently monitors:

| Transaction | Expected fee rows |
|---|---|
| `RECHARGE` | `RECHARGEFEE` with total `Debet == 20`, unless exempt by the unusual out-cluster remark. |
| `SELLTHRU` | `SELLTHRUFEE` with total `Debet == 100` and at least one `SELLTHRUSALESFEE` row. |

Known fee rows without their main transaction are also flagged as unusual. `RECHARGEFEE` without `RECHARGE` is included in summary and its `unusual_reason` ends with `included in summary`. `SELLTHRUFEE` / `SELLTHRUSALESFEE` without `SELLTHRU` is excluded from summary and its `unusual_reason` ends with `excluded from summary`.

Flagged rows include the original transaction rows plus:

```text
base_id
unusual_reason
```

Duplicate rows detected before calculation deduplication are also written to the unusual output. Their original `Remarks` value is preserved, and the kept row number plus minute-level duplicate key are written to `unusual_reason` with `excluded from summary`.

Reversal rows are classified for summary from `Remarks`:

| Summary category | Required remarks and Kredit rules |
|---|---|
| `Reversal - NGRS` | Main reversal rows with `Biaya Pembelian recharge`. Rows containing `biaya pembelian recharge out cluster` are exempt from the fee requirement. |
| `Reversal - NGRS FEE` | Fee rows with `Platform Fee Recharge Rp. 20,-`; total `Kredit` must be `20` unless the group is out-cluster exempt. |
| `Reversal - Recharge Out Cluster` | Main reversal rows with `Fee Pembelian recharge out cluster`. Invalid groups with this main row are still included in summary. |
| `Reversal - Recharge Out Cluster FEE` | Fee rows with `Platform Fee Recharge Rp. 20,-` in the same out-cluster reversal group; total `Kredit` must be `20`. |
| `Reversal - ST*` | Any ST reversal row is unusual-only and excluded from summary. This includes `Sellthru Sales Fee`, `Platform Fee Sellthru Rp. 100,-`, `Fee Transaksi Sellthru sejumlah 100 rupiah`, and `Sales Hold Transaksi Sellthru`. |

Invalid reversal groups are always written to the unusual output. If the group is `Reversal - NGRS`, `Reversal - NGRS FEE`, `Reversal - Recharge Out Cluster`, or `Reversal - Recharge Out Cluster FEE`, it is still transformed and included in the summary with a reason ending in `included in summary`, even when only the fee row exists. ST, ambiguous, or unclassified reversal groups are written with a reason ending in `excluded from summary` and are removed before summary aggregation.

---

## Google Sheets Outputs

### Summary worksheet

Target:

```text
<base worksheet>
```

Example:

```text
PKY
```

If the summary worksheet does not exist, it is created. If it is empty, the pipeline initializes:

- Header row: `TANGGAL`, `KETERANGAN`, `DEBET`, `KREDIT`, `SALDO`
- Opening balance row using the previous-month-end date
- Default starting balance from the cluster config

Each run appends a daily block. Duplicate-date guard checks column A for the formatted transaction date and skips if that date already exists.

Daily data rows are written in this fixed order:

| Label | Summary transaction key |
|---|---|
| TRANSFER MASUK DARI FINPAY | `CASHOUT APOLLO` |
| QRISDUWIT | `QRISDUWIT` |
| DISBURSEMENT | `DISBURSEMENT` |
| PPOB | `FeeTransaksi` |
| NGRS | `RECHARGE` |
| BIAYA FEE NGRS | `RECHARGEFEE` |
| RECHARGE OUT CLUSTER | `RECHARGE OUT CLUSTER` |
| RECHARGE OUT CLUSTER FEE | `RECHARGE OUT CLUSTER FEE` |
| REVERSAL NGRS | `Reversal - NGRS` |
| REVERSAL NGRS FEE | `Reversal - NGRS FEE` |
| REVERSAL RECHARGE OUT CLUSTER | `Reversal - Recharge Out Cluster` |
| REVERSAL RECHARGE OUT CLUSTER FEE | `Reversal - Recharge Out Cluster FEE` |
| ST | `SELLTHRU` |
| BIAYA FEE ST | `SELLTHRUFEE` |
| BIAYA FEE BAR A. ST | `SELLTHRUSALESFEE` |

Each daily block then writes three formula-based summary sections in this order:

1. `CASH IN TEAM REPORT`
2. `ACCOUNTING REPORT`
3. `FOOTER SUMMARY`

Cash In Team rows:

Cash In values are split across `DEBET` and `KREDIT`: positive net values are written in `DEBET`, and negative net values are written as positive amounts in `KREDIT`.

| Row | Formula meaning |
|---|---|
| NGRS | Net value of `RECHARGE`. |
| Recharge Fee | Net value of `RECHARGEFEE`. |
| Reversal - NGRS | Net value of `Reversal - NGRS`. |
| Reversal - NGRS FEE | Net value of `Reversal - NGRS FEE`. |
| QRISDUWIT | Net of `QRISDUWIT`. |

Accounting rows:

Accounting values use the same split layout as Cash In, so report rows do not show negative amounts.

| Row | Formula meaning |
|---|---|
| PPOB | Net of `FeeTransaksi`. |
| DISBURSEMENT | Net of `DISBURSEMENT`. |
| Recharge Out Cluster | Net value of `RECHARGE OUT CLUSTER`. |
| Recharge Out Cluster FEE | Net value of `RECHARGE OUT CLUSTER FEE`. |
| Reversal - Recharge Out Cluster | Net value of `Reversal - Recharge Out Cluster`. |
| Reversal - Recharge Out Cluster FEE | Net value of `Reversal - Recharge Out Cluster FEE`. |
| ST | Net value of `SELLTHRU`. |
| ST FEE | Net value of `SELLTHRUFEE`. |
| ST SLSFEE | Net value of `SELLTHRUSALESFEE`. |

Footer Summary rows:

| Footer | Formula meaning |
|---|---|
| NGRS | Net of `RECHARGE` minus `RECHARGEFEE`. |
| Recharge Out Cluster | Net of `RECHARGE OUT CLUSTER` minus `RECHARGE OUT CLUSTER FEE`. |
| Reversal - NGRS | Net of `Reversal - NGRS` minus `Reversal - NGRS FEE`. |
| Reversal - Recharge Out Cluster | Net of `Reversal - Recharge Out Cluster` minus `Reversal - Recharge Out Cluster FEE`. |
| PPOB | Net of `FeeTransaksi`. |
| ST | Net of `SELLTHRU`, `SELLTHRUFEE`, and `SELLTHRUSALESFEE`. |
| DISBURSEMENT | Net of `DISBURSEMENT`. |
| QRISDUWIT | Net of `QRISDUWIT`. |
| Total | Sum of the footer rows above. |
| RUNNING TOTAL | Carries unsettled footer totals forward until a next-day transfer value appears. It resets to the current `Total` after the previous block has a non-zero `MANDIRI` value. |

After `RUNNING TOTAL`, the block writes two reconciliation rows:

| Row | Behavior |
|---|---|
| MANDIRI | Editable input cell in column C. It defaults to the next block's `TRANSFER MASUK DARI FINPAY` value from column D, or `0` when the next block does not exist yet. Users can overwrite it manually. |
| SELISIH | Formula row where `SELISIH = MANDIRI - RUNNING TOTAL`; status shows `pending transfer`, `sesuai`, `lebih bayar`, or `kurang bayar`. |

Before appending rows, sheet writers expand the worksheet if needed so the
target range fits inside the sheet grid. Summary writes also include the
`MANDIRI` future-transfer lookup range in the capacity check and add a 200-row
buffer when expansion is needed. Detail and unusual sheet writes check their
explicit output range and add a 500-row buffer. The `MANDIRI` lookup skips blank
future transfer cells to tolerate partial writes after transient Google Sheets
errors.

The header row is frozen on each output worksheet; no separate dashboard range is written.

Drive-level ownership, sharing, and editor permission settings are managed by
the spreadsheet owner, not by the workflow. The workflow only writes values,
formats worksheets, and recreates worksheet protected ranges.

Generated sheet ranges are protected after each write. Summary sheet columns A:E are locked for all generated rows except `MANDIRI` cells in column C, which remain editable for users with spreadsheet editor access. QRISDUWIT, Reversal, and Unusual worksheets are locked across their generated used ranges.

Protection editors are recreated by the workflow on every sheet write.
Configure them in `.env_encoded` with `SECRET_FINPAY_PROTECTION_EDITOR_EMAILS`.
The workflow also auto-adds the authenticated service-account email to
protected ranges. If `SECRET_FINPAY_MANDIRI_EDITOR_EMAILS` decodes to a
comma-separated email list, the editable `MANDIRI` column C cells are also
protected so only those listed users and the protected-range editors can edit
them. If it decodes to a blank value, any invited spreadsheet editor can edit
`MANDIRI` column C cells.

### Unusual worksheet

Target:

```text
<base worksheet> - Unusual
```

Example:

```text
PKY - Unusual
```

The worksheet is created if missing. Blank sheets get formatted headers in row 1 and data from row 2. Sheets that already have the expected header append data only. If there are no unusual rows, no rows are appended. Duplicate-date guard checks whether the report date already exists in the unusual sheet.

Current unusual report columns:

| Column | Description |
|---|---|
| REPORT DATE | Date of the FinPay report being checked. |
| NO | Source row number. |
| TRANSACTION DATE | Source transaction timestamp. |
| TRANSACTION ID | Original transaction ID. |
| BASE ID | Grouping key used for fee validation. |
| TRANSACTION | Transaction label. |
| KREDIT | Source kredit value. |
| DEBET | Source debet value. |
| SALDO AWAL | Source starting balance. |
| SALDO AKHIR | Source ending balance. |
| NOMOR RS | Source RS number. |
| REMARKS | Source remarks text. |
| UNUSUAL REASON | Fee-rule validation reason. |

Formatting includes fixed column widths, bold colored headers, date and number formats, wrapped remarks/reason columns, and a top border on each appended daily block.

### QRISDUWIT and Reversal detail worksheets

Targets:

```text
<base worksheet> - QRISDUWIT
<base worksheet> - Reversal
```

Examples:

```text
PKY - QRISDUWIT
PKY - Reversal
```

Both detail exports run in parallel with `summary_upload_branch` after deduplication:

| Detail sheet | Transaction match |
|---|---|
| QRISDUWIT | `QRISDUWIT` |
| Reversal | Any preprocessed Reversal category plus unclassified raw `Reversal` rows |

The Reversal detail export uses the same remark-based categories as the summary transform, such as `Reversal - NGRS`, `Reversal - Recharge Out Cluster FEE`, and `Reversal - ST SELLTHRUFEE`. Rows whose remarks cannot be classified remain `Reversal`; invalid but classifiable rows still show their reversal category and are explained in the unusual sheet.

QRISDUWIT rows include an extra `DISBURSEMENT DATE` column derived from `Remarks` using the phrase `tanggal DD-MM-YYYY`. For example, this remark:

```text
Disburse Qris Duwit atas Transaksi pembayaran QRIS pada tanggal 04-06-2026 sejumlah Rp 180,00
```

produces:

```text
04/06/2026
```

Detail worksheets are created if missing. Blank sheets get formatted headers in row 1 and data from row 2. Duplicate-date guard checks whether the report date already exists in the detail sheet.

---

## Local Validation

From `kestra-mmpp-pipeline`, using the repo-level uv virtualenv:

```bash
source ../.venv/bin/activate
python -m py_compile pipeline.py pipeline_refactored.py finpay_pipeline/*.py
python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('finpay_pipeline.yml').read_text()); print('YAML OK')"
git diff --check -- Dockerfile finpay_pipeline.yml pipeline.py pipeline_refactored.py finpay_pipeline README.md LLM_CONTEXT.md
```

Example smoke test with a local data file:

```bash
source ../.venv/bin/activate
python - <<'PY'
from pipeline import (
    load_and_validate_schema,
    validate_debit_credit_integrity,
    preprocess_transaction_labels,
    drop_duplicate_rows_by_minute,
    flag_unusual_transactions,
    prepare_reversal_summary_transactions,
    prepare_reversal_detail_export,
    prepare_transaction_detail_export,
    summarize_by_transaction,
)

path = "data/finpay-411311(04-06-2026to04-06-2026).csv"
df = load_and_validate_schema(path)
integrity_checked = validate_debit_credit_integrity(df)
preprocessed = preprocess_transaction_labels(integrity_checked)
unusual = flag_unusual_transactions(preprocessed)
deduplicated = drop_duplicate_rows_by_minute(preprocessed)
summary_ready, reversal_unusual = prepare_reversal_summary_transactions(deduplicated)
qrisduwit = prepare_transaction_detail_export(deduplicated, "QRISDUWIT", include_disbursement_date=True)
reversal = prepare_reversal_detail_export(deduplicated)
summary = summarize_by_transaction(summary_ready)
print({
    "rows": len(integrity_checked),
    "preprocessed_rows": len(preprocessed),
    "deduplicated_rows": len(deduplicated),
    "reversal_unusual_rows": len(reversal_unusual),
    "summary_ready_rows": len(summary_ready),
    "unusual_rows": len(unusual),
    "qrisduwit_rows": len(qrisduwit),
    "reversal_rows": len(reversal),
    "summary_rows": len(summary),
})
PY
```

---

## Dependencies

| Package | Version | Purpose |
|---|---:|---|
| pandas | 2.2.2 | File loading, transformation, date parsing |
| pandera | 0.20.4 | Schema validation |
| polars | 0.20.31 | Available for high-performance transforms |
| gspread | 6.1.2 | Google Sheets API client |
| google-auth | 2.30.0 | GCP service-account authentication |
| pyarrow | latest | Parquet inter-task file transfer |
| openpyxl | latest | Excel `.xlsx` reading |
| kestra | latest | Kestra output SDK |
| psycopg | 3.2.3 | FinPay Postgres persistence |

---

## Design Notes

- Orchestration lives in `finpay_pipeline.yml`; reusable data and Google Sheets logic lives in `finpay_pipeline/`.
- The summary worksheet is stable per cluster and continues across month boundaries.
- The starting balance date is the last day of the previous month, computed at runtime.
- Deduplication compares all columns except `No`, and normalizes `Transaction Date` to minute precision for duplicate detection.
- Transaction relabeling runs before unusual detection, calculation deduplication, detail exports, and summary aggregation.
- Sheet writes run sequentially after unusual detection to reduce Google Sheets rate-limit pressure.
- Parquet files are used between Kestra tasks to avoid passing large datasets through variables.
- Google Sheets footer totals are live formulas, not Python-computed totals.
- Dry runs execute all compute steps and skip only Google Sheets writes.
