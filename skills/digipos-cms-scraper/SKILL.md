---
name: digipos-cms-scraper
description: Login to DigiPOS CMS (digipos-cms.finpay.id) via browser automation and download deposit monitoring data (Riwayat Saldo Transaksi) as Excel files with validated row counts.
---

# DigiPOS CMS Scraper (Browser Automation)

Login to DigiPOS CMS via browser automation and download deposit/transaction monitoring data for multiple users as Excel files. Prefer Excel over CSV because DigiPOS CSV can corrupt `Nomor RS` phone-like values into scientific notation.

## Overview

See also: `references/excel-export-nomor-rs.md` for why DigiPOS exports should use Excel instead of CSV to preserve `Nomor RS` phone-like values.
See also: `references/search-click-reliability.md` for the DigiPOS Search button pitfall where jQuery `.trigger('click')` reports 0 entries but real `page.click('#btn_search')` returns the correct table total.
See also: `references/slow-search-and-export-retry-2026-07.md` for slow DigiPOS backend/export retries, `statusCode=05`, `NaN` DataTables totals, and safe missing-cluster recovery before Kestra POST.
See also: `references/slow-digipos-search-and-export.md` for slow DigiPOS table loads, `statusCode=05` server-busy timeouts, modal/loading overlay retries, and recovery with missing-cluster-only downloads plus `--skip-download` Kestra posting.
See also: `references/slow-digipos-search-and-export-recovery.md` for slow DigiPOS table/search/export behavior where 0 entries, `NaN`, `statusCode=05`, loading overlays, or download timeouts should be treated as transient and recovered by retry/missing-cluster download.
See also: `references/server-busy-modal-retry.md` for DigiPOS backend `statusCode=05` timeout responses, `NaN` DataTables totals, and modal/loading overlay retry handling.

This skill uses Playwright browser automation (instead of API calls) to:
1. Navigate to the DigiPOS CMS login page
2. Fill credentials and trigger the jQuery-based login
3. Navigate to the deposit monitoring page
4. Set date range filters
5. Click Search to load data using a real Playwright click
6. Click the Excel export button (browser-native DataTables export)
7. Save the downloaded `.xlsx` file to the target directory with proper naming

This approach matches what users see in the browser while preserving `Nomor RS` values that DigiPOS CSV can corrupt into scientific notation.

## Target Website

- **URL**: https://digipos-cms.finpay.id/
- **Login page**: https://digipos-cms.finpay.id/login
- **Login endpoint**: `/login-post` (POST)
- **Data page**: https://digipos-cms.finpay.id/deposit/monitoring-riwayat
- **CSV export**: DataTables Buttons plugin (browser client-side export)

## Authentication Flow (Browser-Based)

### Step 1: Navigate to login page
```bash
GET https://digipos-cms.finpay.id/login
```

### Step 2: Fill credentials and trigger login
Since the site uses jQuery event handlers (not standard form submission), the browser automation script:
1. Fills the username/password fields using jQuery `.val()`
2. Triggers `.trigger('change')` events on both fields
3. Triggers a click on the Sign in button using jQuery `.trigger('click')`

### Step 3: Navigate to monitoring page
After successful login, the browser is redirected to `/home`, then we navigate to `/deposit/monitoring-riwayat` to access the data table.

## Required Users

| Username | cluster_id |
|----------|------------|
| 411311_A | 411311 |
| 421306_A | 421306 |
| 421307_A | 421307 |
| 421315_A | 421315 |
| 421318_A | 421318 |
| 421320_A | 421320 |

Use configured DigiPOS credential source or pass `--password` at runtime. Do not hard-code or log credentials.

## Date Range Selection

The script supports several date selection modes:

### Single Date
```bash
python3 scripts/download_digipos.py --date 2026-06-08
```

### Date Range
```bash
python3 scripts/download_digipos.py --start 2026-06-01 --end 2026-06-30
```

### Default (Yesterday)
```bash
python3 scripts/download_digipos.py
```

