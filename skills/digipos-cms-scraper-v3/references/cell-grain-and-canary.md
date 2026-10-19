# Cell Grain And Canary Evidence

## Grain Analysis

The `_B` login identifies a dealer/cluster account. DigiPOS adds a
`#parentPhoneNo_pick` selector whose concrete options represent Telkomsel
district cells inside that cluster. The login, cluster, cell, and API filter
are separate values:

| Meaning | Canary value |
|---|---|
| DigiPOS account (`adNo`) | `412110_B` |
| Parent cluster | `412110` |
| District cell | `412111` or `412112` |
| `parentcode` filter value | `412110412111` or `412110412112` |

The portal cell value is the parent cluster code concatenated with the cell
code. Preserve both the cell suffix and the full filter value. Do not derive
cell identity from transaction `Remarks`, `No`, row order, or account naming.

The extraction grain is `(account, cluster, cell, report_date)`. A cluster can
have up to three district cells. Download and validate each cell separately;
do not sum, deduplicate, or otherwise combine their rows during ingestion. Any
later calculation or aggregation across cells requires its own Telkomsel/Finance
rule and a separately reviewed downstream grain change.

The v3 `_B` account roster supplied for dynamic login selection is:

| Account | Cluster |
|---|---|
| `422040_B` | `422040` |
| `422140_B` | `422140` |
| `412110_B` | `412110` |
| `422010_B` | `422010` |
| `422090_B` | `422090` |
| `422100_B` | `422100` |
| `422180_B` | `422180` |
| `422220_B` | `422220` |

All eight rostered accounts have now been inspected live. Discover cell options
from the authenticated page for every run; never copy one cluster's cell list
to another.

## Verified Canary

**Portal:** `https://digipos-cms.finpay.id/deposit/monitoring-riwayat`  
**Account:** `412110_B`  
**Requested report date:** `2026-10-13`  
**Probe date:** `2026-10-14`

Live page options and settled table totals:

| Selector value | Portal label | Footer entries | Meaning |
|---|---|---:|---|
| `412110` | `Semua Cell (Dealer 412110)` | 0 | Aggregate option; unusable in this canary |
| `412110412111` | `412111 - PT MAKASSAR MEGAPUTRA PRIMA` | 589 | Cell `412111` |
| `412110412112` | `412112 - PT MAKASSAR MEGAPUTRA PRIMA` | 1,040 | Cell `412112` |

The two concrete cell totals sum to 1,629 while the `Semua Cell` option returns
zero. This is direct evidence that the aggregate selector cannot be used as a
complete extract for this canary. Do not assume that zero is the correct result
for every `_B` account until checked individually.

The final v3 ingestion run on `2026-10-14` downloaded both concrete
cells independently for `2026-10-13`. The manifest reported two validated
cells, zero empty cells, and zero failures. Independent read-only validation
confirmed:

| Cell | Footer | Saved XLSX rows | Requested-date rows | SHA-256 |
|---|---:|---:|---|---|
| `412111` | 589 | 589 | all `13-10-2026` | matched manifest |
| `412112` | 1,040 | 1,040 | all `13-10-2026` | matched manifest |

Both workbooks had the expected 11-column header. The generated files and
manifest were kept under a temporary output root, not added to the repository.
No calculation, aggregation, Kestra execution, database write, or Google Sheets
write was performed.

## All-Cluster Ingestion Test

On `2026-10-14`, run the v3 downloader separately for every rostered account,
using report date `2026-10-13`. Each account exposed one aggregate option and
two or three concrete cells; the aggregate was excluded. All requests within an
account ran sequentially. An independent manifest/XLSX check verified account,
cluster, cell-parent key, footer count, saved row count, requested date, 11-column
header, and SHA-256 for every artifact.

| Account / cluster | Cell row counts | Cell total |
|---|---|---:|
| `422040_B` / `422040` | `422041`: 87; `422042`: 114; `422043`: 61 | 262 |
| `422140_B` / `422140` | `422141`: 242; `422142`: 242 | 484 |
| `412110_B` / `412110` | `412111`: 589; `412112`: 1,040 | 1,629 |
| `422010_B` / `422010` | `422011`: 607; `422012`: 29; `422013`: 643 | 1,279 |
| `422090_B` / `422090` | `422091`: 2,469; `422092`: 691 | 3,160 |
| `422100_B` / `422100` | `422101`: 2,045; `422102`: 626 | 2,671 |
| `422180_B` / `422180` | `422181`: 212; `422182`: 372 | 584 |
| `422220_B` / `422220` | `422221`: 2,082; `422222`: 379 | 2,461 |
| **Total** | **18 cells** | **12,530 rows** |

