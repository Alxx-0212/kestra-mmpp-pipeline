# XHR Scrape Fallback (export button broken)

## When to use this

Use `scripts/scrape_digipos_table.py` when the Excel export path fails, not before it. Export path is the default because it is the site-native output.

Triggers for the fallback:
- `page.expect_download` times out on `.buttons-excel` (observed repeatedly, all six accounts, 2026-09-29)
- the Excel button is absent from the rendered page
- the export click fires but no file ever arrives
- clicking through table pages collects fewer rows than the footer total

The failure is only in the download step. Login, search, and the table total are usually still fine, so the rows are still reachable without the button.

## Why it works

`/deposit/monitoring-riwayat` is server-side DataTables. The table does not hold all rows in the DOM; the browser asks for them. That XHR goes to:

```text
POST /deposit/monitoring-riwayat-data
```

with a DataTables body including `draw`, `columns[...]`, `start`, `length`, `search[value]`, `startDate`, `endDate`, `trxType`, `adNo`, `parentcode`, `page`.

Two traps, both verified:

- A hand-built `fetch()` from page context returns **HTTP 500** at any page length. The endpoint wants DataTables' own request; do not synthesize it.
- The working approach is to let DataTables issue the request and rewrite the `length` field on the way out via `page.route(...).continue_(post_data=...)`, then read the JSON off the response. One request, whole result set, no page clicking.

```python
page.route("**/deposit/monitoring-riwayat-data*", rewrite_length)
with page.expect_response(lambda r: "/deposit/monitoring-riwayat-data" in r.url and r.request.method == "POST"):
    page.evaluate("n => $('#dataInput').DataTable().page.len(n).draw();", 5000)
payload = response.json()   # {"draw", "recordsTotal", "recordsFiltered", "data": [...]}
```

`--page-len` defaults to 5000. Change `Content-Length` with the body, or the request stalls.

## Row-count gate

Same gate as the export method, and it is the whole point of the fallback. After the search, `#dataInput_info` gives the footer total (`Showing 1 to 100 of 1,396 entries`). Then require **all three** to agree:

- `totalEntries` parsed from the footer (via `download_digipos.set_date_and_search`)
- `recordsFiltered` in the XHR JSON
- `len(payload["data"])`

Mismatch means retry, never write the file. A 0-row or `data`-less response is a transient DigiPOS state, not valid no-data.

## Field mapping

JSON key to Excel column (row 1 = title, row 2 = header, data from row 3):

| Column | JSON key | Notes |
|---|---|---|
| `No` | — | 1-indexed counter |
| `Transaction Date` | `tanggal` | `DD-MM-YYYY HH:mm:ss` |
| `Transaction ID` | `wstransferid` | digit-only becomes a **number**; anything else stays text |
| `Saldo Awal` | `init_amount` | number |
| `Kredit` | `amount_in` | number |
| `Debet` | `amount_out` | number |
| `Saldo Akhir` | `last_amount` | number |
| `Transaction Type` | `transaction_type` | `Credit`/`Debit` |
| `Transaction` | `wstransfertype` | |
| `Nomor RS` | `sumber1` | strip leading `49`; **Credit rows only**, else `-` |
| `Remarks` | `wstransferdesc` | |

Unused keys: `sumber2`, `tujuan1`, `tujuan2`, `trxcode`, `id`, `syslogno`.

### `Nomor RS` — the rule that is easy to get wrong

`Credit` rows take `sumber1` with its `49` prefix removed. `Debit` rows are always `-`.

Tested candidates against all 1,217 rows of the real 2026-09-28 `411311` export:

| Candidate | Mismatches |
|---|---|
| `sumber1` minus `49`, Credit only | **0** |
| `sumber1` minus `49`, any type | 784 |
| `tujuan1` first | 1,217 |
| number parsed from `Remarks` | 190 |

Note the prefix is `49`, two characters, **not** `490`. Stripping three characters silently truncates a digit and still looks plausible (`082350408925` became `08235040892`), so the mistake survives a glance. Strip two and only on `Credit`.

### Excel formatting that must match

- `A1` = `CMS DigiPOS - Riwayat Saldo`, merged `A1:K1`
- money columns use `#,##0` when `abs(value) >= 1000`, else `0`
- `Transaction ID` numeric cells use `0`
- `Nomor RS` is written as **text** so phone numbers never become `8.23E+10`
- column widths as in the real export

Getting any of this wrong still loads in Kestra, so format drift is invisible until the sheet looks wrong. Keep the replay check.

## Self-check

`scripts/check_scrape_replay.py` replays cached XHR rows through the writer and diffs against real downloads in `tmp/kestra-wd/finpay-inbox`. Point it at a date where real exports exist:

```bash
python3 scripts/check_scrape_replay.py
```

It compares values *and* cell types *and* number formats, then asserts zero diffs. Last run (2026-09-28, all six clusters): 11,867 rows, 0 diffs.

Re-run it after any change to `row_to_cells`, `nomor_rs`, or `write_xlsx`.

## Throttling

DigiPOS throttles repeated logins. Expect 180s `set_date_and_search` timeouts and occasional `KeyError: 'data'` when clusters run back to back. Treat both as transient: fresh page, fresh login, sleep, retry. The production run with `--max-attempts 4` recovered `411311` on attempt 4 and the rest on attempt 1.

Never lower the validation to get past a timeout. A wrong file posted to Kestra is worse than a missing day.