### Specific Users
```bash
python3 scripts/download_digipos.py --users 421306_A 421307_A --date 2026-06-08
```

## File Output

### Filename Format
```
finpay-{cluster_id}({DD-MM-YYYY}to{DD-MM-YYYY}).csv
```

### Example
```
finpay-421306(26-05-2026to26-05-2026).csv
```

### Output Directory
```
/home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox/
```

### Subdirectory Structure
The script creates subdirectories by `cluster_id` (username without `_A`):
```
finpay-inbox/
├── 411311/
│   └── finpay-411311(08-06-2026to08-06-2026).csv
├── 421306/
│   └── finpay-421306(08-06-2026to08-06-2026).csv
└── ...
```

### CSV Format (11 columns, matching browser export)

1. `No` — Row number (1-indexed)
2. `Transaction Date` — from `tanggal` field (format: `DD-MM-YYYY HH:mm:ss`)
3. `Transaction ID` — from `wstransferid` field
4. `Saldo Awal` — from `init_amount` field
5. `Kredit` — from `amount_in` field
6. `Debet` — from `amount_out` field
7. `Saldo Akhir` — from `last_amount` field
8. `Transaction Type` — from `transaction_type` field
9. `Transaction` — from `wstransfertype` field
10. `Nomor RS` — computed from `sumber1` (strip `490` prefix; use `-` when empty)
11. `Remarks` — from `wstransferdesc` field

## Workflow

### 1. Login Process
- Navigate to login page
- Use jQuery `.val().trigger('change')` to set credentials
- Trigger click on Sign in button using jQuery

### 2. Data Navigation + Total Entries Validation
- Navigate to `/deposit/monitoring-riwayat`
- Set visible date fields (`date1`, `date2`) with native input setter + `input`/`change` events
- Set hidden API date fields (`dateStart`, `dateEnd`) directly in `YYYY-MM-DD HH:mm:ss` format
- Click Search button with real Playwright `page.click('#btn_search')`; do **not** use jQuery `.trigger('click')` here
- Wait for DataTables redraw/network idle, then add a short stabilization wait before reading `#dataInput_info`
- Read **total entries shown under the table** from `#dataInput_info`, e.g. `Showing 1 to 10 of 3,111 entries`
- Treat `#dataInput_info` total entries as source of truth for that date
- At least one transaction must exist every day; 0 entries is an error/flaky web state, not valid no-data
- If table shows 0, refresh/reload and retry Search; if still bad, close page, relogin, and retry full attempt

### 3. Full Export
- Use Playwright's `expect_download()` to capture native browser Excel download (`.buttons-excel`), not CSV download
- Reason: DigiPOS CSV export can corrupt `Nomor RS` phone-number values into scientific notation like `8E+10`; Excel export preserves phone-like strings that start with `08...`
- Save `.xlsx` to the appropriate subdirectory (by cluster_id). If downstream still needs CSV, write the CSV copy from Excel rows, never from the DigiPOS CSV button. Use `--excel-only` for workflows that accept Excel directly and should not create CSV copies.
- Excel export may prepend title/filter rows; normalize by finding the real table header row (`No`, `Transaction Date`) before validation or CSV copy.
- Validate exported row count and target date before reporting success.
- Exported data row count must equal table total entries exactly; mismatch means retry/fail, never post. This is required for daily cron too: cron must not POST to Kestra unless Excel rows match DigiPOS CMS web table total entries for every cluster/date.
- Header-only/0-row export is invalid and must be deleted.
- Reject export if `Nomor RS` still contains scientific notation.
- Files are automatically named with the correct pattern.
- Details: see `references/excel-export-nomor-rs.md` for the `Nomor RS` Excel-export pitfall and validation recipe

## Configuration

### Python Script: `scripts/download_digipos.py`

