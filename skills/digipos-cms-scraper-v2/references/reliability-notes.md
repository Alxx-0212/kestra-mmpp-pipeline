# DigiPOS CMS scraper reliability notes

Session learning from building reliable Finpay CSV extraction.

## Critical pitfalls

1. Browser CSV export can be partial.
   - DataTables CSV export may only include visible/current page rows.
   - Before clicking CSV, force all rows into export set:
     ```js
     $('#dataInput').DataTable().page.len(-1).draw()
     ```

2. Visible date fields are not enough.
   - `date1` / `date2` are display fields.
   - Hidden fields `dateStart` / `dateEnd` drive server-side filtering.
   - Set both visible and hidden fields.
   - Use native input setter plus `input` and `change` events for visible fields.

3. DataTables can briefly report `0` after search/redraw.
   - Do not trust first `recordsTotal` immediately after search.
   - Retry Search up to 3 times.
   - Wait for DataTables processing/network idle.

4. Download success must be validated.
   - After native browser download, parse CSV.
   - Validate row count is at least DataTables reported count.
   - Validate all `Transaction Date` values match target date.
   - Raise error if validation fails; never silently save bad data.

5. Use native browser download, not API reconstruction.
   - User compares against manual browser download.
   - Preferred path: Playwright click CSV button + `expect_download()`.
   - API reconstruction can differ from browser export.

## Known-good verification example

`411311_A` for `2026-06-09`:
- DataTables count: `1676`
- CSV rows validated: `1676`
- File size: `288,468 B`
- Date validation: all rows `09-06-2026`

## Kestra handoff

After CSV validation, trigger Kestra with one file:
```bash
python3 /home/mmpp/.hermes/profiles/alex/skills/data-pipeline/kestra-finpay-pipeline/scripts/trigger_kestra.py \
  --file "/path/to/finpay-411311(09-06-2026to09-06-2026).csv" \
  --dry-run false
```

Default backfill flow should process dates ascending, then fixed user order.
