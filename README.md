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
      - compute monthly summary worksheet, e.g. PKY 2026-06
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
      - add Amount
      |
      v
[5] deduplicate_transactions
      - drop duplicate rows using all columns
      - compare Transaction Date at date + hour + minute precision only
      |
      v
[6] flag_unusual_transactions
      - run on pre-relabel data
      - write unusual.parquet
      |
      v
[7] branch_after_unusual_flag
      +-- upload_unusual_to_sheets
      |     - writes to one unusual worksheet per cluster
      |
      +-- summary_upload_branch
            - relabel out-cluster rows for summary only
            - summarize transaction totals
            - upload the daily summary block to the monthly worksheet
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

### 3b. Configure Telegram unusual-row alerts

Telegram alerts are sent only when `dry_run=false` and `flag_unusual_transactions` finds at least one unusual row.

Create these Kestra secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

The alert uses Telegram Bot API `sendMessage` through Kestra's `io.kestra.plugin.core.http.Request` task. The bot must be allowed to send messages to the target chat, group, or channel.

### 4. Deploy the flow

In the Kestra UI:

1. Go to `Flows`.
2. Create or update flow `finance.finpay.finpay_daily_pipeline`.
3. Paste the contents of `finpay_pipeline.yml`.

---

## Running

### Via Kestra UI

1. Open `http://localhost:8080`.
2. Go to `Flows -> finance.finpay -> finpay_daily_pipeline`.
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

The first date in the filename becomes the file `iso_date` output. Monthly worksheet selection and starting balance date are based on the pipeline run date, not the file date.

---

## Cluster Configuration

All clusters write to the spreadsheet:

```text
MONITORING FINPAY
```

| Cluster ID | Base worksheet | Summary worksheet example for June 2026 run | Unusual worksheet | Default starting balance |
|---|---|---|---|-------------------------:|
| 421306 | MRT | MRT 2026-06 | MRT - Unusual |                        0 |
| 421307 | TDR | TDR 2026-06 | TDR - Unusual |                        0 |
| 411311 | PKY | PKY 2026-06 | PKY - Unusual |                        0 |
| 421315 | BGI | BGI 2026-06 | BGI - Unusual |                        0 |
| 421318 | MRW | MRW 2026-06 | MRW - Unusual |                        0 |
| 421320 | TNT | TNT 2026-06 | TNT - Unusual |                        0 |

Summary worksheets are monthly and derived from the run month:

```text
<base worksheet> <YYYY-MM>
```

Unusual worksheets are stable per cluster:

```text
<base worksheet> - Unusual
```

The initial balance date for a new monthly summary worksheet is computed as the last day of the previous month. For a run on `2026-06-17`, the starting balance date is `2026-05-31`.

---

## Pipeline Tasks

| # | Task ID | Description |
|---|---|---|
| 1 | `parse_and_resolve` | Parses the filename, resolves cluster config, computes the previous-month-end `starting_balance_date`, and sets the unusual worksheet name. |
| 2 | `determine_current_date` | Computes the `Asia/Makassar` run date and monthly summary worksheet name. |
| 3 | `load_and_validate` | Loads CSV/XLS/XLSX, auto-detects the header row, coerces dtypes, and validates `FINPAY_SCHEMA`. |
| 4 | `validate_integrity` | Ensures each row does not have both `Debet` and `Kredit` non-zero; adds `Amount`. |
| 5 | `deduplicate_transactions` | Drops duplicate rows using all columns, with `Transaction Date` compared at minute precision. |
| 6 | `flag_unusual_transactions` | Runs fee-rule validation on deduplicated, pre-relabel data and writes `unusual.parquet`. |
| 7 | `branch_after_unusual_flag` | Runs unusual upload and summary upload path in parallel. |
| 7a | `upload_unusual_to_sheets` | Uploads unusual rows to the per-cluster unusual worksheet. Skipped on dry run. |
| 7b | `notify_unusual_telegram` | Sends a Telegram alert only when unusual rows exist and the run is not a dry run. |
| 7c | `summary_upload_branch` | Sequential branch for relabeling, summarizing, and uploading summary. |
| 7c.1 | `relabel_out_cluster` | Relabels summary-only out-cluster RECHARGE groups after deduplication. |
| 7c.2 | `summarize` | Aggregates `Sum_of_Kredit`, `Sum_of_Debet`, and `Transaction_Date` by `Transaction`. |
| 7c.3 | `upload_to_sheets` | Appends the formatted daily summary block to the monthly summary worksheet. Skipped on dry run. |

