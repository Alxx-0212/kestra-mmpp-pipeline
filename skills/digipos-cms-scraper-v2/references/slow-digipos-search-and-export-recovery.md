# Slow DigiPOS search/export recovery

Session lesson: DigiPOS CMS can show `Showing 0 to 0 of 0 entries` for several seconds while `#loadingDialog` remains visible, then later update to real totals. Example: `421306_A` for `2026-07-28` showed 0 entries at 5s, then `Showing 1 to 10 of 2,733 entries` at 15s with `statusCode: 00`.

## Failure symptoms

- DataTables footer: `Showing 0 to 0 of 0 entries` while body says `Silakan pilih filter terlebih dahulu untuk menampilkan data.`
- Footer may show `filtered from NaN total entries` after a slow/failed AJAX response.
- AJAX response may be `{"statusCode":"05","statusDesc":"Server sedang sibuk, dimohon untuk mengulangi beberapa saat lagi (timeout)"}`.
- `#loadingDialog` or `#modal-gagal` can intercept clicks, causing Playwright `page.click('#btn_search')` timeout.
- Excel export can fail with `Timeout 45000ms exceeded while waiting for event "download"` even after table total is correct.

## Durable handling pattern

1. Use real click for Search: `page.click('#btn_search')`.
2. Do not trust DataTables after fixed `networkidle + 5s`; DigiPOS may still be loading.
3. Wait up to ~180s until:
   - `#loadingDialog` hidden
   - `#modal-gagal` hidden
   - `#dataInput_info` no longer has initial no-filter text
   - `#dataInput_info` does not contain `NaN`
4. Treat `statusCode == "05"`, `NaN`, visible failure modal, and download timeout as transient; retry fresh login/page.
5. If some clusters succeed and others fail, do recovery by downloading only missing clusters, then run daily runner with `--skip-download` to validate all files and POST to Kestra.

## Verification commands

After recovery downloads, validate all target files exist before `--skip-download` POST phase:

```bash
python3 /home/mmpp/.hermes/profiles/alex/skills/data-pipeline/kestra-finpay-pipeline/scripts/run_daily_v5_excel.py --date YYYY-MM-DD --skip-download
```

This performs validation, dry-run Kestra v5, then production POST Kestra v5 without re-downloading.
