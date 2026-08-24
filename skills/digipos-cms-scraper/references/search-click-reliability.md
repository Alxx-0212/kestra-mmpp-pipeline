# DigiPOS Search Click Reliability

## Session lesson

Date: 2026-07-24

Manual website check showed data for `421306_A` on `2026-07-22`:

```text
All:    Showing 1 to 10 of 3,092 entries
Debit:  Showing 1 to 10 of 1,829 entries
Credit: Showing 1 to 10 of 1,263 entries
```

Old automation still logged:

```text
Showing 0 to 0 of 0 entries
Silakan pilih filter terlebih dahulu untuk menampilkan data.
```

## Root cause

The script used synthetic jQuery click:

```python
page.evaluate("$('#btn_search').trigger('click')")
```

DigiPOS sometimes ignored this path or left table in initial no-filter state. Hidden date fields were correctly set and POST body included `startDate`/`endDate`, but DataTables still reported 0.

## Durable fix

Use real browser click for Search:

```python
page.click('#btn_search')
page.wait_for_function("() => typeof $ !== 'undefined' && $.fn.DataTable.isDataTable('#dataInput')")
page.wait_for_function("() => { const t = $('#dataInput').DataTable(); return !t.processing || !t.processing(); }")
page.wait_for_load_state("networkidle")
page.wait_for_timeout(5000)
```

Keep login behavior separate: login still needs jQuery value/change/click handling because that form uses jQuery handlers.

## Verification pattern

When script says 0 but user says table has data:

1. Login as affected user.
2. Navigate to `/deposit/monitoring-riwayat`.
3. Set both visible date fields and hidden API fields.
4. Use `page.click('#btn_search')`.
5. Read `#dataInput_info`.
6. Compare total shown there to exported Excel data rows.

If Excel row count differs from `#dataInput_info`, delete export and retry/fail. Never POST mismatched files to Kestra.
