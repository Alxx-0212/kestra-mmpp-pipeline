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
      - choose the stable cluster summary worksheet, e.g. PKY
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

Telegram alerts are sent only when `dry_run=false` and `flag_unuduplicated, "QRISDUWIT", include_disbursement_date=True)
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
- Postgres `raw_transaction_label` and `processed_transaction_label` values are stored lowercase.
- Sheet writes run sequentially after unusual detection to reduce Google Sheets rate-limit pressure.
- Google Sheets writes uppercase literal text cells while preserving formulas and numeric values.
- Parquet files are used between Kestra tasks to avoid passing large datasets through variables.
- Google Sheets footer totals are live formulas, not Python-computed totals.
- Dry runs execute all compute steps and skip only Google Sheets writes.
