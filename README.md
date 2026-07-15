# FinPay Daily Pipeline

Kestra flow for processing daily FinPay exports. The pipeline loads one
CSV/XLS/XLSX file, validates the FinPay schema, flags unusual rows, persists
selected workflow outputs to Postgres, summarizes transactions, and writes
Google Sheets reports.

The flow is designed for manual runs with a file upload. Runtime dates use the
`Asia/Makassar` timezone.

---

## Current Flow

```text
Upload CSV/XLS/XLSX
      |
      v
[1] parse_and_resolve
      - extract cluster_id and file date from filename
      - resolve spreadsheet and base worksheet
      - compute starting_balance_date as the last day of the previous month
      |
      v
[2] determine_current_date
      - compute current run timestamp
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
[5] persist_raw_transactions_to_db
      - production only
      - write raw validated rows to finpay_raw_transactions
      |
      v
[6] preprocess_transaction_labels
      - preserve raw_transaction_label
      - relabel Reversal rows from Remarks
      - relabel Recharge Out Cluster groups
      - write preprocessed.parquet
      |
      v
[7] flag_unusual_transactions
      - run on preprocessed, pre-calculation-dedup data
      - flag duplicate rows before they are removed from calculations
      - validate fee rules and reversal rules on a deduped view
      - write unusual.parquet
      |
      v
[8] branch_after_unusual_flag
      +-- unusual outputs
      |     - persist_unusual_to_db
      |     - upload_unusual_to_sheets
      |     - notify_unusual_telegram when unusual rows exist
      |
      +-- downstream_processing_branch
            - deduplicate rows for calculations/details
            - export QRISDUWIT and REVERSAL detail rows
            - remove reversal/fee rows excluded from summary
            - summarize and upload the daily summary block
```

`dry_run=true` still runs validation, integrity checks, relabeling, unusual
detection, calculation deduplication, detail filtering, and summary generation.
Production side effects are skipped: Postgres writes, Google Sheets uploads,
and Telegram alerts.

---

## Project Structure

```text
kestra-mmpp-pipeline/
├── AGENTS.md                # Architecture and editing guide for coding agents
├── pipeline.py              # Kestra compatibility shim: re-exports finpay_pipeline
├── pipeline_refactored.py   # Backward-compatible shim for older local imports
├── finpay_pipeline/         # Split Python implementation modules
├── finpay_pipeline.yml      # Kestra flow definition
├── Dockerfile               # Builds finpay-pipeline:3.11
├── docker-compose.yml       # Kestra + PostgreSQL + pgAdmin local stack
├── requirements.txt         # Python dependencies for the Docker image
├── tests/                   # Regression tests for refactor contracts
├── README.md
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
- The service account shared as Editor on the target spreadsheet, unless the
  pipeline should create the spreadsheet itself
- The local Docker image `finpay-pipeline:3.11`
- A Kestra secret named `GCP_SA_KEY` containing the full service-account JSON
- FinPay database Kestra secrets
- Optional Telegram alerting secrets: `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`

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

FinPay pgAdmin UI:

```text
http://localhost:5050
```

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

The image installs `requirements.txt`, including `psycopg`, and copies
`pipeline.py`, `pipeline_refactored.py`, and the `finpay_pipeline/` package
into `/app`. Kestra tasks continue to import through `from pipeline import ...`.

### 4. Configure Kestra secrets

Create a Kestra secret named:

```text
GCP_SA_KEY
```

The value must be the full JSON body of the GCP service account key. Keep local
env/secret files out of git; `.env_encoded` is already ignored.

For the local open-source Docker Compose setup, `.env_encoded` must store
base64-encoded values with Kestra's environment secret prefix:

```text
SECRET_GCP_SA_KEY=<base64-encoded-service-account-json>
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

Do not include the `SECRET_` prefix inside `secret(...)`; the prefix is only
used in the environment variable name.

### 5. Configure Telegram alerts

