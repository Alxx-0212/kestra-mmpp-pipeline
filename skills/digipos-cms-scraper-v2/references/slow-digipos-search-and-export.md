# Slow DigiPOS Search and Excel Export Handling

Use this when DigiPOS CMS manually shows data but scraper logs `Showing 0 to 0 of 0 entries`, `NaN total entries`, `Server sedang sibuk`, or Excel download timeouts.

## Durable findings

- DigiPOS may show initial footer `Showing 0 to 0 of 0 entries` while `#loadingDialog` is still visible. This is not final result.
- For 2026-07-28, `421306_A` showed 0 entries at 5s, then `Showing 1 to 10 of 2,733 entries` at 15s.
- AJAX may return `statusCode: "05"` with `statusDesc: "Server sedang sibuk, dimohon untuk mengulangi beberapa saat lagi (timeout)"`.
- Failure modal `#modal-gagal` and loading overlay `#loadingDialog` can intercept subsequent clicks.
- Excel export may also be slow: table can show valid total entries, but `.buttons-excel` download event may not fire within 45s. Retrying fresh login/page can succeed.

## Scraper requirements

1. Use real Playwright click for search:

```python
page.click('#btn_search')
```

Do not use:

```python
page.evaluate("$('#btn_search').trigger('click')")
```

2. Wait for slow table stabilization before reading total entries:

```python
page.wait_for_function("""() => {
    const loading = $('#loadingDialog').is(':visible');
    const modal = $('#modal-gagal').is(':visible');
    const info = $('#dataInput_info').text().trim();
    return !loading && !modal && info && !info.includes('Silakan pilih filter') && !info.includes('NaN');
}""", timeout=180000)
```

3. Treat these as transient retry conditions, not valid no-data:

```text
#loadingDialog still visible
#modal-gagal visible
Showing 0 to 0 ... while loading
(filtered from NaN total entries)
statusCode=05 server busy timeout
Excel download timeout after table total entries were valid
```

4. Save export only after all validations pass:

```text
web table total entries > 0
Excel data rows > 0
Excel date == requested date
Nomor RS not scientific notation
Excel data rows == web table total entries
```

## Recovery pattern

If full daily download fails halfway:

1. Inspect which `finpay-<cluster>(DD-MM-YYYYtoDD-MM-YYYY).xlsx` files exist.
2. Keep validated successful Excel files.
3. Retry only missing clusters with higher `--max-attempts`.
4. After all 6 exist, run daily runner with `--skip-download` to validate, dry-run, and production POST to Kestra.

Example:

```bash
python3 scripts/download_digipos.py \
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