```python
#!/usr/bin/env python3

import os, sys, time, argparse
from datetime import datetime, timedelta

# Default configuration
DEFAULT_USERS = ["411311_A", "421306_A", "421307_A", "421315_A", "421318_A", "421320_A"]
PASSWORD = os.environ.get("DIGIPOS_PASSWORD")  # or pass --password at runtime; never hard-code in docs/logs
BASE_URL = "https://digipos-cms.finpay.id"
OUTPUT_DIR = "/home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox"

# Can be overridden via command line arguments
```

### Command Line Options

| Option | Description |
|--------|-------------|
| `--users USER1 USER2 ...` | List of usernames to process (default: all 6 users) |
| `--password PASS` | Password from secure/runtime source; do not store in docs/logs |
| `--date DATE` | Single date in YYYY-MM-DD format (default: yesterday) |
| `--start DATE` | Start date in YYYY-MM-DD format |
| `--end DATE` | End date in YYYY-MM-DD format |
| `--output-dir DIR` | Output directory (default: /home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox) |
| `--headless` / `--no-headless` | Headless mode (default: headless) |

### Pitfalls and Solutions

#### ⚠️ CRITICAL: Partial Data Export (Most Common Issue)
**Symptom:** CSV file is much smaller than expected, contains only ~10 rows despite thousands of records.
**Cause:** DataTables CSV export only exports the *current page* by default (page length = 10).
**Fix:** Before clicking CSV, run:
```javascript
$('#dataInput').DataTable().page.len(-1).draw();
page.wait_for_load_state("networkidle");
```
This sets page length to maximum so all records are included in the export.

#### ⚠️ CRITICAL: Wrong Dates in Downloaded Data
**Symptom:** CSV contains today's date instead of the requested date range.
**Cause:** The page has TWO sets of date fields:
- **Visible fields** (`date1`/`date2`): Display format `DD Month YYYY HH:mm:ss` — only for UI display
- **Hidden fields** (`dateStart`/`dateEnd`): API format `YYYY-MM-DD HH:mm:ss` — what the Search button actually sends
**Fix:** Update BOTH sets of fields:
```javascript
// Visible fields (display)
$('input[name="date1"]').val('08 June 2026 00:00:00').trigger('change');
$('input[name="date2"]').val('08 June 2026 23:59:59').trigger('change');
// Hidden fields (API — these are what actually filter the data)
$('#dateStart').val('2026-06-08 00:00:00');
$('#dateEnd').val('2026-06-08 23:59:59');
```

#### ⚠️ CRITICAL: Search Shows 0 Entries But Browser Shows Data
**Symptom:** Script logs `Showing 0 to 0 of 0 entries` / `Silakan pilih filter terlebih dahulu untuk menampilkan data`, but manual website check shows thousands of entries for the same user/date.
**Cause:** DigiPOS UI can ignore synthetic jQuery click `$('#btn_search').trigger('click')` for Search. Date fields may be set correctly and POST body may include `startDate`/`endDate`, but table remains in initial no-filter state.
**Fix:** Use real Playwright click and wait before reading DataTables info:
```python
page.click('#btn_search')
page.wait_for_function("() => typeof $ !== 'undefined' && $.fn.DataTable.isDataTable('#dataInput')")
page.wait_for_function("() => { const t = $('#dataInput').DataTable(); return !t.processing || !t.processing(); }")
page.wait_for_load_state("networkidle")
page.wait_for_timeout(5000)
```
**Verification:** For `421306_A` on `2026-07-22`, real click showed `Showing 1 to 10 of 3,092 entries`; old jQuery trigger showed 0.

#### ⚠️ CRITICAL: Slow Search/Export Looks Like 0 Entries
**Symptom:** Script sees `Showing 0 to 0 of 0 entries`, `filtered from NaN total entries`, `statusCode=05`, `#loadingDialog` blocking clicks, or Excel download timeout. Manual web may work if user waits longer.
**Cause:** DigiPOS backend/search/export is slow or temporarily busy; DataTables footer can remain stale while `#loadingDialog` is still visible.
**Fix:** Wait for loading/modal disappearance and non-NaN footer up to 180s, treat server-busy/NaN/download timeout as transient, retry fresh login/page, and for partial success download missing clusters only before running `run_daily_v5_excel.py --date YYYY-MM-DD --skip-download`. See `references/slow-digipos-search-and-export-recovery.md`.

