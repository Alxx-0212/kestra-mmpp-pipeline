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
├── pipeline_refactored.py   # Core data and Google Sheets functions
├── finpay_pipeline.yml      # Kestra flow definition
├── Dockerfile               # Builds finpay-pipeline:3.11 and copies pipeline.py into /app
├── docker-compose.yml       # Kestra + PostgreSQL local stack
├── requirements.txt         # Python dependencies for the Docker image
├── README.md
├── .gitignore
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

### 1. Start Kestra

```bash
cd kestra-mmpp-pipeline
docker compose up -d
```

Kestra UI:

```text
http://localhost:8080
```

Default credentials from `docker-compose.yml`:

```text
admin@kestra.io / Admin1234!
```

### 2. Build the pipeline image

```bash
docker build -t finpay-pipeline:3.11 .
```

The image installs `requirements.txt` and copies `pipeline_refactored.py` as `/app/pipeline.py`, which is what the Kestra Python tasks import.

### 3. Configure Google Sheets credentials

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

The first date in the filename becomes the file `iso_date` output. The summary worksheet is stable per cluster, while the starting balance date for a newly created summary sheet is based on the pipeline run date.

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
| 5 | `preprocess_transaction_labels` | Relabels Reversal rows and Recharge Out Cluster groups before unusual detection, then writes `preprocessed.parquet`. |
| 6 | `flag_unusual_transactions` | Runs before calculation dedup on preprocessed rows, flags duplicates, fee-rule issues, and invalid reversal groups, then writes `unusual.parquet`. |
| 7 | `branch_after_unusual_flag` | Runs unusual upload/alerting in parallel with the downstream calculation/detail path. |
| 7a | `upload_unusual_to_sheets` | Uploads unusual rows to the per-cluster unusual worksheet. Skipped on dry run. |
| 7b | `notify_unusual_telegram` | Sends a Telegram alert only when unusual rows exist and the run is not a dry run. |
| 7c | `downstream_processing_branch` | Deduplicates preprocessed rows, then runs QRISDUWIT detail, Reversal detail, and summary branches. |
| 7c.1 | `deduplicate_transactions` | Writes `deduplicated.parquet` for calculation/detail outputs using all columns except `No`, with `Transaction Date` compared at minute precision. |
| 7c.2 | `qrisduwit_upload_branch` | Filters `QRISDUWIT` rows, extracts `Disbursement Date` from `Remarks`, and uploads the detail rows. |
| 7c.3 | `reversal_upload_branch` | Exports rows already labeled as Reversal categories and any unclassified raw `Reversal` rows. |
| 7c.4 | `summary_upload_branch` | Removes reversal rows excluded from summary, summarizes, and uploads summary. |
| 7c.4.1 | `prepare_summary_transactions` | Keeps invalid main NGRS/ST groups in summary with unusual flags and excludes fee-only/ambiguous/unclassified reversal groups. |
| 7c.4.2 | `summarize` | Aggregates `Sum_of_Kredit`, `Sum_of_Debet`, and `Transaction_Date` by `Transaction`. |
| 7c.4.3 | `upload_to_sheets` | Appends the formatted daily summary block to the stable summary worksheet. Skipped on dry run. |

On failure, `notify_on_failure` currently logs the flow ID, execution ID, and UI log path.

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

Flagged rows include the original transaction rows plus:

```text
base_id
unusual_reason
```

Duplicate rows detected before calculation deduplication are also written to the unusual output. Their original `Remarks` value is preserved, and the kept row number plus minute-level duplicate key are written to `unusual_reason`.

Reversal rows are classified for summary from `Remarks`:

