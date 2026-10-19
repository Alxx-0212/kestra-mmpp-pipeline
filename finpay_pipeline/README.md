# FinPay Workflow and Calculation Guide

This is the canonical technical and operational guide for the FinPay domain.
Repository-wide setup and service commands remain in the root `README.md`.

## Project Shape

This repo contains a Kestra workflow for daily FinPay files. The workflow reads
one uploaded CSV/XLSX file, validates it, relabels transaction groups,
flags unusual rows, deduplicates calculation rows, writes selected outputs to
Postgres, and writes reports to Google Sheets.

The active flow is:

```text
workflows/finpay_pipeline.yml
id: finpay_daily_pipeline_v5
namespace: finance.finpay
```

The reversal-aware monthly settlement design and implementation status are
documented in
[`FINPAY_REVERSAL_MONTHLY_IMPLEMENTATION_PLAN.md`](FINPAY_REVERSAL_MONTHLY_IMPLEMENTATION_PLAN.md).

The manual monthly flow is `workflows/finpay_monthly_materialization.yml` with ID
`finance.finpay.finpay_monthly_materialization_v1`. It produces a preview by
default. Final publication requires an explicit cutoff, expected source dates,
the preview source fingerprint, and a `READY_FOR_APPROVAL` result.

The monthly flow queues executions with concurrency limit one across calculation,
Google rendering, and publication-status recording. Requested Sheet writes fail
and activate the configured retries when credentials are missing or rendering
fails, including preview writes. Published failures record `FAILED` before
raising; a successful retry is recorded `COMPLETE` by the following audit task.
The writer uses the existing `FINPAY_DB_*` secrets and inherits the finance task
network, which must support both PostgreSQL access and Google HTTPS egress.

Kestra tasks run the Docker image `finpay-pipeline:3.11` and import Python with:

```python
from finpay_pipeline import ...
```

`pipeline.py` and `pipeline_refactored.py` remain package-local compatibility
shims for older callers; new Kestra code imports `finpay_pipeline` directly.

### Current Invocation And Inputs

Despite `daily` in the flow ID, the reporting flow has no Kestra trigger or
schedule. An operator or API client can start it with an uploaded file. The
separate `finpay_digipos_acquisition_v1` parent flow can acquire the six daily
exports in Kestra and call this reporting flow sequentially as subflows; the
reporting flow still begins at the immutable `FILE` boundary. A 05:00
Asia/Makassar daily schedule is defined on the acquisition flow but is
`disabled: true`; it remains manual until explicitly activated in production.

### Kestra DigiPOS acquisition flow

The acquisition flow is `workflows/finpay_digipos_acquisition.yml`:

```text
finance.finpay.finpay_digipos_acquisition_v1
```

Build its browser runtime from repository root after building/releasing the flow
image:

```bash
docker build -f finpay_pipeline/Dockerfile.digipos-scraper \
  -t finpay-digipos-scraper:1.0 .
```

It runs Chromium/Playwright in a dedicated `finpay-digipos-scraper:1.0` image.
For each account it prefers the native Excel download and falls back to the
DataTables XHR response if the Excel button is absent or its download does not
fire. Both paths produce the same XLSX shape and validate the footer count,
headers, date, identifiers, integer fields, and `Nomor RS`. The fallback rejects
missing financial values rather than manufacturing zero, preserves exact
transaction IDs and literal text, and accepts only its rewritten request's
response. CSV/XLSX loading coerces only schema numeric/date columns so leading
zeros in IDs/phone numbers and numeric-looking Remarks remain source text.
Cluster logins/downloads run concurrently
in a bounded thread pool (three workers by default). Each cluster creates and
closes its own Playwright instance and Chromium browser on its worker thread;
authenticated sessions and browser objects are not shared between threads.
Retries stay local to the failing cluster and retain the existing fresh-login
and backoff behavior. The task persists all six
validated files as Kestra artifacts before calling `finpay_daily_pipeline_v5`
once per cluster in fixed order. If any account or export fails validation, no
daily reporting subflow starts.

Required Kestra secret:

```text
DIGIPOS_PASSWORD
```

Set this to the **rotated** DigiPOS credential; do not copy credentials from a
scraper script, pass them in inputs/CLI arguments, or log them. The legacy
standalone scraper skill has a historical credential exposure and must not be
used as the Kestra runtime. Rotate that credential before enabling this flow.

Inputs:

| Input | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `report_date` | STRING | yes | - | One business date in `YYYY-MM-DD`, interpreted in Asia/Makassar. |
| `dry_run` | BOOLEAN | no | `true` | Scrape and validate, then invoke daily v5 without normal DB/Sheet effects. |
| `write_gsheet` | BOOLEAN | no | `true` | Pass through to each daily v5 subflow. |
| `send_telegram` | BOOLEAN | no | `true` | Pass through unusual Telegram alert preference to each daily v5 subflow. |
| `scrape_workers` | INT | no | `3` | Concurrent DigiPOS accounts, from `1` (serial) through `6`. Does not parallelize reporting or Sheet writes. |

If DigiPOS reports server-busy errors or the runner is short on memory, reduce
`scrape_workers`; parallel browsers use more resources and may encounter portal
throttling. All six exports must still validate before any child starts, and
manifest entries retain the fixed cluster order regardless of completion order.
An exhausted cluster failure prevents a success manifest and all reporting
subflows; successfully downloaded cluster files remain in the task directory.

The flow is manually triggered through the Kestra execution API while its
schedule is disabled. When enabled, it runs at 05:00 Asia/Makassar and sets
`report_date` to the previous local calendar day (for example, Sep 28 runs Sep
27). The scraper task uses host networking for DigiPOS
egress; production must permit this or provide an approved egress network/proxy.
The child daily flow waits for each prior execution and propagates failures, so
database/report processing and Google Sheets writes remain sequential.
Re-running the same date re-downloads the six files and relies on the existing
daily `cluster_id + report_date` idempotency contract. The schedule starts in
production mode (`dry_run=false`) with Google Sheets writes enabled.

The Kestra server must have the acquisition YAML deployed and the
`finpay-digipos-scraper:1.0` image available to its Docker task runner. The image
is a large Playwright/Chromium image; build it in an egress-enabled build
environment and publish/load it where Kestra's Docker runner can pull it. The
local simulation built the image and completed a real DigiPOS portal canary.

Dry-run canary:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "report_date=2026-09-27" \
  -F "dry_run=true" \
  -F "write_gsheet=false" \
  -F "send_telegram=false" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.finpay/finpay_digipos_acquisition_v1"