Result: 8 account manifests, 18 validated cell artifacts, 0 empty cells, and 0
failed cells. The manifests and XLSX files are temporary evidence under
`/tmp/kilo/digipos-v3-all-20261013/`; this reference records the sanitized
verification summary, not the transaction data. No calculation, cluster/cell
aggregation, workflow, database, or Google Sheets action was performed.

Captured DataTables POST parameters confirmed:

| Request key | Canary value |
|---|---|
| `adNo` | `412110_B` |
| `parentcode` | selected cell value, e.g. `412110412111` |
| `startDate` | `2026-10-13 00:00:00` |
| `endDate` | `2026-10-13 23:59:59` |
| `length` | page size; v3 changes only this value for the full-row request |

Setting dates, selecting the cell, and clicking Search in that order was the
observed reliable sequence. Setting only dates or only selecting a cell did not
issue the needed filtered POST. Selecting the cell before setting the dates
left the page in a state where Search did not consistently apply the requested
filter. Keep the real Playwright click on `#btn_search`; do not substitute a
jQuery synthetic click.

## XHR And Validation

Use the page's own server-side DataTables POST to
`/deposit/monitoring-riwayat-data`. Change its `length` parameter in the route
handler and continue the original request. Do not synthesize an endpoint
request with `fetch()`: the v2 investigation observed HTTP 500 for hand-built
requests. Wait for the search response and settled footer before starting the
full-row request, so the response capture cannot race an earlier cell/date
request. Accept a signed integer for the incoming length; DataTables may use
`-1` to mean “All” before the route rewrites it to the bounded page length.

Require all available counts to agree before writing an XLSX:

- Settled `#dataInput_info` footer total.
- XHR `recordsFiltered`, when present.
- Number of rows returned by the enlarged-length XHR.
- Number of data rows reopened from the saved XLSX.

Require the XHR to carry the intended `adNo`, full `parentcode`, start date,
end date, and rewritten `length`. Treat `statusCode` other than `00` as a
failure. Treat `statusCode: 05`, server-busy text, a visible failure modal,
`NaN`, or a still-visible loading overlay as transient portal failure; retry
with a fresh page/login. Do not create an artifact from a partial response.

A zero-row district can be legitimate even when another district in the same
cluster has rows. Represent it as `status: empty` only after receiving a
successful response whose data length and settled footer both equal zero. The
aggregate option's zero result is not a substitute for these per-cell checks.

Run the bundled offline checks before a live canary:

```bash
python -m unittest discover -s skills/digipos-cms-scraper-v3/tests -v
```

## Artifact Contract

Write each non-empty cell to a separate XLSX path whose name includes account,
cell, and report date, for example:

```text
412110_B/412111/finpay-412110_B-412111(13-10-2026to13-10-2026).xlsx
```

Include a manifest record for every discovered concrete cell, including
validated empty cells and failed cells. Record the account, cluster, cell code,
full `parentcode`, portal label, report date, artifact path, SHA-256, footer
total, validated row count, method, and status. Keep raw transaction payloads,
passwords, cookies, CSRF tokens, and transaction examples out of the manifest.

Preserve the v2 11-column XLSX contract and use Excel rather than CSV to retain
phone-like `Nomor RS` values. `No` is a display counter, never a record key.
The `Nomor RS` mapping is `sumber1` with leading `49` removed for `Credit`
rows only; write `-` for `Debit` rows. Do not use `tujuan1` or parse phone
numbers from `Remarks`.

## Egress Shim

On the tested host, direct TLS to `ajax.googleapis.com` and
`fonts.googleapis.com` timed out, while the page's HTML and the jsDelivr jQuery
3.6.4 mirror loaded. The v3 browser route serves jQuery from
`https://cdn.jsdelivr.net/npm/jquery@3.6.4/dist/jquery.min.js` and neutralizes
Google Fonts CSS. The shim allows local inspection/testing; it does not change
production DNS, firewall, proxy, or egress settings. Production reachability is
an operator check and is outside this skill's ingestion test.