On failure, `notify_on_failure` currently logs the flow ID, execution ID, and UI log path.

---

## Out-Cluster and Unusual Rules

The pipeline intentionally uses two different remark phrases for two different purposes:

| Purpose | Phrase | Behavior |
|---|---|---|
| Summary relabeling | `fee pembelian recharge out cluster` | In the summary branch only, matching `RECHARGE` groups are relabeled to `RECHARGE OUT CLUSTER`, and matching fee rows are relabeled to `RECHARGE OUT CLUSTER FEE`. |
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

---

## Google Sheets Outputs

### Monthly summary worksheet

Target:

```text
<base worksheet> <YYYY-MM>
```

Example:

```text
PKY 2026-06
```

If the monthly worksheet does not exist, it is created. If it is empty, the pipeline initializes:

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
| REVERSAL | `Reversal` |
| ST | `SELLTHRU` |
| BIAYA FEE ST | `SELLTHRUFEE` |
| BIAYA FEE BAR A. ST | `SELLTHRUSALESFEE` |

Footer rows are formula-based:

| Footer | Formula meaning |
|---|---|
| NGRS | Net of `RECHARGE`, `RECHARGEFEE`, `RECHARGE OUT CLUSTER`, `RECHARGE OUT CLUSTER FEE`, and `Reversal`. |
| PPOB | Net of `FeeTransaksi`. |
| ST | Net of `SELLTHRU`, `SELLTHRUFEE`, and `SELLTHRUSALESFEE`. |
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
| KREDIT | Source kredit amount. |
| DEBET | Source debet amount. |
| AMOUNT | Computed `Kredit - Debet`. |
| SALDO AWAL | Source starting balance. |
| SALDO AKHIR | Source ending balance. |
| NOMOR RS | Source RS number. |
| REMARKS | Source remarks text. |
| UNUSUAL REASON | Fee-rule validation reason. |

Formatting includes fixed column widths, bold colored headers, date and number formats, wrapped remarks/reason columns, and a top border on each appended daily block.

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
    validate_and_add_amount,
    drop_duplicate_rows_by_minute,
    flag_unusual_transactions,
    relabel_out_cluster_transactions,
    summarize_by_transaction,
)

path = "data/finpay-411311(04-06-2026to04-06-2026).csv"
df = load_and_validate_schema(path)
with_amount = validate_and_add_amount(df)
deduplicated = drop_duplicate_rows_by_minute(with_amount)
unusual = flag_unusual_transactions(deduplicated)
relabeled = relabel_out_cluster_transactions(deduplicated)
summary = summarize_by_transaction(relabeled)
print({
    "rows": len(with_amount),
    "deduplicated_rows": len(deduplicated),
    "unusual_rows": len(unusual),
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
- The summary monthly worksheet is based on the pipeline run month, not the uploaded file date.
- The starting balance date is the last day of the previous month, computed at runtime.
- Deduplication compares all columns, but normalizes `Transaction Date` to minute precision for duplicate detection.
- Unusual detection runs before summary relabeling so fee checks see the original transaction labels.
- Unusual upload, Telegram unusual-row alerting, and summary upload run in parallel after unusual detection.
- Parquet files are used between Kestra tasks to avoid passing large datasets through variables.
- Google Sheets footer totals are live formulas, not Python-computed totals.
- Dry runs execute all compute steps and skip only Google Sheets writes.
