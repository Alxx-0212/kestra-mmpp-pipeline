# DigiPOS slow search/export retry lesson (2026-07)

## Trigger
Daily FinPay download/backfill for `2026-07-28` showed `Showing 0 to 0 of 0 entries` although manual web check worked.

## What actually happened
DigiPOS was slow/flaky, not empty:

- At ~5s after Search: footer still showed `Showing 0 to 0 of 0 entries`, `#loadingDialog` was still visible.
- At ~15s after Search: same page updated to real rows, e.g. `Showing 1 to 10 of 2,733 entries` for `421306_A`.
- Some AJAX responses returned `statusCode: "05"` / `Server sedang sibuk... (timeout)`.
- Some attempts found table entries but Excel download did not fire within 45s.
- Retry later succeeded and saved Excel with rows matching web totals.

## Durable scraper rule
Do not read `#dataInput_info` immediately after `networkidle` or a fixed 5s sleep. Wait until:

```text
#loadingDialog is hidden
#modal-gagal is hidden
#dataInput_info is not initial/no-filter text
#dataInput_info does not contain NaN
```

Use a long timeout (`180000ms` observed necessary). Treat `statusCode=05`, `NaN total entries`, visible failure modal, and Excel-download timeout as transient conditions. Retry with fresh login/page before failing.

## Recovery pattern
When full all-cluster run fails after partially successful downloads:

1. Do **not** POST to Kestra yet.
2. Inspect which target-date `.xlsx` files exist and recount data rows excluding title/header rows.
3. Re-run `download_digipos.py` only for missing clusters with higher `--max-attempts`.
4. After all 6 files exist, run daily v5 with `--skip-download` to validate + dry-run + production POST.

Example:

```bash
python3 /home/mmpp/.hermes/profiles/alex/skills/digipos-cms-scraper/scripts/download_digipos.py \
  --users 411311_A 421306_A 421307_A \
  --date 2026-07-28 \
  --output-dir /home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox \
  --headless \
  --max-attempts 8 \
  --excel-only

python3 /home/mmpp/.hermes/profiles/alex/skills/data-pipeline/kestra-finpay-pipeline/scripts/run_daily_v5_excel.py \
  --date 2026-07-28 \
  --skip-download
```

## Row count interpretation
DigiPOS Excel typically has:

```text
row 1: CMS DigiPOS - Riwayat Saldo
row 2: No | Transaction Date | Transaction ID | ...
row 3+: data rows
```

Validation counts only rows after actual `No | Transaction Date` header. It excludes both title row and header row. Web `totalEntries` must equal Excel data rows only.
