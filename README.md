# FinPay Daily Pipeline

An automated daily data pipeline that ingests FinPay transaction exports (CSV or Excel), validates the data, and appends a formatted daily summary block to a Google Sheets monitoring spreadsheet. Orchestrated by [Kestra](https://kestra.io) running in Docker.

---

## Overview

```
Upload CSV/XLSX
      │
      ▼
[Task 1] Parse filename → extract cluster ID + date → resolve spreadsheet config
      │
      ▼
[Task 2] Load file → auto-detect header row → coerce dtypes → validate against schema
      │
      ▼
[Task 3] Debet/Kredit mutual-exclusivity check → add Amount column
      │
      ▼
[Task 4] Summarize by transaction type
      │
      ▼
[Task 5] Upload daily block to Google Sheets (skipped on dry run)
```

---

## Project Structure

```
files/
├── pipeline_refactored.py   # Core data logic (pure functions, no orchestration)
├── finpay_pipeline.yml      # Kestra flow definition (5 tasks + error handler)
├── Dockerfile               # Python 3.11-slim image — bakes pipeline.py into /app
├── docker-compose.yml       # Kestra + PostgreSQL stack
├── requirements.txt         # Python dependencies
├── .env_encoded             # Kestra encoded env config (not committed)
└── tmp/kestra-wd/
    ├── finpay-inbox/        # Drop-off folder for input files
    ├── finpay-archive/      # Processed files moved here after success
    └── secrets/
        └── gcp-sa-key.json  # GCP service account key (not committed)
```

---

## Prerequisites

- Docker + Docker Compose
- A GCP service account with **Google Sheets API** and **Google Drive API** enabled
- The service account must be shared as an **Editor** on the target spreadsheet
- The `finpay-pipeline:3.11` Docker image built and available to the Kestra Docker runner

---

## Setup

### 1. Start the Kestra stack

```bash
cd files/
docker compose up -d
```

Kestra UI will be available at `http://localhost:8080`.  
Default credentials: `admin@kestra.io` / `Admin1234!`

### 2. Build the pipeline image

```bash
docker build -t finpay-pipeline:3.11 .
```

The image bakes `pipeline_refactored.py` in as `/app/pipeline.py` so all Kestra tasks can import it directly.

### 3. Configure the GCP secret

In the Kestra UI, create a secret named `GCP_SA_KEY` containing the full JSON content of your GCP service account key file.

### 4. Deploy the flow

In the Kestra UI, create a new flow under namespace `finance.finpay` and paste the contents of `finpay_pipeline.yml`.

---

## Running the Pipeline

### Via Kestra UI

1. Open `http://localhost:8080`
2. Navigate to **Flows → finance.finpay → finpay_daily_pipeline**
3. Click **Execute**
4. Upload the FinPay export file
5. Toggle **Dry run** if you only want validation without writing to the sheet

### Input filename format

The filename must match the pattern:

```
finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).<csv|xlsx|xls>
```

Example: `finpay-421306(09-06-2026to09-06-2026).csv`

The `cluster_id` in the filename determines which worksheet to write to:

| Cluster ID | Worksheet |
|------------|-----------|
| 421306 | MRT |
| 421307 | TDR |
| 411311 | PKY |
| 421315 | BGI |
| 421318 | MRW |
| 421320 | TNT |

All clusters write to the **"MONITORING FINPAY"** spreadsheet.

---

## Pipeline Tasks

| # | Task ID | Description |
|---|---------|-------------|
| 1 | `parse_and_resolve` | Extracts `cluster_id` and date from filename; resolves spreadsheet/worksheet config |
| 2 | `load_and_validate` | Loads CSV or Excel with auto header-row detection; validates against FINPAY Pandera schema |
| 3 | `validate_integrity` | Ensures Debet and Kredit are mutually exclusive per row; adds `Amount` column |
| 4 | `summarize` | Groups by `Transaction` type and aggregates totals |
| 5 | `upload_to_sheets` | Appends a formatted daily block to the target Google Sheet (skipped on dry run) |

On any task failure, the `notify_on_failure` error handler logs the execution details (extend it with Slack / email / Teams as needed).

---

## Google Sheets Output Format

Each daily run appends two sections to the worksheet:

**Data rows** — one row per transaction type with kredit/debet values and a running balance formula in column E.

**Footer rows** — aggregated net values by category, calculated via Excel formulas referencing the data rows above:

| Label | Calculation |
|-------|-------------|
| NGRS | net(RECHARGE + RECHARGEFEE + Reversal) |
| PPOB | net(FeeTransaksi) |
| ST | net(SELLTHRU + SELLTHRUFEE + SELLTHRUSALESFEE) |
| DISBURSEMENT | net(DISBURSEMENT) |
| QRISDUWIT | net(QRISDUWIT) |
| Total | Sum of all footer lines |

A duplicate-date guard prevents the same date from being written twice.

---

## Python Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| pandas | 2.2.2 | DataFrame loading, transformation, date parsing |
| pandera | 0.20.4 | Schema validation |
| polars | 0.20.31 | Available for high-performance transforms |
| gspread | 6.1.2 | Google Sheets API client |
| google-auth | 2.30.0 | GCP service account authentication |
| pyarrow | latest | Parquet inter-task file transfer |
| openpyxl | latest | Excel (.xlsx) file reading |
| kestra | latest | Kestra output/variable SDK |

---

## Key Design Decisions

- **Pure function module** — `pipeline_refactored.py` contains only data logic. All orchestration lives in the Kestra YAML flow.
- **Auto header detection** — the loader scans the first 10 rows to find the real header regardless of leading metadata rows in the export.
- **Parquet inter-task handoff** — tasks exchange data via `.parquet` files rather than environment variables to handle large row counts efficiently.
- **Excel formulas for footer** — footer totals use spreadsheet formulas (not Python-computed values) so they stay correct if the sheet is manually edited.
- **Dry run flag** — task 5 is skipped entirely when `dry_run=true`, making validation safe to run in production.
