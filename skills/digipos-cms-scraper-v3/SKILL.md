---
name: digipos-cms-scraper-v3
description: This skill should be used when testing DigiPOS CMS ingestion for the newer _B dealer accounts whose cluster data must be selected and downloaded separately for each district cell, producing cell-scoped XLSX artifacts and a provenance manifest without running FinPay calculations or workflows.
---

# DigiPOS CMS Per-Cell Ingestion

Use this skill to inspect and download DigiPOS deposit-monitoring data at the
district-cell grain. Treat the account, parent cluster, selected cell, and
portal filter value as different identifiers. The current FinPay reporting
pipeline still uses a cluster/date grain; v3 artifacts are test/source evidence
only until a separately approved cell-aware persistence and calculation change
exists.

## Procedure

1. Read `references/cell-grain-and-canary.md` for the identity model, verified
   portal behavior, and limits of the current evidence.
2. Use a dedicated Playwright/Chromium environment with `openpyxl`. Read the
   DigiPOS password from `DIGIPOS_PASSWORD` or the ignored `.env_encoded` file;
   never print, pass as a CLI argument, or store credentials in the manifest.
3. Run one explicit account and report date with
   `scripts/download_cells.py`. Begin with the approved canary `412110_B`; do
   not fan out across the account roster until the canary has passed.
4. Discover cell options from the logged-in monitoring page. Record and skip
   the `Semua Cell` aggregate. Select every concrete cell separately; do not
   infer cell identity from a transaction row, its `No` display index, remarks,
   or download ordering.
5. For each cell, set the requested date, select the cell, and use a real
   Playwright click on `#btn_search`. Wait for the matching DigiPOS XHR and a
   settled table footer. Use the browser's own DataTables POST for the full-row
   fetch, rewriting only its `length` parameter as documented in v2's
   `references/xhr-scrape-fallback.md`.
6. Write one XLSX per cell only after the requested date, selected-cell request
   identity, footer count, full XHR row count, and saved workbook row count
   agree. Write an empty-cell manifest result without an XLSX only when DigiPOS
   reports a successful zero-row response and the settled footer is also zero.
7. Inspect the manifest and per-cell artifacts. Keep each cell as a distinct
   dataset; do not aggregate across cells, call Kestra, update a database, or
   write Google Sheets as part of this skill.

Example invocation:

```bash
python skills/digipos-cms-scraper-v3/scripts/download_cells.py \
  --account 412110_B \
  --date 2026-10-13 \
  --output-dir /path/to/fresh/output-root
```

The output root receives a unique run directory containing one account/cell
subdirectory per artifact and `manifest.json`. The command exits non-zero if a
cell fails validation. A validated empty cell is represented in the manifest
with `status: empty` and no workbook.

## Identifier Contract

Use these meanings consistently:

- **Account**: DigiPOS login, for example `412110_B` (`adNo` in the XHR).
- **Cluster**: Telkomsel parent cluster, for example `412110`.
- **Cell**: District within that cluster, for example `412111`.
- **Portal `parentcode`**: concatenated cluster and cell, for example
  `412110412111`; this is the selected option value sent to DigiPOS.
- **Report date**: one business date requested in `YYYY-MM-DD` format.

The extraction grain is `(account, cluster, cell, report_date)`. Preserve the
cell code and portal `parentcode` separately in the manifest. Never collapse
two district extracts into one cluster artifact during ingestion.

## Network Compatibility

When `ajax.googleapis.com` is unreachable, the login page's synchronous jQuery
request prevents its JavaScript from initializing. The script serves jQuery
3.6.4 from the reachable jsDelivr mirror and returns empty CSS for
`fonts.googleapis.com`. This is a local browser compatibility shim, not a
production egress fix. Verify production connectivity separately before using
the workflow or changing deployment configuration.

For verified selector values, request behavior, field mapping, server-busy
handling, and canary evidence, read `references/cell-grain-and-canary.md`.