#### ⚠️ CRITICAL: DigiPOS Server Busy / Modal Overlay During Search
**Symptom:** Search returns `Showing 0 to 0 of 0 entries (filtered from NaN total entries)` or `Silakan pilih filter terlebih dahulu...`; browser click may later time out because `#loadingDialog` or `#modal-gagal` intercepts pointer events.

**Cause:** `/deposit/monitoring-riwayat-data` can return HTTP 200 with application error JSON, especially:
```json
{"statusCode":"05","statusDesc":"Server sedang sibuk, dimohon untuk mengulangi beberapa saat lagi (timeout)"}
```
This is a transient DigiPOS backend timeout, not valid no-data.

**Fix:** Capture the AJAX response during Search. Treat `statusCode == "05"`, timeout text, or DataTables `NaN` totals as retryable. Dismiss `#modal-gagal`, wait for `#loadingDialog` to hide, and retry with a fresh page/login if needed. Details: `references/server-busy-modal-retry.md`.

#### Login Issues
- **Incorrect password**: Use current configured credential from secure source; do not add extra punctuation not present in credential.
- **Login endpoint**: The form uses `/login-post`, not `/login`
- **jQuery trigger pattern**: Standard `page.fill()` and `page.click()` don't work; use jQuery `.val().trigger('change')` and `.trigger('click')`
- **Wait for `/home`**: After login, the site redirects to `/home` (NOT to `/deposit/monitoring-riwayat`). Wait for `**/home` then navigate manually.

#### Browser Navigation
- **Date selectors**: Use `input[name="date1"]` and `input[name="date2"]` — `startDate`/`endDate` do not exist on this page
- **Search button**: Use real Playwright `page.click('#btn_search')` (button has `id="btn_search"`, class `btn-red`). DigiPOS can ignore synthetic `$('#btn_search').trigger('click')`, leaving the table in `Silakan pilih filter terlebih dahulu...` state and reporting `Showing 0 to 0 of 0 entries` even when data exists.
- **CSV button**: Use `$('.buttons-csv').trigger('click')` (DataTables button class, NOT `button:contains("CSV")`)
- **Data loading**: Wait for DataTables initialization using `page.wait_for_function(() => $.fn.DataTable.isDataTable('#dataInput'))`
- **Download capture**: Use Playwright's `expect_download()` before clicking the CSV button

#### Session Management
- **Fresh page per user**: Create a new `page = context.new_page()` for each user. Reusing the same page across users causes session state contamination.
- **Row limits**: Maximum 20,000 rows per request. For larger date ranges, use `--start`/`--end` with `--chunk-days`.

#### File Management
- **Directory structure**: Scripts create subdirectories by `cluster_id`
- **File naming**: Automatic naming follows `finpay-{cluster_id}({date}to{date}).csv` pattern
- **CSV format**: Matches browser export exactly (11 columns, not API fields)

## Backfill Task: `finpay-backfill`

Reference: see `references/backfill-reliability-lessons.md` for strict no-skip backfill rules, table-footer-vs-CSV validation, stale/header-only CSV handling, chronological Kestra polling, and recovery patterns.
Reference: see `references/kestra-v3-cron-and-postonly.md` for the current v3 Kestra target, daily cron command, POST-only backfill procedure, and unexpected rerun diagnosis.
Reference: see `references/421315-fresh-redownload-v4-backfill-2026-06.md` for affected-cluster repair when cached CSVs produce wrong sheet/result format: remove affected cache, re-download from DigiPOS CMS, validate table totals, then POST to Kestra v4 chronologically.

Script:
```bash
python3 scripts/finpay_backfill.py
```

