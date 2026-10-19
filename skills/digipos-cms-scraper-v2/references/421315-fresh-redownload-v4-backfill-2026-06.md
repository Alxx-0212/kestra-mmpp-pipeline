# 421315 fresh re-download + Kestra v4 backfill (2026-06)

Session lesson: when user reports wrong sheet/result format for a Finpay cluster, cached CSVs may be suspect even if prior local validation passed. For affected-cluster repair, do not keep retrying POST from cache; delete affected cluster CSVs, download fresh data from DigiPOS CMS, validate against CMS table totals, then POST to Kestra v4 chronologically.

## Applied pattern

1. Remove only affected cluster cache:
   ```bash
   rm -f /home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox/421315/finpay-421315*.csv
   ```
2. Confirm removal:
   ```bash
   find /home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox/421315 -maxdepth 1 -name 'finpay-421315*.csv' | wc -l
   ```
3. Re-download from DigiPOS CMS using browser scraper, not cache:
   ```bash
   python3 /home/mmpp/projects/kestra-mmpp-pipeline/tmp/download_digipos.py \
     --users 421315_A \
     --start 2026-06-01 \
     --end 2026-06-18 \
     --output-dir /home/mmpp/projects/kestra-mmpp-pipeline/tmp/kestra-wd/finpay-inbox \
     --max-attempts 5
   ```
4. Scraper must validate each downloaded CSV:
   - table footer total entries from `#dataInput_info`
   - CSV data rows equal table entries
   - all CSV transaction dates match requested date
   - header-only or 0-row CSV invalid
5. Only after all dates for cluster validate, POST to Kestra v4 one file at a time in chronological order and wait for terminal state before next POST.

## User expectation

If user says cache/result has issues, prefer fresh re-download over repeated POST-only reruns. Preserve chronological posting and fail loudly on any missing or mismatched date.