| Summary category | Required remarks and Kredit rules |
|---|---|
| `Reversal - NGRS` | Main reversal rows with `Biaya Pembelian recharge`. Rows with `Fee Pembelian recharge out cluster` are also classified as NGRS and still require the Rp 20 fee. Rows containing `biaya pembelian recharge out cluster` are exempt from the fee requirement. |
| `Reversal - NGRS FEE` | Fee rows with `Platform Fee Recharge Rp. 20,-`; total `Kredit` must be `20` unless the group is out-cluster exempt. |
| `Reversal - ST` | Main reversal rows with `Sellthru Sales Fee`. |
| `Reversal - ST SELLTHRUFEE` | Fee rows with `Platform Fee Sellthru Rp. 100,-` or `Fee Transaksi Sellthru sejumlah 100 rupiah`; total `Kredit` must be `100`. |
| `Reversal - ST SELLTHRUSALESFEE` | Sales hold rows with `Sales Hold Transaksi Sellthru`; total `Kredit` must be non-zero. |

Invalid reversal groups are always written to the unusual output. If the group has a main `Reversal - NGRS` or `Reversal - ST` row, it is still transformed and included in the summary with a reason ending in `included in summary`. Fee-only, ambiguous, or unclassified reversal groups are written with a reason ending in `excluded from summary` and are removed before summary aggregation.

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
| REVERSAL ST | `Reversal - ST` |
| REVERSAL ST SELLTHRUFEE | `Reversal - ST SELLTHRUFEE` |
| REVERSAL ST SELLTHRUSALESFEE | `Reversal - ST SELLTHRUSALESFEE` |
| ST | `SELLTHRU` |
| BIAYA FEE ST | `SELLTHRUFEE` |
| BIAYA FEE BAR A. ST | `SELLTHRUSALESFEE` |

Footer rows are formula-based:

| Footer | Formula meaning |
|---|---|
| NGRS | Net of `RECHARGE` minus `RECHARGEFEE`. |
| Recharge Out Cluster | Net of `RECHARGE OUT CLUSTER` minus `RECHARGE OUT CLUSTER FEE`. |
| Reversal - NGRS | Net of `Reversal - NGRS` minus `Reversal - NGRS FEE`. |
| PPOB | Net of `FeeTransaksi`. |
| ST | Net of `SELLTHRU`, `SELLTHRUFEE`, and `SELLTHRUSALESFEE`. |
| Reversal - ST | Net of `Reversal - ST` minus `Reversal - ST SELLTHRUFEE` and `Reversal - ST SELLTHRUSALESFEE`. |
| DISBURSEMENT | Net of `DISBURSEMENT`. |
| QRISDUWIT | Net of `QRISDUWIT`. |
| Total | Sum of the footer rows above. |

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

The Reversal detail export uses the same remark-based categories as the summary transform, such as `Reversal - NGRS`, `Reversal - NGRS FEE`, and `Reversal - ST SELLTHRUFEE`. Rows whose remarks cannot be classified remain `Reversal`; invalid but classifiable rows still show their reversal category and are explained in the unusual sheet.

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
python -m py_compile pipeline_refactored.py
python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('finpay_pipeline.yml').read_text()); print('YAML OK')"
git diff --check -- finpay_pipeline.yml pipeline_refactored.py README.md
```

Example smoke test with a local data file:

```bash
source ../.venv/bin/activate
python - <<'PY'
from pipeline_refactored import (
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

---

## Design Notes

- Orchestration lives in `finpay_pipeline.yml`; reusable data and Google Sheets logic lives in `pipeline_refactored.py`.
- The summary worksheet is stable per cluster and continues across month boundaries.
- The starting balance date is the last day of the previous month, computed at runtime.
- Deduplication compares all columns except `No`, and normalizes `Transaction Date` to minute precision for duplicate detection.
- Transaction relabeling runs before unusual detection, calculation deduplication, detail exports, and summary aggregation.
- Unusual upload, Telegram unusual-row alerting, and summary upload run in parallel after unusual detection.
- Parquet files are used between Kestra tasks to avoid passing large datasets through variables.
- Google Sheets footer totals are live formulas, not Python-computed totals.
- Dry runs execute all compute steps and skip only Google Sheets writes.