Purpose:
1. Fetch all cluster data from `2026-06-01` through yesterday by default.
2. For each cluster, download every date first.
3. Only after all dates for that cluster have validated CSVs, POST that cluster's CSVs to Kestra chronologically.
4. Wait for Kestra terminal state before posting the next CSV, so sheet writes remain chronological.
5. Retry failed Kestra executions for the same CSV (`--post-attempts`, default `3`) before failing the cluster.
6. Missing/header-only CSV is a hard failure: do not skip that date and do not post later dates for that cluster.
7. Fixed cluster order: `411311_A`, `421306_A`, `421307_A`, `421315_A`, `421318_A`, `421320_A`.

Default behavior posts to Kestra with `dry_run=false` (updates sheet/data):
```bash
python3 scripts/finpay_backfill.py
```

Preview commands only:
```bash
python3 scripts/finpay_backfill.py --command-dry-run
```

Download only, no Kestra post:
```bash
python3 scripts/finpay_backfill.py --no-post
```

Safer Kestra test mode:
```bash
python3 scripts/finpay_backfill.py --dry-run true
```

Target a non-default Kestra flow (current production target is v3):
```bash
python3 scripts/finpay_backfill.py \
  --kestra-url http://localhost:8080/api/v1/executions/finance.finpay/finpay_daily_pipeline_v3
```

Specific range:
```bash
python3 scripts/finpay_backfill.py --start 2026-06-01 --end 2026-06-09
```

## Usage Examples

### Basic Usage (Yesterday's data for all users)
```bash
python3 scripts/download_digipos.py
```

### Specific Users and Date
```bash
python3 scripts/download_digipos.py --users 421306_A 421307_A --date 2026-06-08
```

### Date Range
```bash
python3 scripts/download_digipos.py --start 2026-06-01 --end 2026-06-30
```

### Check Output
```bash
python3 scripts/download_digipos.py --users 411311_A
# Lists files after processing
```

## References

- `references/reliability-notes.md` — durable lessons from reliability testing: hidden date fields, DataTables full export, retry pattern, CSV validation, and Kestra handoff.

## Testing

The script is thoroughly tested for:

- Login functionality with corrected credentials
- Date filter application and data loading
- CSV download capture and file saving
- Proper subdirectory structure creation
- Error handling for various failure scenarios

## Integration with Kestra Pipeline

This skill can be integrated with the `kestra-finpay-pipeline` skill to automatically trigger Kestra pipeline executions with the downloaded CSV files:

1. Download CSV files using this script
2. Run the Kestra pipeline with the files in the inbox directory
3. Monitor pipeline execution status

## Technical Details

### Browser Configuration
- Uses Playwright with Chromium (headless mode)
- Each user gets a fresh browser context
- Download path is configured to the target directory

### CSRF Handling
- The login form uses a CSRF token that's dynamically loaded
- jQuery triggers are used to ensure proper form handling
- No manual token extraction needed

### DataTables Integration
- Waits for DataTables initialization before interaction
- Uses proper jQuery selectors for all UI interactions
- Handles potential race conditions during page load

## Troubleshooting

### Common Issues

1. **Login fails**: Verify password is `TselDigipos123` (no `!`)
2. **No data found**: Check date format and ensure data exists for that period
3. **Download doesn't start**: Ensure `accept_downloads=True` in Playwright context
4. **File not saved**: Verify output directory permissions

### Debug Output
The script provides detailed output for each user:
- Login status
- Records found
- File save status
- File sizes

This comprehensive logging helps identify issues quickly.

## Integration with Kestra Pipeline

This skill can be integrated with the `kestra-finpay-pipeline` skill (archived) to automatically trigger Kestra pipeline executions with the downloaded CSV files:

1. Download CSV files using this script → files land in `finpay-inbox/{cluster_id}/`
2. Run the Kestra pipeline with the files in the inbox directory
3. Monitor pipeline execution status

## References

- `references/site-elements.md` — Full DOM element selectors, date field formats, DataTables column order, and JS patterns
- `references/login-troubleshooting.md` — Password pitfalls, API vs browser session differences, jQuery vs native selectors