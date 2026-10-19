# DigiPOS CMS server-busy / modal retry pitfall

## Trigger
During FinPay daily/backfill download, DigiPOS `/deposit/monitoring-riwayat-data` can return HTTP 200 with JSON business error instead of DataTables rows:

```json
{"statusCode":"05","statusDesc":"Server sedang sibuk, dimohon untuk mengulangi beberapa saat lagi (timeout)"}
```

Observed page state after this response:

```text
#dataInput_info: Showing 0 to 0 of 0 entries (filtered from NaN total entries)
#dataInput tbody: No matching records found
DataTables page.info(): recordsTotal=NaN, recordsDisplay=NaN
```

A visible failure/loading overlay may then block later clicks:

```text
#loadingDialog intercepts pointer events
#modal-gagal intercepts pointer events
```

## Correct diagnosis
This is not cache, Kestra, cron, or Excel validation. Date fields can be correct and Search can be a real `page.click('#btn_search')`; DigiPOS backend is timing out or showing an application-level failure modal.

## Required scraper behavior
1. Listen for `/deposit/monitoring-riwayat-data` response during Search.
2. Parse response body when possible.
3. If body contains `statusCode == "05"` or `statusDesc` mentions timeout/server busy, classify as transient `DigiPOS_SERVER_BUSY`.
4. If DataTables info contains `NaN`, classify as transient server-busy/bad-response, not valid zero data.
5. Close/dismiss `#modal-gagal` if visible before retry.
6. Wait for `#loadingDialog` hidden/detached before retrying or before next click.
7. Retry with fresh page/login when overlays or NaN state persist.
8. Never POST to Kestra unless every requested cluster has Excel rows exactly matching web table total entries.

## Useful probes
During debugging, capture actual AJAX response and page state:

```python
page.on('response', lambda r: print(r.status, r.text()[:500]) if 'monitoring-riwayat-data' in r.url else None)
# after search:
page.evaluate("""() => ({
  info: $('#dataInput_info').text().trim(),
  tbody: $('#dataInput tbody').text().trim().slice(0, 300),
  pi: $('#dataInput').DataTable().page.info(),
  modalVisible: $('#modal-gagal').is(':visible'),
  loadingVisible: $('#loadingDialog').is(':visible')
})""")
```

## User workflow preference
For daily/backfill operations, do not use stale cache when user asks for fresh processing. Delete target-date cache first, download fresh from DigiPOS, validate, dry-run Kestra v5, then production POST Kestra v5 sequentially.