Telegram alerts are optional. They are sent only when `dry_run=false` and
`flag_unusual_transactions` reports one or more unusual rows.

Add these secrets only if Telegram alerts should run:

```text
SECRET_TELEGRAM_BOT_TOKEN=<base64-encoded-value>
SECRET_TELEGRAM_CHAT_ID=<base64-encoded-value>
```

---

## Outputs

### Postgres tables

Production runs replace the current `cluster_id + report_date` batch in each
pipeline-owned table:

| Table | Source |
|---|---|
| `finpay_raw_transactions` | `integrity_checked.parquet` |
| `finpay_unusual_transactions` | `unusual.parquet` |
| `finpay_qrisduwit_transactions` | `qrisduwit.parquet` |
| `finpay_reversal_transactions` | `reversal.parquet` |
| `finpay_transactions` | `summary_ready.parquet` |

`finpay_transactions` contains only rows included in summary calculations.
Excluded unusual rows are not written there.

### Google Sheets

Target spreadsheet name from current cluster config: `FINPAY REPORT`.

Each configured cluster uses stable worksheet names:

```text
<base worksheet>
<base worksheet> - Unusual
<base worksheet> - QRISDUWIT
<base worksheet> - Reversal
```

All sheet writers now use the shared
`open_or_create_finpay_spreadsheet(...)` path. If the target spreadsheet is
missing, the service account creates it and attempts to apply locale,
timezone, sharing, and protection settings.

The summary worksheet uses:

- columns `A:F` for FinPay cash flow
- column `G` as a spacer
- columns `H:K` for a compact invoice report panel with Cash In - NGRS,
  QRISDUWIT disbursement-date subrows, Sellthru, and Accounting mini-tables

Detail and unusual sheets replace existing rows for the same report date on
rerun. Literal text cells are uppercased before writing; formulas and numeric
values are preserved. QRISDUWIT, Reversal, and Unusual sheets use zebra-striped
data rows for readability.

---

## Development Checks

Run syntax checks after editing Python:

```bash
python3 -m compileall -q pipeline.py pipeline_refactored.py finpay_pipeline tests
```

Run the regression tests in an environment with `requirements.txt` installed:

```bash
python3 -m unittest discover -s tests
```

In a bare host interpreter without project dependencies, tests that import
`pandas`/`gspread` may skip. The Docker image or a local virtualenv with
`requirements.txt` is the expected runtime environment.

Check whitespace before committing:

```bash
git diff --check
```

When dependencies or Docker-copied source files change, rebuild:

```bash
docker build -t finpay-pipeline:3.11 .
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

- Orchestration lives in `finpay_pipeline.yml`; reusable data and Google Sheets
  logic lives in `finpay_pipeline/`.
- `pipeline.py` keeps the Kestra import contract stable. Do not remove it
  unless every workflow import changes.
- `pipeline_refactored.py` remains for older local scripts/notebooks.
- The summary worksheet is stable per cluster and continues across month
  boundaries.
- Deduplication compares all columns except `No`, and normalizes
  `Transaction Date` to minute precision for duplicate detection.
- Transaction relabeling runs once in `preprocess_transaction_labels`.
  Downstream reversal summary/detail helpers trust `processed_transaction_label`
  when present and keep a fallback for raw legacy callers.
- Unusual detection intentionally runs before calculation dedup so duplicate
  rows can still be reported.
- Later summary-only unusual checks are not appended to the unusual sheet.
- ST reversal categories are intentionally unusual/excluded and are not
  summarized.
- Postgres `raw_transaction_label` and `processed_transaction_label` values are
  stored lowercase.
- Sheet writes run sequentially after unusual detection to reduce Google Sheets
  rate-limit pressure.
- Parquet files are used between Kestra tasks to avoid passing large datasets
  through variables.
- Google Sheets footer totals are live formulas, not Python-computed totals.
- Agent-specific architecture notes live in `AGENTS.md`.