```

Use `dry_run=false` only after checking the six XLSX artifacts and manifest.
Poll the parent execution to a terminal `SUCCESS`; its six child daily-flow
executions are sequential and surfaced as subflow task state/outputs.

### Requested cell-selective extraction update (2026-10-14)

Status: eight-account skill extraction verified; the separate cell-aware
database/Sheets implementation is source-only and has not been deployed.
The following source logins were supplied for the new portal contract. They do
not yet replace the legacy workflow/parser's six numeric reporting identities.

| Source Login | Logical Cluster |
|---|---|
| `422040_B` | BOLAANG MONGONDOW (new) |
| `422140_B` | KOTAMOBAGU (new) |
| `412110_B` | PALANGKARAYA |
| `422010_B` | BANGGAI |
| `422090_B` | HALMAHERA MOROTAI |
| `422100_B` | HALMAHERA TIDORE |
| `422180_B` | MOROWALI |
| `422220_B` | TERNATE |

The requested behavior selects each concrete cell before searching/exporting,
never uses `Semua Cell` as a complete extract, and saves one distinct artifact
per account/cell/date. Cell IDs, labels, selectors, and request/export correlation
must come from the portal, not download order or the source `No` column. Retain
the strict 11-column export; put cell provenance in a separate manifest.

The v3 skill tested all eight accounts for `2026-10-13`: 18 cells and 12,530
validated source rows. See
[`../skills/digipos-cms-scraper-v3/references/cell-grain-and-canary.md`](../skills/digipos-cms-scraper-v3/references/cell-grain-and-canary.md)
for sanitized per-cell counts. This establishes extraction evidence, not live
database or Sheet correctness. Legacy daily v5 still rejects the new identities;
its cluster/date replacement grain must not receive per-cell files.

### Cell-aware versioned path (P4, source-only)

New flows are `finance.finpay.finpay_digipos_cell_acquisition_v2` and
`finance.finpay.finpay_cell_reporting_v1`. Legacy v1/v5 and monthly flow contracts
remain unchanged. The new path has no invoice, monthly, LinkAja, or Telegram
side effects. The 05:00 Asia/Makassar previous-day schedule is defined but
disabled. Supply the approved configuration FILE and checksum in its inputs
before any separately authorized activation; the schedule embeds no target defaults.

The acquisition flow uses a new image source definition,
`Dockerfile.cell-scraper`, for `finpay-digipos-cell-scraper:1.0`. It includes the
public package's reporting dependencies as well as Playwright. The legacy
scraper image is unchanged. Neither new browser image nor an updated reporting
image has been built/deployed during this work. Deployment must verify package
imports, browser egress, database/Google reachability and artifact retention first.

The reporting flow takes a flat ZIP bundle and explicit JSON configuration,
both checked by SHA-256. For acquisition or non-dry reporting the configuration
must also match the operator-managed `FINPAY_CELL_CONFIG_SHA256` secret. This
pin is not a flow input: a caller's matching file/checksum cannot approve its
own targets, balances, roster or editor lists. Provision/update this trust pin
only through separately approved deployment configuration; none was created here.
ZIP paths, duplicate entries/JSON keys, missing cells,
artifact hashes, dates, source amounts and exact textual IDs are validated
before any write factory is called. All required cells are calculated before
serial cluster publication. `dry_run=true` writes local prepared/design artifacts
only and never constructs DB/Google clients. For a dry-run date after the opening
date, it reports that prior coverage is required rather than reading the database.
`write_gsheet=false` stores DB-only state without a Google target/client. Database
configuration has no legacy fallback and migration application is never automatic.

Each account configuration contains `account`, `cell_codes`, `first_report_date`,
`opening_boundary` (midnight with timezone at the first included day), an integer
`opening_balances` value per cell, and `roster_version`. Top-level JSON has
`schema_version: 1` and an `accounts` list. Mark synthetic configurations
`dummy_data_only: true`; they cannot enter live persistence. Before live use,
provide explicit `database_target` and `approved_database_targets` objects
(`host`, `port`, `database`, `schema`, `user`). For Sheets also supply
`sheet_targets` keyed by account and `approved_sheet_targets`, each with
`spreadsheet_id`, numeric `worksheet_id`, `writer_email` and `finance_emails`.
The writer must match the supplied service-account identity. Credentials remain
in the existing `FINPAY_DB_PASSWORD` / `GCP_SA_KEY` secrets, not configuration JSON.
Source-bundle checks establish integrity/schema, not proof of DigiPOS origin.
Before commissioning, verify Kestra execution ACLs restrict non-dry child runs
to the acquisition service identity or explicitly approved manual-data importers.
Configuration approval does not grant arbitrary callers source-import authority.
No deployed ACL has been inspected/changed and no artifact signer is implemented;
this is a mandatory live-security gate, not an attestation claim from a checksum.

An explicit, un-applied migration `003_finpay_cell_reporting_v1.sql` defines
separate `finpay_cell_*_v1` evidence/currentness/publication relations. Apply only
in an approved schema under a separately authorized migration. It never modifies
legacy relations. Immutable evidence preserves distinct source rows; processed
calculation deduplication and family validation are cell-scoped. Verified empty
days supersede that cell's current data; failed or incomplete days do not.
Persisted dirty dates survive DB-only writes and drive subsequent Sheet repairs.
Configuration-only corrections rebuild affected current date cohorts from
immutable stored source rows, then select all cohorts and the new publication
snapshot atomically. They do not require scraping the same source again.
Balance/formula-only dirty dates are distinguished from source-change dates,
so recalculating old saldos does not reset unrelated manual MANDIRI entries.
Incomplete historical source/roster cohorts fail closed before Google mutation.
The reporting child also offers `configuration_only=true` for a balance/rules
recalculation using already stored identical source generations. It preserves
manual entries on the submitted date and its predecessor unless an actual
unpublished source change requires a reset. `configuration_only` defaults to
`false`, preserving normal source-refresh behavior: explicit refresh restores
defaults on the selected date and preceding dependency even when the uploaded
source is unchanged. Configuration-only mode cannot introduce a new generation.
All account histories are read-only preflighted before the first business write;
each publication reacquires locks and rechecks the baseline before mutating.
This is a validation barrier, not an atomic transaction across every cluster.
Raw source IDs/remarks remain verbatim; internal normalized comparison keys
are used only for family matching and calculation deduplication. Monetary
grouping, source/aggregate precision and producer/consumer size limits are
validated before casts, hashing or packaging can hide malformed data.

Compact presentation v3 has shared Date/Section/Description in A:C, numeric
Total in D:F, C1 in H:J, C2 in L:N and optional C3 in P:R. White G/K/O spacers
separate the numeric blocks. Freeze both top rows and A:C. Centered block titles
are above DEBET/KREDIT/SALDO headers; descriptions stay left-aligned and amounts
right-aligned. Header merges never cross a frozen boundary. Opening balances
remain in row 3. Repeated district descriptions/dates are not written.
The palette was read directly from original PKY formatting: header 1F4E78,
section-label CCE0F2, alternating DETAIL F6F9FC/FFFFFF, MANDIRI E5EFF9 and CASH
F6EDFC. Original financial amount values were not read for the style capture.
KETERANGAN uses 430 px (the reference's 410 px plus breathing room) and CLIP,
not wrapping. Body/calculation/section-label text uses black for readability;
titles and column headers retain white text on dark blue. The compact grid
keeps light solid cell borders, medium tops at
DETAIL/MANDIRI starts, and DEBET-only medium tops at Total/SELISIH. CASH does
not receive a full-row strong top; numeric-block vertical separators stay bold.
The cell path reserves at least 100 columns for horizontal scrolling and never
shrinks an existing grid. Before a data write, insufficient row capacity grows
to required rows plus 200; adequate row capacity stays unchanged. This uses
`cell_grid_capacity`, rather than calling the legacy `ensure_row_capacity`.
The financial row template remains v2; only its numeric-column projection is
v3. `render_cell_matrix(..., presentation_version=2)` retains the published wide
baseline for controlled verification/migration; the default is compact v3.
Medium-weight borders separate blocks and dates. Publication status, epoch and
fingerprint are sheet-scoped developer metadata, not an extra visible header.
An amber tab means incomplete Sheet publication; green and the A1 note mean
Sheet contents were verified, not that the database completion record succeeded.
End-to-end success requires both confirmed Sheet output and DB completion;
ambiguous/failed completion still blocks the target for operator reconciliation.
Daily DETAIL/MANDIRI/Cash rows align across all blocks. Opening balances and
continuous calendar coverage are required. Combined saldo equals the sum of
cell saldos; gross inter-cell transfers are not settlement eliminations.
Finance can edit per-cell MANDIRI only. Refresh resets the changed and preceding
dependent dates after a controlled audit; other manual dates are preserved.
Next-day defaults remain pending until that day's coverage exists.

### Cell Invoice And Detail V2

The authorized P2-P6 contract is implemented in the version-isolated
`run_cell_reporting_v2` / `finance.finpay.finpay_cell_reporting_v2` path.
`acquire_cell_bundle_v3` / `finance.finpay.finpay_digipos_cell_acquisition_v3`
consume the separately pinned schema-v2 configuration and `cell_sources_v2.zip`.
Legacy acquisition, reporting-v1, daily-v5 and monthly contracts remain unchanged.
The new flow uses a FinPay-only per-cluster Invoice worksheet with separate C1/C2/optional
C3 invoice panels. LinkAja fee rows, data and workflow remain separate and are
not included in this cell invoice. Each cell panel contains
CASH IN - NGRS (including QRISDUWIT disbursement-date rows), SELLTHRU and
ACCOUNTING. The version-isolated new flow does not emit a separate QRISDUWIT detail worksheet;
existing legacy QRIS tabs/history remain unchanged. The new path adds `CELL`
after `REPORT DATE` to the existing-format Unusual and Reversal outputs.

Read-only local evidence from main-checkout fixtures (no IDs, Remarks or amounts
were emitted): `data/411311` has 140 workbooks / 177,883 rows for May-September
2026. SLSFEE labels/suffixes occur May-August (3,836; 4,358; 4,167; 3,771 label
rows), with none in September. June also has two `REVERSAL` labels with an
SLSFEE identifier suffix. `data/new_finpay_data` has one October workbook /
2,054 rows, including 32 `SELLTHRUSALESHOLD` labels with SLSHLD suffixes.
Existing preprocessing normalizes the hold alias to the SLSFEE family. The v2
invoice output shows valid October hold families as
`BIAYA FEE BAR A ST (HOLD)`, preserves the September cutoff, and does not infer
a fee from the suffix alone. These fixtures do not prove complete production
coverage and were not copied/committed as test data.

The production P3/P6 contract uses one dedicated v2 workbook for all eight clusters,
distinct from v1, with stable `<cluster> Summary/Invoice/Unusual/Reversal` titles.
It scopes the synthetic E2E to one cluster/four output tabs; all-roster checks
stay offline. The `_B` IDs do not match the existing LinkAja invoice aliases.
LinkAja fee rows, crosswalk and reference sheet are outside the FinPay cell
invoice path and remain in the separate LinkAja workflow. The earlier proposed LinkAja
marker/crosswalk interface from P4/P5 is superseded for this FinPay-only invoice
scope. Every output belongs to one cluster/workbook lock and publication epoch;
all four tabs are guarded and marked pending before content writes. Complete
value/formula/protection readback precedes DB completion. Formatting is bounded
and chunked. Unknown mutation outcomes block automatic retry. Because all four
tabs are fully replaced, their actual full-history range is fingerprinted;
an unchanged multi-date rerun reuses its completed epoch.

Verification on 2026-10-16: 261 offline FinPay tests passed with zero skips;
cached Kestra v1.3.26 accepted the versioned flows. The explicitly approved
synthetic native canary passed in the existing copied workbook, using only
new `412110 Summary/Invoice/Reversal/Unusual` tabs (IDs 1900164101-1900164104).
It covers two cells, 13-15 October, a verified empty cell/day, 6 immutable sources,
60 ledger rows, 6 calculations/current selections, four-tab publication,
evaluated formulas and protections, stable reruns, manual preservation and
audited reset to defaults. Final epoch: 5, COMPLETE. Existing tab metadata was
unchanged; writes were confined to the four approved tabs, without reading old
transaction contents or changing sharing. The disposable DB/storage and private
test password were removed; the four synthetic tabs remain inspectable.

The canary's first attempt exposed date-range fingerprint drift despite identical
snapshots; an offline regression reproduced it before the fix. It resumed only
after all previous epochs were COMPLETE, the operation was IDLE, and all four
tabs were independently read back. No UNKNOWN operation was retried or cleared.
Evidence and scope are recorded in
`.kilo/plans/finpay-cell-db-gsheet-execution.md`; ignored local artifacts are in
`finpay_pipeline/data/cell_invoice_e2e_20261016/`.

The canary uses an explicitly synthetic legacy-workbook sentinel and writer-only
Finance test identity; it does not commission real v1/v2 target separation or
human-editor ACLs. Production remains gated on exact 32-tab targets, Finance
opening balances/boundary, approved roles/config pin and deployment. New image
sources target `finpay-cell-reporting:2.0` and `finpay-digipos-cell-scraper:2.0`;
neither image was built/pulled or deployed. Native tests used the cached runtime
with read-only current source. No real portal acquisition or Kestra execution
was triggered in this canary. Acquisition `dry_run` still contacts DigiPOS;
use synthetic bundles with reporting `dry_run=true` for offline work. The 05:00
Asia/Makassar previous-day schedule remains disabled.

#### Cell Design P2 Source Update

The authorized design amendment is frozen as
`.kilo/plans/finpay-cell-design-P1.md` plus `finpay-cell-design-P2-amendment.md`;
execution evidence lives in the existing cell DB/Sheet execution record. These
changes are source/offline work, not an update to the existing live canary tabs.
The epoch-5 native result above describes the earlier presentation only.

- Reporting-v2 explicitly renders Summary presentation v4: shared A:C, C1 D:F,
  spacer G, C2 H:J, and optional spacer K/C3 L:N. The aggregate display is gone;
  per-cell totals, financial calculations and manual-input identity remain.
  Legacy/default v3 and old v2 renderers remain unchanged. The local dummy
  generator accepts `presentation_version=4`; its default stays v3.
- Invoice layout v2 uses contiguous CASH IN - NGRS, SELLTHRU and ACCOUNTING
  sections, one empty borderless row between days, and separate cell panels.
  Original PKY section palettes/borders and 110/410/140/140 px panel widths are
  pinned in source. Title centering is applied after body alignment rules.
- Reversal/Unusual layout v2 uses white/`E5F0FF` explicit row stripes, original
  semantic column widths, wrapped remarks/reasons, and a centered 50 px CELL
  column. Displayed cell numbers are validated cluster-relative integers 1-3;
  `412111`/`412112` display as 1/2. Sparse Cell 2 stays 2. Full identifiers remain
  in configuration, facts, matching, audit and snapshot identity metadata.
- Every v2 dry-run/DB-only/live matrix declares the same deterministic expected
  style contract. Declaring it is not live attestation. Live COMPLETE requires
  bounded, value-free effective-format/dimension reads against the independently
  pinned semantic oracle: initial writes and unchanged reruns both check styles,
  widths, header merges/freezes and protections. Style drift fails closed instead
  of being silently repaired or accepted because monetary values still match.
- A valid, committed older layout with no style contract is only a value/marker/
  protection baseline. Its approved presentation migration must explicitly use
  `configuration_only=true` and existing selected generations so manual entries
  survive coordinate movement. The flag does not authorize the operation or
  manufacture a baseline. Whole-roster preflight blocks an ordinary refresh of
  such a layout before any account commits. Real source-refresh reset/audit rules
  remain unchanged after migration.
- Migration guards all four outputs and confirms PENDING markers before managed
  cleanup. It unmerges complete governed ranges, removes contained old banding,
  clears obsolete formatting/values over old/new extents, rebuilds inputs, and
  verifies the cleared tails. It never deletes physical rows/columns or clears
  UNKNOWN state. Out-of-scope intersecting merges/banding block the operation.

Native formatting, evaluated values, protections and reruns have now been verified
on the separately approved retained test resources described below. Human visual
approval, Finance sign-off, production commissioning and rollback publication
remain separate gates. Retained old renderers are not an automatic rollback
command. Never treat an existing managed workbook as fresh or infer a committed
baseline from its markers; use verified reconciliation or approved fresh targets.

Initial P2 offline verification: **276 FinPay tests passed, zero skipped**,
including effective-style drift rejection, exact two/three-cell titles, guard
timeout/under-guard failure evidence, and old-layout manual-input migration.
Two independent source reviewers concluded that no material findings remain.
They performed static review, not native API tests. The subsequent native result
is recorded below; human visual approval is still pending. Ruff is unavailable
and was not installed.

Real-source pre-production E2E on 2026-10-17 subsequently scraped all 54 cell-days
(14-16 October, 18 cells), ingested 36,504 rows in the explicitly approved isolated
test DB, and created 32 fresh tabs in the private copy. Publication stopped on
the first cluster with epoch/operation UNKNOWN after native formatting checks.
All four first-cluster value matrices match the staged DB. Native metadata exposed
merged-anchor/shared-edge representation and a late gutter clear erasing invoice
outlines; source fixes now compare visible logical edges and omit that late erase.
The enhanced fake reproduces the observed native behaviors; **285 offline tests
pass, zero skipped**, and focused independent review has no material findings.
That initial failed attempt was preserved, not blindly replayed. After explicit
current-user Controlled Resume approval, a verified native backup, target/cluster
locks, quiescence, snapshot/guard/value checks and compare-and-swap transitions,
epoch 1 was repaired and verified COMPLETE. Immutable precheck and outcome files
preserve the failure/recovery audit. The new design was then published through
configuration-only refreshes from the retained data, without another scrape.

**Initial feedback real-source E2E: PASS**, 54 cell-days / 36,504 rows / eight clusters /
32 test tabs, with evaluated formulas, logical visible styles, merges, widths,
protections, private sharing, original-tab preservation and unchanged rerun epochs
verified. Latest epochs are 412110=2 and the other seven clusters=1; all COMPLETE,
target operation IDLE, zero unresolved PENDING/IN_FLIGHT/UNKNOWN publications.
The final offline suite passes **293 tests, zero skipped**. This is direct native
scraper-core/public-v2-reporter verification, not a Kestra execution/deployment.

Invoice layout v3 merges and centers first-row titles such as `C1 | CELL 412111`
across each four-column panel, uses 140px report-date columns and three blank
columns between panels. Detail layout v3 retains the `CELL` header at 70px so its
filter does not clip it to `CE`, and adds medium top borders for each visible
report-date group. Old layout-v2 snapshots remain supported for verified recovery.
Genuine unacknowledged mutation/lost ownership or DB-connectivity ambiguity still
becomes UNKNOWN. A failed readback after acknowledged mutations remains PENDING
with its error/audit; it is never silently accepted as COMPLETE. The test harness
retries only bounded GET transport failures, never POST writes.

The subsequent Invoice4/Summary5 revision also completed natively with all 32
outputs, evaluated formulas, protection/style readbacks and unchanged reruns PASS.
Invoice4 uses two blank rows per day, continuous panel side rails across them,
and reference QRISDUWIT subtitle/child formatting. Summary5 keeps editable bank
inputs D/H/L but fixes their automatic next-day cashout references to E/I/M;
signed MANDIRI net rows remain in D/H/L. Legacy snapshots/layouts remain readable.
The independent published-data oracle checks 5,202 numeric cells over all 54
cell-days against recorded eligible ledger rows, with zero mismatches; all 36,504
raw row balance equations also pass. The current offline suite passes 299 tests.
This does not certify independent family classification, timestamp continuity,
actual Finance openings or bank statements. Real non-owner Finance identity
validation was explicitly deferred; owner edits cannot prove this access boundary.
The design was accepted visually by the user, not approved for production.

The one-off reporter exited after its additional E2E checks; no watcher remains.
This harness intentionally makes two full reporting calls and repeats bounded
style/protection scans at 1.1-second HTTP pacing, so final verification can lag
the visible update substantially. It is not a second scrape or a production
service, and its extra idempotency pass is not a requirement for every normal run.

Results: `data/cell_live_design_e2e_20261017/result.json`, with all 32 direct links;
the canonical execution record contains approval, hashes and recovery evidence.
The isolated DB/container/volume and private password remain retained for design
review; zero opening balances are TEST inputs, not Finance-approved balances.
Production/original-workbook/sharing/schedules remain unchanged. Human review and
production release are not implied by the test PASS. Do not replay the fresh-run
harness or manufacture a baseline for an already published target.

### October 1-17 Alias Backfill (2026-10-19)

The manual backfill to the new private workbook is **complete**. All 17 WITA dates,
306 cell-days across 18 cells, yielded 184,760 source rows. Oct14-16 reused the
previously validated, checksum-pinned bundles; Oct1-13 and Oct17 were acquired
from DigiPOS. Persisted isolated-schema coverage is 306 sources, 184,760 ledger
rows, 306 current selections and 306 calculations. Existing DB schema
`cell_design_test` and all prior tabs were preserved.

Summary tabs use `PKY` (412110), `MRT` (422090), `BGI` (422010), `TNT` (422220),
`TDR` (422100), `MRW` (422180), `KOTAMOB` (422140), `BOLMONG` (422040). Invoice,
Unusual and Reversal use the same alias suffixes. MRT is Morotai; MRW is Morowali.
The new workbook is [Salinan dari Salinan dari Monitoring Finpay & LinkAja](https://docs.google.com/spreadsheets/d/1PbHtxmdr558BEnb9UT047WIMpBMnPgGkC3J9TGHG6Cg/edit).
All 32 reports are COMPLETE; the alias mapping and individual worksheet IDs/links
are in `data/cell_october_backfill_20261018/result.json`. The pre-existing workbook
tabs and file sharing were verified unchanged.

The independent PUBLISHED Summary oracle checked recorded eligible ledger rows,
running totals, per-cell SALDO, the corrected next-day CASHOUT credit-column
references and each numeric Summary block: **29,394 numeric cells, zero
mismatches**. Separately all 184,760 raw source row balance equations passed.
The final offline FinPay suite is **307 passed, zero skipped**.

This was a direct cached-runtime scraper/reporter run, not a Kestra deployment or
cron execution. The temporary reporter failure was the known one-owner merge
metadata readback bug; a native-tested fix and focused regression were applied,
the exact acknowledged PENDING PKY publication was guarded and completed, and
the remaining aliases then published in one serial run. The stale `error_class`
in `result.json` is historical from an earlier failed publisher attempt; its final
status/checks are PASS, and final DB verification confirms all eight COMPLETE.

This is TEST data, not release/sign-off. New cluster openings were the operator-
provided zero boundary, not independently verified Finance balances. Real Finance
non-owner identity/access validation was explicitly deferred, the last-day bank
input awaits next-day/manual evidence, and bank statements, timestamp continuity
and independent family-policy disposition are not signed off. Do not promote these
Sheets as production accounting output. The isolated DB/volume/password remain
retained for evaluation; no prod DB, old workbook, sharing, scheduler, deployment
or image changes occurred.

Publication keeps target and cluster session locks across DB staging and
Sheet confirmation. A durable operation intent precedes temporary protections;
the single sheet-level protection is converted between refresh-guard and managed
states, not duplicated. Existing bank-input ranges are locked during refresh.
Google's omitted `warningOnly` defaults to false; editor comparisons permit only
configured editors plus independently read existing spreadsheet owners, who
already have implicit access. The client requests Drive metadata read-only for
this ownership check, never Drive sharing/write permissions. Unrecognized
editors, groups, domain access, narrowed sheet guards and bank-range holes fail
closed. Full-sheet and per-cell protection scopes are verified on readback.
The controlled refresh window still needs live validation because owners/admins
can bypass protections. Bounded typed batches and generation/formula readback
precede COMPLETE. The new client disables automatic HTTP mutation retries.
An unknown mutation response/lock loss blocks the target for operator reconciliation,
including proof that no old Google request remains in flight. Never clear the
guard/UNKNOWN state or blindly rerun after an ambiguous request. No automatic
unattended/global operator-recovery command or distributed atomicity guarantee is
provided; the scoped test recovery above required explicit approval and evidence.

After user approval, the missing synthetic test tab was recreated as a compact
**DESIGN PREVIEW**, using the same pinned synthetic inputs. Its full evaluated
numeric/text/blank results, palette, alignment, frozen rows/columns, merges and
protections were read back successfully. No database was restarted or published
for this presentation-only update; the blue tab/A1 note states that explicitly.
The normal publisher refuses a DESIGN_PREVIEW baseline until separately
approved DB-backed commissioning. Original workbook and Sheet70 were untouched.
Local evidence: `data/cell_shared_details_recreated/design-preview-result.json`;
all-cluster compact workbook: `data/cell_shared_details_preview/`.
The subsequent spacing/border preview is a separate tab
`PREVIEW 412110_B - SPACING V3` (ID `1900160313`) in the same copy; the earlier
tab was preserved. Native readback verified C430px/CLIP, 100 columns, 299 rows,
reference section/subtotal borders and unchanged evaluated synthetic results.
Evidence: `data/cell_preview_spacing_new_20261016/new-preview-result.json`.

Generate an explicitly local dummy design with no DB/Google clients:

```python
from finpay_pipeline import generate_dummy_cell_design
generate_dummy_cell_design("/path/to/synthetic-output")
```

The XLSX has formula tabs and separate Python-calculated numeric previews.
This does not validate Excel/Google formula evaluation. Live DB migration,
permissions/locks, Google protections/formulas and failure recovery remain
separately authorized T0L/live-T6 commissioning gates.

### Bounded synthetic DB/Sheets canary (2026-10-15)

An explicitly approved canary passed through synthetic XLSX ingestion, a
disposable PostgreSQL 18.4 instance and the copied spreadsheet. It did not use
DigiPOS, deploy/trigger Kestra flows, modify the original workbook or share files.

- Scope: `412110_B`, cells `412111`/`412112`, dates `2026-10-13` to `2026-10-15`.
- Native migration 003, complete/empty ingestion and rerun: passed; six source
  generations, eight raw ledger rows, six current cell/date selections.
- Real Sheets publication/rerun and evaluated numbers, text statuses and blanks:
  passed. A synthetic MANDIRI override was reset and audited without duplicating
  source/ledger evidence. DB and Sheets publication epochs matched; no dirty
  dates, refresh guards or in-flight target operation remained.
- Native header/gutter/border/freeze/protection metadata: one frozen row, three
  frozen columns, no merge across the freeze line, white header gutters, medium
  borders, one sheet protection and six protected bank inputs.
- Only `E2E 412110_B - SYNTHETIC` (worksheet `1900150013`) was created/written in
  `Salinan dari Monitoring Finpay & LinkAja` (spreadsheet
  `1OFPQJ4UMU83tXMJDGwN0GW5Zs66AAGnxXYlVNFeWWvg`). `Sheet70` was preserved.
- Two initial attempts stopped safely on native metadata canonicalization and
  the one-sheet-protection API constraint. Fixes received independent review;
  known completed/rejected requests were reconciled under locks with recorded
  scope/readback proof. This was not blind retry of a timeout or unknown apply.
- The approved disposable container, anonymous DB volume, loopback listener and
  generated DB password were removed after evidence capture; the synthetic
  worksheet remains for inspection. No production DB or schedule was changed.

Sanitized result/reconciliation evidence is under ignored local
`data/cell_e2e_20261015/`. The opt-in test runner is
`tests/run_cell_e2e_canary.py`; it has fixed test targets and independently
supplied config/principal/bundle pins, not a general production runner/recovery
tool. Production role/ACL, real-data/all-cluster, new-image, scheduling and
deployment commissioning remain separate gates.

### Parallel acquisition validation (2026-10-04)

The uncommitted parallel-scraping change uses the standard-library thread pool
and Playwright's per-thread lifecycle, checked against official documentation
through Context7. It changes the scraper, acquisition worker input, regression
tests, and the README/agent API guide only; reporting and Sheet tasks are unchanged.

- `docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace finpay-pipeline:3.11 python -m unittest discover -s finpay_pipeline/tests`: 96 tests passed, none skipped. Coverage includes worker bounds, overlap, browser isolation/cleanup, failure barrier, and serialized subflows.
- Pinned Kestra `v1.3.26` local validation accepted all three FinPay flows, including `scrape_workers`. Existing input-deprecation/subflow-resolution warnings remain; offline hostname resolution also emitted a warning.
- Rebuilt `finpay-digipos-scraper:1.0` and smoke-tested six real Chromium sessions with three concurrent workers and validated synthetic XLSX files, with networking disabled.
- Independent read-only concurrency review found no actionable defects. Successful-file retention is verified in the task directory, not Kestra artifact recovery after a failed task; portal throttling and production memory/throughput still need a canary. This review is not release approval.

No live portal login, throughput benchmark, database/Sheet write, workflow
deployment, or commit was performed for this change. Deploy the updated
acquisition YAML and scraper image together to expose the worker-count input;
keep the schedule disabled pending a controlled portal canary.

### Workflow regression verification (2026-10-08)

The current uncommitted fixes passed 111 FinPay tests with no skips in the owning
runtime image. On an isolated pinned Kestra server, the daily workflow completed
two synthetic dry runs with database, Sheet, and notification tasks skipped.
The actual monthly writer script failed for the intended missing-key error
after three real Kestra attempts; this was a synthetic preview probe, not a
live Google write or a full monthly publication.

The rebuilt browser image also passed a network-disabled smoke with six real
Chromium sessions and three concurrent workers, preserving leading-zero IDs and
literal formula-like text. FinPay fixes received independent static review with
no remaining actionable findings. See [shared verification evidence](../docs/validation.md#local-verification-2026-10-08)
for commands, cross-domain outcomes, and release gaps. No production deployment,
live portal login, or real Sheet/notification write was performed for this pass.

### Local E2E result (2026-09-28)

The complete scraper-to-daily-reporting flow was run for the last completed
business date, `2026-09-27`, with `dry_run=false`, Google Sheets enabled, and
Telegram alerts disabled.

| Cluster | Validated XLSX rows | Daily child state | Summary tab |
|---|---:|---|---|
| `411311` | 548 | `SUCCESS` | `PKY` |
| `421306` | 2,779 | `SUCCESS` | `MRT` |
| `421307` | 2,412 | `SUCCESS` | `TDR` |
| `421315` | 904 | `SUCCESS` | `BGI` |
| `421318` | 424 | `SUCCESS` | `MRW` |
| `421320` | 1,908 | `SUCCESS` | `TNT` |
| **Total** | **8,975** | **6/6 succeeded** | - |

All six exports passed header/schema, exact DataTables/XLSX row-count, requested
date, and `Nomor RS` validation before any child flow started. PostgreSQL source,
raw, and summary rows were persisted for all six clusters. Google Sheet summary
writes succeeded in `Salinan dari Monitoring Finpay & LinkAja`; unusual tabs were
updated with zero rows, and QRISDUWIT/Reversal tabs were cleared for the date
because those categories had no rows.

The separate August monthly simulation was also written to PostgreSQL and
Google Sheets. FinPay cluster `411311` is `FINAL` in the simulation with 31
report days, a 48-day reversal evidence window, and zero unresolved reversals.
LinkAja has six August monthly worksheets with 878 unresolved reversals total;
they are marked `PUBLISHED WITH EXCEPTIONS - REVIEW REQUIRED`, not Odoo-final.

These writes used the local simulation stack and the copied monitoring
spreadsheet, not production. The Kestra `DIGIPOS_PASSWORD` key was configured
and the portal login succeeded; its value was not read. Rotate the credential
exposed by the legacy scraper before production enablement.

| Input | Type | Default | Contract |
|---|---|---|---|
| `csv_file` | `FILE` | required | One FinPay `.csv` or `.xlsx` export matching the filename contract below. |
| `dry_run` | `BOOLEAN` | `false` | When `true`, execute validation and calculation but skip PostgreSQL, Google Sheets, Telegram, and the final sleep. |
| `source_filename` | `STRING` | empty | Original basename supplied by API clients when Kestra stores the FILE input under a temporary `.upl` path. |
| `write_gsheet` | `BOOLEAN` | `true` | When `false`, skip Google Sheets and Telegram side effects while retaining PostgreSQL/source-ledger persistence for isolated replay. |
| `send_telegram` | `BOOLEAN` | `true` | Disable unusual Telegram notifications independently while keeping Google Sheet writes enabled. |

The filename parser uses this exact shape:

```text
finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).<csv|xlsx>
```

The parser also accepts `_` in place of the opening parenthesis and an optional
closing `)` or `_`. The first date becomes `report_date`; the second date is
matched syntactically but is not parsed or otherwise used. The previous
month-end relative to the first date becomes the opening-balance initialization
date.
An unconfigured cluster, non-matching filename, or legacy `.xls` file fails
before processing. Convert `.xls` exports to CSV or XLSX first.

Current cluster routing is embedded in `workflows/finpay_pipeline.yml`:

| Cluster ID | Base worksheet | Spreadsheet | Default starting balance |
|---|---|---|---:|
| `411311` | `PKY` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |
| `421306` | `MRT` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |
| `421307` | `TDR` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |
| `421315` | `BGI` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |
| `421318` | `MRW` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |
| `421320` | `TNT` | `Salinan dari Monitoring Finpay & LinkAja` | `0` |

Runtime metadata uses `Asia/Makassar`. The report date used by calculations,
database replacement, and sheet replacement comes from the filename, not the
execution date.

## Editing Rules

- Keep changes aligned with the workflow task boundaries in `workflows/finpay_pipeline.yml`.
- Do not put secrets, emails, spreadsheet IDs, or credentials in committed code.
  Runtime workflow values are passed through Kestra secrets, commonly from
  base64 values in local `.env_encoded`.
- Preserve rerun idempotency: DB writes replace `cluster_id + report_date`, and
  sheet writers replace existing rows for the same report date.
- Do not reintroduce parallel Google Sheets writes unless backoff/batching is
  redesigned. The workflow is intentionally sequential around sheet writes to
  avoid Sheets API rate limits.
- Use structured DataFrame operations. Avoid ad hoc string mutation for schema
  and table writes when helper functions already exist.
- If a Kestra task import changes, update both `finpay_pipeline/__init__.py` and
  the task script that imports it.
- Keep public imports stable unless the user explicitly approves a contract
  change.

## Module Map

| Module | Responsibility |
|---|---|
| `finpay_pipeline.loading` | Load CSV/XLSX, detect the true header row, coerce data types, validate `FINPAY_SCHEMA`. |
| `finpay_pipeline.integrity` | Enforce debit/credit integrity after schema validation. |
| `finpay_pipeline.classification` | Preprocess transaction labels, relabel out-cluster, standalone ST, and reversal rows, validate known remark structures and fee groups, build unusual rows, decide summary exclusion. |
| `finpay_pipeline.dedup` | Drop duplicate calculation rows and create duplicate-row reports. |
| `finpay_pipeline.summary` | Aggregate summary-ready rows by processed transaction label. |
| `finpay_pipeline.database` | Maintain daily compatibility tables plus explicit append-only source loads and ledger evidence. |
| `finpay_pipeline.monthly` | Build cutoff-aware transaction previews and publish guarded monthly snapshots. |
| `finpay_pipeline.sheets_common` | Build gspread clients, create/open spreadsheets, manage protection, row capacity, A1 ranges, value formatting, and shared formatting helpers. |
| `finpay_pipeline.summary_sheets` | Write the main daily summary sheet, compact invoice panel, and the prerequisite shared `LinkAja` reference-sheet layout used by invoice formulas. |
| `finpay_pipeline.detail_exports` | Build and write QRISDUWIT and Reversal detail worksheets. |
| `finpay_pipeline.unusual_sheets` | Write unusual transaction rows to the per-cluster unusual worksheet. |

The read-only database rule check is owned by
`finpay_pipeline/queries/check_finpay_transactions_rules.sql`.

## Workflow Data Flow

Current high-level flow:

```text
uploaded file
-> parse_and_resolve
-> determine_current_date
-> load_and_validate -> validated.parquet
-> validate_integrity -> integrity_checked.parquet
-> persist_raw_transactions_to_db -> finpay_raw_transactions
   + append source load + finpay_ledger_events evidence
