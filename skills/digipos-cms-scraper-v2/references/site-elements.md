# DigiPOS CMS — Site Element Reference

Live-tested DOM structure for `https://digipos-cms.finpay.id/deposit/monitoring-riwayat`.

## Login Page (`/login`)

| Element | Selector | Notes |
|---------|----------|-------|
| Username input | `#username` | Also `input[placeholder="Masukan username Anda"]` |
| Password input | `#password` | Also `input[placeholder="Masukan password anda"]` |
| Sign in button | `button:contains("Sign in")` (jQuery) | Has `type="button"`, click handler via jQuery |
| CSRF token | `input[name="_token"]` | Multiple hidden `_token` inputs on page |

**Login flow:** POST to `/login-post` with `_token`, `username`, `password` → JSON response → redirect to `/home`.

## Monitoring Page (`/deposit/monitoring-riwayat`)

### Date Filter Fields

| Element | ID | Name | Format | Purpose |
|---------|----|------|--------|---------|
| Start Date (visible) | `date1` | `date1` | `DD Month YYYY HH:mm:ss` (e.g. "08 June 2026 00:00:00") | Display only |
| End Date (visible) | `date2` | `date2` | `DD Month YYYY HH:mm:ss` (e.g. "08 June 2026 23:59:59") | Display only |
| Start Date (hidden/API) | `dateStart` | `dateStart` | `YYYY-MM-DD HH:mm:ss` (e.g. "2026-06-08 00:00:00") | **What the API actually reads** |
| End Date (hidden/API) | `dateEnd` | `dateEnd` | `YYYY-MM-DD HH:mm:ss` (e.g. "2026-06-08 23:59:59") | **What the API actually reads** |

**CRITICAL:** Update BOTH visible AND hidden fields. Search reads from `dateStart`/`dateEnd`.

### Action Buttons

| Element | ID | Class | jQuery Selector |
|---------|----|-------|-----------------|
| Search | `btn_search` | `btn-red` | `$('#btn_search')` |
| Reset | `btn_reset` | `btn-redoutline` | `$('#btn_reset')` |

### DataTables Export Buttons

| Element | Class | jQuery Selector |
|---------|-------|-----------------|
| Copy | `dt-button buttons-copy buttons-html5` | `$('.buttons-copy')` |
| CSV | `dt-button buttons-csv buttons-html5` | `$('.buttons-csv')` |
| Excel | `dt-button buttons-excel buttons-html5` | `$('.buttons-excel')` |
| PDF | `dt-button buttons-pdf buttons-html5` | `$('.buttons-pdf')` |
| Print | `dt-button buttons-print` | `$('.buttons-print')` |

### DataTables Table

| Property | Value |
|----------|-------|
| Table ID | `#dataInput` |
| AJAX endpoint | `/deposit/monitoring-riwayat-data` (POST) |
| Default page length | 10 |
| Max page length | 20,000 |

### Column Order (11 columns)

1. `No` — Row number
2. `Transaction Date` — `tanggal` field
3. `Transaction ID` — `wstransferid` field
4. `Saldo Awal` — `init_amount` field
5. `Kredit` — `amount_in` field
6. `Debet` — `amount_out` field
7. `Saldo Akhir` — `last_amount` field
8. `Transaction Type` — `transaction_type` field
9. `Transaction` — `wstransfertype` field
10. `Nomor RS` — computed from `sumber1` (strip `490` prefix)
11. `Remarks` — `wstransferdesc` field

## Key JS Patterns

### Login (jQuery trigger)
```javascript
$('#username').val('USER').trigger('change');
$('#password').val('PASS').trigger('change');
$('button:contains("Sign in")').trigger('click');
// URL changes to /home
```

### Set Date Filters (visible + hidden)
```javascript
$('input[name="date1"]').val('DD Month YYYY HH:mm:ss').trigger('change');
$('input[name="date2"]').val('DD Month YYYY HH:mm:ss').trigger('change');
$('#dateStart').val('YYYY-MM-DD HH:mm:ss');
$('#dateEnd').val('YYYY-MM-DD HH:mm:ss');
$('#btn_search').trigger('click');
```

### Export All Records
```javascript
$('#dataInput').DataTable().page.len(-1).draw();
// Wait for networkidle
$('.buttons-csv').trigger('click');
```

### Check Record Count
```javascript
var info = $('#dataInput').DataTable().page.info();
// info.recordsTotal
// info.pages
```

## Common Mistakes

1. **Only updating visible date fields** → API gets today's date
2. **Not setting page length to -1** → CSV only exports first 10 rows
3. **Using native `querySelector` for jQuery selectors** → `:contains()` is jQuery-only
4. **Waiting for `/deposit/monitoring-riwayat` after login** → Login redirects to `/home`
5. **Reusing same page for multiple users** → Session contamination