-> preprocess_transaction_labels -> preprocessed.parquet
-> flag_unusual_transactions -> unusual.parquet
-> persist_unusual_to_db -> finpay_unusual_transactions
-> upload_unusual_to_sheets
-> notify_unusual_telegram when unusual rows exist
-> deduplicate_transactions -> deduplicated.parquet
-> filter_qrisduwit_rows -> qrisduwit.parquet
   -> persist_qrisduwit_to_db
   -> upload_qrisduwit_to_sheets
-> filter_reversal_rows -> reversal.parquet
   -> persist_reversal_to_db
   -> upload_reversal_to_sheets
-> prepare_summary_transactions -> summary_ready.parquet
-> persist_transactions_to_db -> finpay_transactions
-> summarize -> summary.parquet
-> upload_to_sheets (summary.parquet + qrisduwit.parquet)
```

The separate `finpay_monthly_materialization_v1` flow reads the append-only
evidence, builds a provisional or final cutoff-bound result, and writes monthly
artifacts and snapshots. It does not rewrite the daily Sheet.

Important ordering:

- `persist_raw_transactions_to_db` runs before relabeling and dedup.
- `flag_unusual_transactions` runs after preprocessing but before calculation
  dedup, so duplicate rows can still be reported as unusual.
- Dedup happens before detail exports and summary preparation.
- `finpay_transactions` is written from `summary_ready.parquet`, so it only
  contains rows included in summary calculation.
- Summary preparation re-evaluates fee-cap, unknown-label, unknown-remark,
  reversal, and fee-only exclusion conditions against the explicitly
  deduplicated dataframe.
  The returned summary-unusual dataframe is used for task counts only; the
  workflow does not persist it or append it to `unusual.parquet` or the unusual
  sheet.

`dry_run=true` still runs the compute path. Production side effects are skipped:
Postgres writes, Google Sheets uploads, and Telegram alerts.

### Persisted Task Artifacts

Artifact names are workflow contracts because later tasks reference them
directly:

| Artifact | Meaning |
|---|---|
| `validated.parquet` | Strictly schema-validated source rows. |
| `integrity_checked.parquet` | Validated rows after debit/credit integrity checks. |
| `preprocessed.parquet` | Rows with preserved raw labels and processed transaction labels. |
| `unusual.parquet` | Pre-summary unusual rows, including calculation duplicates. |
| `deduplicated.parquet` | Calculation rows after minute-level duplicate removal. |
| `qrisduwit.parquet` | QRISDUWIT detail rows, including extracted disbursement date. |
| `reversal.parquet` | Reversal detail rows. |

Monthly artifacts are `finpay_monthly_result.json`,
`finpay_monthly_transactions.csv`, `finpay_monthly_categories.csv`, and
`finpay_monthly_exceptions.json`.
| `summary_ready.parquet` | Deduplicated rows retained for summary calculation. |
| `summary.parquet` | Totals grouped by processed transaction label. |

## Input File Contract

The loader detects the real header within the first ten rows. CSV parsing tries
semicolon, comma, pipe, and tab delimiters using UTF-8; Excel uses `.xlsx`.
Legacy `.xls` is explicitly rejected. The Pandera schema is strict, so missing
or additional columns fail validation. Column order may vary, but these 11 names
are required:

```text
No
Transaction Date
Transaction ID
Saldo Awal
Kredit
Debet
Saldo Akhir
Transaction Type
Transaction
Nomor RS
Remarks
```

`No`, the three balance fields, `Kredit`, and `Debet` must coerce to integers.
`Transaction Date` must parse as `DD-MM-YYYY HH:MM:SS`. After schema validation,
integrity validation rejects a row only when both `Kredit` and `Debet` are
non-zero; a row with both values zero is allowed by the current rule.

## Public API Used By Kestra

The public import surface is controlled by `finpay_pipeline/__init__.py`.
Common task imports:

| Task | Public functions |
|---|---|
| `load_and_validate` | `load_and_validate_schema` |
| `validate_integrity` | `validate_debit_credit_integrity` |
| `preprocess_transaction_labels` | `preprocess_transaction_labels` |
| `flag_unusual_transactions` | `flag_unusual_transactions` |
| `deduplicate_transactions` | `drop_duplicate_rows_by_minute` |
| `filter_qrisduwit_rows` | `prepare_transaction_detail_export` |
| `filter_reversal_rows` | `prepare_reversal_detail_export` |
| `prepare_summary_transactions` | `prepare_reversal_summary_transactions` |
| `summarize` | `summarize_by_transaction` |
| DB tasks | `postgres_dsn_from_env`, `write_finpay_dataframe_to_postgres` |
| Sheet tasks | `make_gspread_client`, `process_daily_upload`, `process_unusual_upload`, `process_transaction_detail_upload` |

## Transaction Label Rules

Preprocessing preserves the original source label in `raw_transaction_label`,
then updates `Transaction` and `processed_transaction_label` for downstream
outputs.

Downstream reversal summary/detail helpers call an internal
`ensure_reversal_labels_prepared(...)` helper. If `processed_transaction_label`
exists, they trust the preprocessed labels and do not relabel again. Raw legacy
callers without that marker still fall back to the reversal remark classifier.

Out-cluster relabeling:

- A `RECHARGE` group whose main row remarks contain
  `fee pembelian recharge out cluster` becomes `RECHARGE OUT CLUSTER`.
- Its matching `RECHARGEFEE` rows become `RECHARGE OUT CLUSTER FEE`.
- A `RECHARGE` row whose remarks contain
  `biaya pembelian recharge out cluster` becomes
  `PEMBELIAN RECHARGE OUT CLUSTER`.

Standalone Sellthru relabeling:

- A source `RECHARGE` row whose complete normalized remark is
  `transaksi sellthru` becomes `SELLTHRU` and is included in the ST summary.
- This is a characterized standalone main-row case, so it does not require
  `SELLTHRUFEE` or `SELLTHRUSALESFEE` companions.
- The exception is identified from the raw `RECHARGE` label and the complete
  normalized remark; a direct `SELLTHRU` row does not receive this exemption.
- If the standalone family contains any fee or other companion row, the whole
  family is flagged unusual and excluded from the summary.
- `raw_transaction_label` remains `RECHARGE`; only `Transaction` and
  `processed_transaction_label` become `SELLTHRU`. Source `Remarks` remains
  unchanged.

October sales-hold preprocessing:

- From `2026-10-01`, source `SELLTHRUSALESHOLD` is normalized to the existing
  `SELLTHRUSALESFEE` category. `raw_transaction_label`, `Transaction ID`, amounts,
  and `Remarks` stay unchanged; historical aliases are not activated before
  October. This is an effective-date restoration until another rule change,
  not an automatic October 31 expiry.
- The terminal source ID suffix `SLSHLD` identifies the exact parent transaction.
  Family normalization, duplicate diagnostics, and pure database identity
  enrichment recognize it consistently. Its existing normalized
  `transaction_id_type` is `SLSFEE`; no table, column, or type is added.
- Keep both observed platform-fee rows. Their combined `SELLTHRUFEE` debit must
  still total Rp100; generic `FEE` rows are not reclassified as sales hold by
  amount, row order, or source `No`.

Reversal relabeling:

- `biaya pembelian recharge` -> `Reversal - NGRS`
- `platform fee recharge rp. 20,-` -> `Reversal - NGRS FEE`
- `biaya pembelian recharge out cluster` -> `Reversal - PEMBELIAN RECHARGE OUT CLUSTER`
- `fee pembelian recharge out cluster` main/fee groups become
  `Reversal - Recharge Out Cluster` and
  `Reversal - Recharge Out Cluster FEE`
- ST reversal labels still exist for detection, but all `Reversal - ST*`
  categories are intentionally unsupported for summary and should be
  unusual/excluded.

Fee validation:

- `RECHARGE` expects `RECHARGEFEE` debet total of `20`.
- Before `2026-09-01` (Asia/Makassar), normal `SELLTHRU` expects
  `SELLTHRUFEE` debet total of `100` and at least one `SELLTHRUSALESFEE` row.
- During September 2026, normal `SELLTHRU` expects only
  `SELLTHRUFEE` debet total of `100`; `SELLTHRUSALESFEE` is retired.
- From `2026-10-01`, normal `SELLTHRU` again expects a `SELLTHRUSALESFEE` companion
  alongside the Rp100 platform-fee total. Preserve the source hold amount; this
  phase adds no inferred fee rate or new amount cap.
- A missing or incorrect ST fee is flagged unusual but remains included in the
  summary for finance review. Incoming September SLSFEE quarantines the entire
  ST family; a family crossing the September or October rule boundary also
  quarantines instead of using a single policy for mixed-era rows.
- `Reversal - NGRS` expects `Reversal - NGRS FEE` kredit total of `20`.
- `Reversal - Recharge Out Cluster` expects its fee row kredit total of `20`.

Fee caps:

- Recharge-style fees are included only up to `20` per base transaction.
- Sellthru fee is included only up to `100` per base transaction.
- Fee rows beyond those caps are flagged unusual and excluded from summary.
- Fee totals below the expected amount are still included but flagged unusual.

Fee-only behavior:

- `RECHARGEFEE` without `RECHARGE`, including reversal recharge-style fee-only
  cases, is flagged unusual but included in summary.
- `SELLTHRUFEE` / `SELLTHRUSALESFEE` without `SELLTHRU` is flagged unusual and
  excluded from summary.

Unknown transaction and remark behavior:

- Any non-reversal transaction label outside the known FinPay summary
  categories is flagged with an `unknown transaction label` reason.
- Every known processed transaction label must also match one characterized
  remark structure. A mismatch is flagged with an
  `unknown remarks pattern for transaction` reason.
- Both cases quarantine the complete transaction family and are excluded from
  `summary_ready.parquet`; they cannot affect the persisted calculation rows or
  summary sheet.
- Remark matching is case-insensitive and replaces changing numeric values and
  `DD-MM-YYYY` dates with placeholders. Static words, punctuation, and order
  must still match. This permits different amounts, phone/cluster numbers, and
  dates without weakening the transaction-to-remark contract.

The historical remark vocabulary was characterized from all 60 local cluster
`411311` exports (74,532 rows), with the additional October ST structures
characterized from the new workbook described below:

| Processed transaction | Accepted normalized remark structure |
|---|---|
| `CASHOUT APOLLO` | `apollo mrc<value> <date>` |
| `QRISDUWIT` | `disburse qris duwit atas transaksi pembayaran qris pada tanggal <date> sejumlah rp <value>` |
| `DISBURSEMENT` | `sales fee payment` for voucher and/or perdana counts, followed by `sejumlah <value> rupiah` |
| `FeeTransaksi` | `fee digipos rp.<value> \| fee sbp rp.<value> \| dari nomor :<value>` |
| `RECHARGE` / `Reversal - NGRS` | `biaya pembelian recharge sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGEFEE` / `Reversal - NGRS FEE` | `platform fee recharge rp. <value>,-` |
| `PEMBELIAN RECHARGE OUT CLUSTER` / its reversal | `biaya pembelian recharge out cluster sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGE OUT CLUSTER` / its reversal | `fee pembelian recharge out cluster sejumlah <value> rupiah, dari <value> ke <value>` |
| `RECHARGE OUT CLUSTER FEE` / its reversal | `platform fee recharge rp. <value>,-` |
| `SELLTHRU` | `sellthru sales fee`, the standalone `transaksi sellthru` case, or observed `transaksi sellthru sejumlah rp<value>,- dari <value>` |
| `SELLTHRUFEE` | `platform fee sellthru rp. <value>,-`, observed `platform fee sellthru rp<value>,-`, or the existing `fee transaksi sellthru ... fee tsel ... fee finnet ...` structure |
| `SELLTHRUSALESFEE` | `sales hold transaksi sellthru sejumlah <value> rupiah, dari <value>` or observed `sales hold transaksi sellthru sejumlah rp<value>,- dari <value>` |

The daily summary sheet keeps the historical `BIAYA FEE BAR A ST (HOLD)` detail
row and formula before the cutoff. From September 2026 it omits that row and
the MANDIRI ST formula uses only `SELLTHRU + SELLTHRUFEE`. Historical reruns
remain governed by the legacy rule. The October preprocessing update does not
change this layout or invoice calculation yet; October Sheet/invoice reinstatement
and monthly release impacts remain a separate phase before production use.

### October preprocessing evidence (2026-10-12)

Scope: preprocessing labels, family validation, identity enrichment, and duplicate
diagnostics only. Source was the main checkout's ignored
`data/new_finpay_data/CMS DigiPOS - Riwayat Saldo (2).xlsx`, opened read-only with
networking disabled. It contains 2,054 rows, all dated `2026-10-10`, including
32 `SELLTHRU`, 64 `SELLTHRUFEE`, and 32 `SELLTHRUSALESHOLD` rows. All 32 `SLSHLD`
IDs map exactly to the corresponding main ID after removing that terminal suffix.
The two platform-fee rows per family sum to Rp100.

The `2026-10-01` return date follows the user's month-level October update, not
direct evidence of October 1 from this one-day file. The three new ST remark
structures above were characterized from the actual workbook; all other case
allowlists remain unchanged.

Before the update, 128 ST-family rows were unusual. The read-only post-update
check preserved all 2,054 rows and source fields except the processed transaction
label, recognized the 32 hold rows, and returned zero unusual rows. This checks
classification membership, not final invoice correctness or month completeness.
All 126 FinPay tests passed, including September/August regression cases, the
October boundary, exact-ID alias linkage, split fees, unknown-remark quarantine,
and row-specific alias selection with repeated dataframe index labels.
Independent static review found no remaining actionable defects after clarifying
the open-ended return-date contract; a November alias-continuation regression
guards against an unrequested month-end expiry. This is not release approval.

Implementation is an uncommitted worktree change. No source workbook, applied
migration, workflow YAML, Sheet layout/formula, or monthly calculation was edited;
no history backfill, image rebuild, DB/Sheet write, deployment, or commit was
performed. Review and defer October invoice/Sheet and monthly readiness changes
before enabling these rules in production. Other cases have not been expanded.

Do not silently map another new label or remark structure into an existing
category; characterize it from source data and add tests first.

Unusual rows must keep transaction `Remarks` unchanged. Extra explanation
belongs in `unusual_reason`.

## Dedup Rules

Dedup removes calculation duplicates after unusual detection. The duplicate
report should still show the original duplicate rows in unusual output.

`No` is not a stable duplicate key because duplicate transactions can have
different source row numbers. Existing dedup logic compares all dataframe
columns except `No`, with transaction date compared at minute precision.

## Postgres Rules

There are five daily compatibility tables, two daily evidence relations, and
seven monthly-only relations/views:

| Table | Source | Scope |
|---|---|---|
| `finpay_raw_transactions` | `integrity_checked.parquet` | Raw validated rows before relabeling and dedup. Duplicate source rows are retained. |
| `finpay_transactions` | `summary_ready.parquet` | Final calculation rows included in summary. Excluded unusual rows are not here. |
| `finpay_unusual_transactions` | `unusual.parquet` | Unusual rows from the pre-summary unusual detection phase and `unusual_reason`, including rows still included in summary. |
| `finpay_reversal_transactions` | `reversal.parquet` | Reversal detail output from the deduplicated dataframe. |
| `finpay_qrisduwit_transactions` | `qrisduwit.parquet` | QRISDUWIT detail output with `disbursement_date`. |
| `finpay_source_loads` | Daily raw persistence | Append-only source generations and current-load replacement metadata. |
| `finpay_ledger_events` | Daily raw persistence | Append-only source rows with load ID and content hash. |
| `finpay_current_ledger_events_v` | Monthly model | Ledger events filtered to currently selected source generations. |
| `finpay_transaction_events_current_v` | Monthly model | Current transaction-group events by exact cluster and ID. |
| `finpay_reversal_status_current_v` | Monthly model | Guarded exact-ID reversal states and active flags. |
| `finpay_monthly_transactions` | Monthly model | Frozen cluster-month transaction/reversal facts. |
| `finpay_monthly_publications` | Monthly model | Cutoff, fingerprint, release status, and approval audit. |
| `finpay_monthly_source_expectations` | Monthly model | Expected report dates used to gate source coverage. |
| `finpay_monthly_previews` | Monthly model | Durable preview fingerprint/status records. |

All DB writes replace the current `cluster_id + report_date` batch before
insert. This is the primary rerun/idempotency mechanism.

Daily source evidence is created by
`migrations/001_finpay_daily_source_evidence.sql` only. A daily raw upload does
not create monthly tables or views. Source loads and ledger events are retained
across reruns, while a corrected upload marks the previous generation for that
cluster/report date non-current. The historical combined
`001_finpay_transaction_model_legacy_compat.sql` remains only for compatibility
callers; new daily tasks must use the daily-only initializer. Monthly objects
are created by `migrations/002_finpay_monthly_model.sql` when a monthly flow is
explicitly run. Monthly snapshots are replaced only for the explicit
cluster-month publication and retain their source fingerprint.

### Daily historical backfill procedure

Use `finpay_daily_pipeline_v5` for each source file in chronological order. First
run a representative file with `dry_run=true`; after it succeeds, run production
mode (`dry_run=false`) with a unique source filename. Poll each execution to
`SUCCESS` before submitting the next file. Replays replace the compatibility
`cluster_id + report_date` daily batch but retain prior source generations and
ledger rows for audit. Daily backfill does not apply monthly DDL, resolve
reversal-adjusted payable, or publish an Odoo monthly snapshot.

Schema rules:

- No table has a surrogate `id`.
- Source `No` is not persisted.
- Processed tables do not persist a `transaction` column because it duplicates
  `processed_transaction_label`.
- `raw_transaction_label` preserves source spelling; in-memory `Transaction`
  labels are normalized to uppercase before classification, and
  `processed_transaction_label` is lowercased only at the DB write boundary.
- Every table includes `base_id`.
- Every table except `finpay_qrisduwit_transactions` includes
  `transaction_id_type`.
- `transaction_id_type` is `MAIN` for base transaction IDs and otherwise the
  trailing suffix such as `FEE`, `SLSFEE`, or `SALESFEE`.
- Existing pipeline-owned tables are reconciled on write, so obsolete columns
  from old schemas are removed.
- Treat that automatic reconciliation as legacy runtime behavior. Do not add new
  implicit schema changes to ordinary writes; plan new tables or columns as an
  explicit migration with compatibility, backfill, and rollback decisions.

## Google Sheets Outputs

Target spreadsheet name from the active workflow cluster config:
`Salinan dari Monitoring Finpay & LinkAja`.

Each configured cluster has four stable FinPay sheets:

```text
<base worksheet>
<base worksheet> - Unusual
<base worksheet> - QRISDUWIT
<base worksheet> - Reversal
```

The same spreadsheet also has one stable shared worksheet named `LinkAja`.
FinPay owns the invoice-side integration: `process_daily_upload` creates the
worksheet when missing and initializes or repairs its two-row, per-cluster
reference headers before appending the invoice report. This ordering is a
required contract because invoice formulas referencing a missing `LinkAja`
worksheet fail. LinkAja business calculations and source population remain
outside the FinPay calculation modules.

All sheet writers use `sheets_common.open_or_create_finpay_spreadsheet`. If the
service account creates the spreadsheet, code attempts to set locale/timezone,
disable editor sharing, share configured writer emails, and remove public/domain
permissions. If the service account does not own an existing spreadsheet, Drive
permission sync is intentionally limited.

The workflow appends daily summary blocks to the same cluster summary sheet
across month boundaries. It does not create month-suffixed sheets and does not
auto-hide older worksheets.

All Google Sheets writers uppercase literal text cells immediately before
`ws.update(...)`. Formulas and numeric values are preserved; formula-generated
status text should be authored uppercase.

### Main Summary Sheet

Current summary columns are:

```text
A REPORT DATE
B SECTION
C KETERANGAN
D DEBET
E KREDIT
F SALDO
G spacer
H REPORT DATE
I INVOICE REPORT
J DEBET
K KREDIT
```

Opening balance uses the previous-month-end date from the uploaded file date.
Its `SECTION` cell is intentionally blank.

Daily cash-flow sections are written in columns `A:F` in this order:

1. `DETAIL`
2. `MANDIRI`
3. `Cash`

Invoice reports are written as one compact panel in columns `H:K`, starting on
the same row as the first `DETAIL` row:

- Column `H` repeats the invoice report date for filterability.
- `CASH IN - NGRS` is the first mini-table.
- QRISDUWIT disbursement-date rows are a subpart inside
  `CASH IN - NGRS`, not a separate mini-table.
- `SELLTHRU` is below Cash In.
- `ACCOUNTING` is the final mini-table.
- Invoice header and data rows stay unmerged so date filtering still works.
- Contiguous fully blank invoice-side `H:K` blocks are merged vertically and
  bordered as blank space.
- The opening balance/saldo initialization row is not merged in the invoice
  area; invoice-side merges are limited to inserted daily date blocks.

Cash In / Invoice NGRS report rows:

- `NGRS`
- `Recharge Fee`
- `Pembelian Recharge Out Cluster`
- `Expected Biaya Pembelian Recharge Out Cluster FEE`
- `LINKAJA EXPECTED RECHARGE OUT CLUSTER FEE`
- `LINKAJA DIGIPOS B2B TRANSFER IN CLUSTER FEE`
- `QRISDUWIT` parent row when QRISDUWIT rows exist

QRISDUWIT Cash In subrows:

- One row per `DISBURSEMENT DATE`, sorted ascending.
- The row label is `QRISDUWIT - <DISBURSEMENT DATE>`.
- Column `J` uses the grouped source `KREDIT` amount as invoice debit from
  the company perspective; column `K` is blank.
- Missing/blank disbursement dates are grouped under
  `QRISDUWIT - MISSING DISBURSEMENT DATE`.
- The `QRISDUWIT` parent row uses the same fill, bold text, and centered
  alignment as the `CASH IN - NGRS` header row.

Additional rows currently live at the bottom of the `DETAIL` section:

- `Jumlah Pembelian Recharge Out Cluster`
- `Expected Biaya Pembelian Recharge Out Cluster FEE`
- `Jumlah Reversal - PEMBELIAN RECHARGE OUT CLUSTER`

Sellthru rows:

- `ST`
- `BIAYA FEE ST`
- `BIAYA FEE BAR A ST (HOLD)`

Accounting rows:

- `PPOB`
- `DISBURSEMENT`
- `Recharge Out Cluster`
- `Recharge Out Cluster FEE`
- `Reversal - Recharge Out Cluster`
- `Reversal - Recharge Out Cluster FEE`

`Jumlah Pembelian Recharge Out Cluster` and
`Jumlah Reversal - PEMBELIAN RECHARGE OUT CLUSTER` are counts. The matching
`Expected Biaya Pembelian Recharge Out Cluster FEE` row is calculated as the
normal pembelian count multiplied by `200`. This is intentionally different
from the recharge-style fee validation cap of `20`.

`MANDIRI` footer rows include `PEMBELIAN RECHARGE OUT CLUSTER` in the `NGRS`
formula and include a separate `Reversal - Pembelian Recharge Out Cluster` row.

`MANDIRI` rows compute the final net summary totals. `Cash` rows contain:

- `RUNNING TOTAL`
- `MANDIRI`
- `SELISIH`

`MANDIRI` input is column `D` (`DEBET`). The formula defaults from the next
`TRANSFER MASUK DARI FINPAY` value, but the cell can be manually overwritten.
Protection leaves only Mandiri input cells editable unless
`FINPAY_MANDIRI_EDITOR_EMAILS` restricts them to a subset.

### Sheet Formatting

- Header row is frozen on every output worksheet.
- `KETERANGAN` column `C` and invoice label column `I` are set to `410px`,
  wide enough for `EXPECTED BIAYA PEMBELIAN RECHARGE OUT CLUSTER FEE`.
- Invoice date column `H` is compact. Invoice label column `I` is intentionally
  wide and clipped instead of wrapped so long labels stay on one row.
- The workflow clears shared basic filters; users should use temporary filter
  views for personal filtering.
- Summary sheet uses full cell borders across the written range. Detail and
  unusual output sheets use horizontal borders plus zebra-striped data rows.
- Light grid borders are applied across normal summary rows.
- Summary `DETAIL` rows use subtle alternating row backgrounds. On the first
  detail row, `A:B` uses the stronger section header fill while `C:F` keeps the
  light stripe.
- Summary sheet keeps strong top borders at starts of `DETAIL` and `MANDIRI`.
  The `DETAIL` start border is reapplied on both cash-flow columns `A:F` and
  invoice columns `H:K` because later appends can otherwise weaken the invoice
  report top edge.
- Each summary append reapplies strong top borders for current and existing
  daily `DETAIL` starts, because normal grid borders on later appends can
  otherwise overwrite the first block's top border.
- `Cash` section start intentionally has no full-row strong top border.
- `Total` and `SELISIH` strong top borders apply only to column `D`.
- QRISDUWIT, Reversal, and Unusual sheets keep a strong top border on the first
  data row of each report-date block.
- QRISDUWIT, Reversal, and Unusual sheets use one Google Sheets banded range for
  zebra striping with a stronger pale-blue stripe than the summary detail
  section. Writers delete existing banded ranges before adding the current one
  to avoid stacking banding rules on reruns.
- Avoid merged cells in data rows because they make filtering and reruns harder.
  The summary invoice panel intentionally merges only contiguous fully blank
  invoice-side `H:K` blocks; invoice header and data rows stay unmerged.

### Detail And Unusual Sheets

QRISDUWIT/Reversal detail sheets are written by the generic detail writer.
QRISDUWIT includes `DISBURSEMENT DATE`; Reversal does not.

`Nomor RS` is written/formatted as text to avoid integer conversion.

Unusual sheet columns include `Remarks` and separate `UNUSUAL REASON`; both are
rendered uppercase by the sheet writer. Do not overwrite transaction remarks
with diagnostic text before the sheet write.

## Google Sheets Protection And Access

Generated worksheet ranges are protected after each write. Protection editors
come from `FINPAY_PROTECTION_EDITOR_EMAILS` plus the authenticated service
account. `FINPAY_MANDIRI_EDITOR_EMAILS` controls who can edit Mandiri cells
when set.

## Row Replacement And Capacity

Sheet writers replace existing rows for the same report date instead of
skipping. This matters for reruns after logic changes.

Before writing near the grid boundary, writers call `ensure_row_capacity`.
Summary writes must account for the future-transfer lookup range used by
Mandiri formulas. Detail and unusual writers add buffer rows around their
explicit output range.

## Current Workflow Concurrency

The workflow uses `Sequential` blocks around sheet writes. DB writes and sheet
writes are near each other, but do not assume Google Sheets tasks are safe to
parallelize. The current design prioritizes reliability over runtime.

That sequencing applies only inside one execution. The daily flow declares a
flow-level concurrency limit of one with queued executions, so overlapping
manual runs cannot write the shared worksheet at the same time.

Telegram unusual notification uses an HTTP request task with exponential retry.
Google Sheets upload tasks also use exponential retry.

For both categories the current retry policy starts at 15 seconds, caps at 60
seconds, and allows three attempts. PostgreSQL tasks do not declare an explicit
Kestra retry policy. The flow-level error task sends a failure alert through the
configured Telegram bot and includes the execution identifier and UI log path.
Treat retry, notification, and same-date concurrency controls as release
contracts before increasing execution frequency or automating file ingestion.
Notification delivery is best effort because the error handler itself can fail
if Telegram is unavailable.

## Testing Checklist

Run FinPay behavior tests with Python 3.11 and the package manifest at
`finpay_pipeline/requirements.txt`:

```bash
python3.11 -m unittest discover -s finpay_pipeline/tests
docker run --rm \
  -e PYTHONPYCACHEPREFIX=/tmp/finpay-pycache \
  -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 \
  python -m unittest discover -s finpay_pipeline/tests
git diff --check
git diff --cached --check
```

The FinPay behavior suite intentionally fails import instead of skipping when
`pandas`, `gspread`, `psycopg`, or `pandera` is unavailable. A missing runtime
dependency or zero executed FinPay behavior tests is a failed validation.

When dependencies or Docker-copied source files change, rebuild:

```bash
docker build -f finpay_pipeline/Dockerfile -t finpay-pipeline:3.11 .
```

Keep the root `.dockerignore` protections in place before building. When the
workflow changes, also validate it against the same Kestra version deployed
on-premises and smoke-test all `from finpay_pipeline import ...` symbols in the image.
The repository provides a secret-free, in-memory local validator configuration:

```bash
docker run --rm \
  -v "$PWD/finpay_pipeline/workflows/finpay_pipeline.yml:/flows/finpay_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml
```

When behavior changes, prefer testing with real files from `data/` or local
sample files already used in this project. Avoid committing sample data or
secrets.

## Common Pitfalls

- Updating a helper but not exporting it from `finpay_pipeline/__init__.py` when
  Kestra imports it.
- Changing sheet columns without updating formulas, protection ranges, DB
  schema docs, and replacement logic.
- Adding shared basic filters back into sheets. Temporary filter views are the
  intended user workflow.
- Treating all unusual rows as excluded. Some unusual rows are intentionally
  included in summary.
- Assuming `finpay_transactions` contains all processed rows. It only contains
  summary-included rows.
- Persisting source `No` or adding surrogate IDs to FinPay tables.
- Replacing transaction `Remarks` with diagnostic text. Use `unusual_reason`.
- Relabeling reversal rows again in downstream paths when preprocessed labels
  are already present.
- Reintroducing parallel sheet writes and hitting Sheets API rate limits.
