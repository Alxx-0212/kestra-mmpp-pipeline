# FinPay Reversal And Monthly Settlement Plan

Status: implementation staged; production rollout pending source replay and Finance approval

Implemented in this change:

- additive source-load and ledger-event migration;
- guarded exact-ID monthly preview and snapshot publication API;
- daily raw-load provenance wiring;
- QRIS detail eligibility aligned with summary filtering;
- manual monthly Kestra materialization flow;
- focused reversal, coverage, QRIS, and publication-contract tests.

The daily Google Sheet remains the operational cash-flow output. A real
September/cross-month source replay and an approved reversal waiting window are
still release gates.

This document records the design reasoning, pre-mortem, contracts, and delivery
plan for adding reversal-aware monthly settlement to FinPay without changing the
existing daily cash-flow workflow.

## 1. Objective

Keep the current daily FinPay workflow and its Google Sheets outputs for cash
flow monitoring while adding a PostgreSQL-backed monthly settlement layer for
Finance and Odoo.

The monthly layer must answer a different question from the daily sheet:

```text
Which eligible cash-in events remain payable after all known valid reversals
before an explicit close cutoff?
```

The daily sheet answers:

```text
What did the source file post on this operational report date?
```

Daily cash flow must not be rewritten retroactively when a later reversal is
loaded. Monthly settlement must be reproducible and may change only through an
explicit preview and republish.

## 2. Current FinPay Behavior

The active flow is `finance.finpay.finpay_daily_pipeline_v5` in
`finpay_pipeline.yml`.

```text
uploaded CSV/XLSX
-> filename cluster/date resolution
-> schema validation
-> debit/credit integrity validation
-> raw daily table replacement
-> label preprocessing
-> unusual detection
-> unusual Postgres/Sheet/Telegram output
-> calculation deduplication
-> QRISDUWIT detail output
-> reversal detail output
-> summary filtering
-> daily Postgres summary replacement
-> daily Google Sheet cash-flow and invoice output
```

The current daily data tables are useful evidence but are not a monthly close
model:

| Table | Current meaning |
|---|---|
| `finpay_raw_transactions` | Validated rows for one cluster and filename report date; reruns replace the batch. |
| `finpay_transactions` | Rows retained for the daily summary after filtering. |
| `finpay_unusual_transactions` | Pre-summary unusual rows, including rows that can remain included. |
| `finpay_reversal_transactions` | Reversal detail rows classified from remarks. |
| `finpay_qrisduwit_transactions` | Label-selected QRISDUWIT detail rows and parsed disbursement dates. |

Known limitations:

- No explicit `original_transaction_id` exists in the source or database.
- `base_id` only strips a terminal `FEE`, `SLSFEE`, or `SALESFEE` suffix.
- Reversal rows do not deactivate an original transaction.
- Raw and derived tables are independently replaced by `cluster_id + report_date`.
- Source file hash, load ID, source row locator, and prior versions are absent.
- The filename's second date is syntactically accepted but not used.
- Summary grouping is by processed label, not transaction event.
- QRIS invoice rows are selected before final summary exclusion and can diverge
  from the cash-flow summary unless the monthly path applies the same eligibility.

## 3. Current LinkAja Pattern

LinkAja provides reusable building blocks:

- raw ledger rows are separate from transaction-grain facts;
- exact `Original Transaction ID` links are scoped by cluster;
- originals and reversals retain actual posting timestamps;
- a valid reversal makes the original inactive without subtracting twice;
- live views are separate from cutoff-bound monthly snapshots;
- monthly replacement and audit rows are written transactionally;
- unresolved links remain visible instead of being inferred from amount or date.

LinkAja is not copied unchanged. Its current close has material gaps:

- unresolved reversals do not block publication;
- missing source coverage does not block publication;
- an empty month can be reported as complete;
- preview and publication are not bound to the same source fingerprint;
- current-only refresh candidates can miss data corrected out of a month;
- self-links, negative chronology, chains, and cycles are not blocked;
- the official report combines frozen fee results with a live exception query.

FinPay adopts the strong transaction and cutoff mechanics, but adds explicit
coverage, fingerprint, graph-validity, and final-release gates.

## 4. Approved Finance Decisions

### 4.1 Monthly scope

The first monthly report covers **all cash-in categories**. Domain-specific
categories remain separate in the canonical facts and are standardized only in
the read model:

```text
platform
cluster_id
report_month
settlement_category
gross_amount
reversed_amount
net_payable
```

FinPay categories initially include NGRS principal, NGRS fee, out-cluster
cash-in, QRISDUWIT, and other approved cash-in labels. LinkAja categories remain
owned by LinkAja. The consolidated finance report must not move business rules
between domains.

### 4.2 FinPay reversal linkage

Use a guarded exact-ID match:

```text
same cluster
+ exact Transaction ID
+ non-reversal original exists
+ original timestamp is earlier
+ reversal timestamp is later
+ debit/credit inversion is consistent
```

The observed 411311 corpus supports exact-ID matching at transaction-group
grain, but not row grain. Repeated and multi-row IDs remain possible.

Never infer a link from:

- suffix-stripped `base_id`;
- equal amount alone;
- equal date or outlet;
- remarks similarity;
- customer or counterparty fields.

### 4.3 Monthly lifecycle

Monthly processing uses provisional and final states:

```text
PROVISIONAL
INCOMPLETE_SOURCE
INCOMPLETE_REVERSALS
READY_FOR_APPROVAL
FINAL
SUPERSEDED
```

The close uses an explicit exclusive Asia/Makassar cutoff. A month can be
previewed before the waiting window is decided, but final publication is blocked
until source coverage and relevant reversal evidence are complete or formally
resolved.

### 4.4 Release policy

An Odoo-ready final month is blocked when:

- expected source days or files are missing;
- a relevant reversal is unresolved or ambiguous;
- chronology, amount inversion, self-link, chain, or cycle checks fail;
- QRIS disbursement date is missing for a payable QRIS row;
- the preview fingerprint differs from publication input;
- category reconciliation does not equal transaction detail.

Exceptions stay visible in the provisional result. They are not silently
converted to zero or excluded from the evidence tables.

## 5. Target Architecture

```text
daily source load
-> append-only source manifest and ledger evidence
-> current transaction-event view
-> guarded reversal-status view
-> daily compatibility tables and Sheets (unchanged contract)

monthly request
-> choose report month and exclusive WITA cutoff
-> select current evidence as of cutoff
-> calculate active-at-cutoff facts
-> build exception and coverage gates
-> preview
-> approve and publish fingerprint-bound snapshot
-> render monthly finance report
```

PostgreSQL is the canonical calculation store. Google Sheets is a presentation
surface. Kestra orchestrates ingestion, preview, publication, and artifacts.

## 6. Proposed Relations

The first implementation uses explicit migration-owned relations. Ordinary daily
table reconciliation must not silently create or drop these structures.

### `finpay_source_loads`

One row per uploaded source generation:

```text
load_id
cluster_id
source_file
source_sha256
report_date
source_start_date
source_end_date
row_count
loaded_at
is_current
supersedes_load_id
```

The current flag lets a corrected upload supersede an earlier generation while
retaining its evidence for audit and lag analysis.

### `finpay_ledger_events`

Append-only source evidence:

```text
load_id
source_row_number
source_row_hash
cluster_id
report_date
transaction_date
transaction_id
raw_transaction_label
transaction_type
remarks
kredit
debet
saldo_awal
saldo_akhir
nomor_rs
```

The source row number is a locator, not a business key. The content hash and
load ID are required to distinguish a correction from a duplicate ingestion.

### `finpay_transaction_events_current_v`

Groups current ledger evidence by exact cluster and transaction ID while
retaining:

- event timestamp and report dates;
- raw label and remarks evidence;
- signed source amount;
- source row count and load IDs;
- fee/main component indicators;
- current source fingerprint.

### `finpay_reversal_status_current_v`

Adds guarded reversal state:

```text
NOT_REVERSAL
COMPLETE
UNRESOLVED_MISSING_ORIGINAL
AMBIGUOUS_MULTIPLE_ORIGINALS
INVALID_CHRONOLOGY
AMOUNT_MISMATCH
MULTIPLE_REVERSALS
```

The original is active at a cutoff only when no valid linked reversal finalized
before that cutoff. A reversal event contributes zero payable but remains in
gross evidence.

### `finpay_monthly_transactions`

Frozen transaction facts keyed by month, cluster, event, and event timestamp.
It must include `report_month` in its uniqueness contract. It stores the
cutoff, materialization ID, source fingerprint, active state, reversal status,
and classification version.

### `finpay_monthly_publications`

Audit and release state:

```text
publication_id
cluster_id
report_month
calculation_cutoff
source_fingerprint
source_day_count
missing_source_day_count
unresolved_reversal_count
blocking_exception_count
status
previewed_at
published_at
approved_by
```

## 7. Monthly Calculation Contract

For each eligible category:

```text
gross payable
  = sum of eligible original event values

reversed adjustment
  = sum of distinct eligible originals with a valid reversal before cutoff

active net payable
  = gross payable - reversed adjustment
```

The monthly result must also expose:

```text
gross_transaction_count
reversed_transaction_count
active_transaction_count
gross_amount
reversed_amount
net_payable
unresolved_count
unresolved_exposure
missing_source_count
calculation_status
```

QRISDUWIT uses its disbursement date as the settlement-date basis. Missing or
invalid disbursement dates block that category from final publication. QRIS rows
must use the same eligible/current fact set as the summary; detail-only rows
must never re-enter the invoice.

## 8. Cutoff Study

Before selecting D+N, measure both platform reversal lag and source-file
availability lag:

```text
reversal lag = reversal finalized timestamp - original finalized timestamp
source lag   = ingestion timestamp - source event timestamp
```

For every platform, cluster, and category report P50, P75, P90, P95, P99,
maximum, same-day rate, cross-day rate, cross-month rate, unresolved age, and
missing source coverage. The final waiting window is chosen from the slower
operational distribution and approved by Finance.

Late data after `FINAL` becomes a restatement candidate. It must not silently
rewrite the frozen snapshot.

## 9. Pre-Mortem

### Tigers: launch blockers

| Failure | Prevention |
|---|---|
| Same ID links to the wrong original | Exact ID plus chronology, role, and inversion gates |
| Multiple reversals subtract twice | Distinct original cancellation plus blocking repeated-reversal state |
| Missing source looks like zero activity | Expected source manifest and coverage gate |
| Preview differs from publication | Source fingerprint bound to publication |
| Corrected data leaves a stale month | Append-only loads and before/after impact tracking |
| Unresolved reversal is silently ignored | Final release gate |
| QRIS exception appears in invoice | Invoice reads only eligible monthly facts |
| Daily flow changes during rollout | Shadow monthly layer and daily parity checks |
| Frozen summary and live exceptions disagree | Freeze exceptions and facts together |

### Paper tigers: deliberately out of scope

- Replacing the existing daily Google Sheet.
- Inferring missing originals from amount, date, or remarks.
- Moving FinPay business logic into LinkAja modules.
- Making dbt a runtime dependency before parity is proven.
- Direct Odoo writes before Finance approves the published report.

### Elephants: explicit assumptions

- Exact FinPay ID behavior is empirically supported but not a provider contract.
- Missing history biases reversal-lag estimates.
- Some reversals may never resolve because their originals predate retained data.
- Finance must approve category-to-Odoo account mapping.
- Late changes after final close require a restatement policy.

## 10. Delivery Phases

### Phase A: evidence and compatibility

1. Keep the daily tables and Sheets unchanged.
2. Append source manifests and ledger events on each successful daily load.
3. Add current event and reversal-status views.
4. Fix QRIS monthly eligibility to use the same filtered disposition as summary.
5. Verify daily output parity.

### Phase B: monthly preview

1. Add an explicit monthly preview entry point.
2. Select current evidence by cluster, month, and exclusive cutoff.
3. Produce transaction detail, category totals, unresolved reversals, and source
   coverage.
4. Never mutate frozen monthly tables during preview.

### Phase C: final publication

1. Require a matching preview fingerprint.
2. Require all release gates to pass.
3. Replace exactly one cluster-month snapshot transactionally.
4. Record publication audit and immutable source fingerprint.

The current implementation publishes PostgreSQL snapshots and CSV/JSON
artifacts first. A dedicated monthly Google Sheet renderer remains gated on
Finance approving the monthly worksheet layout; it must read the frozen snapshot
and never recalculate payable values independently.

### Phase D: cutoff calibration

1. Replay historical source bundles.
2. Measure reversal and ingestion lag distributions.
3. Recommend D+N separately by platform/category when evidence requires it.
4. Obtain Finance approval before marking any month final.

## 11. Rollback And Non-Goals

Rollback means disabling monthly publication and continuing the existing daily
workflow. Append-only source evidence remains useful and is not deleted. The
monthly snapshot can be marked `SUPERSEDED`; daily tables are not reconstructed
from it.

This change does not:

- modify LinkAja production files;
- write directly to Odoo;
- delete current FinPay tables;
- infer missing reversal relationships;
- make the daily report reversal-adjusted;
- declare a final cutoff before lag analysis.

## 12. Acceptance Criteria

- Existing daily FinPay workflow IDs, inputs, task IDs, and Sheet layout remain
  compatible.
- Every uploaded generation has a source manifest and row-level provenance.
- Current event views are reproducible from current source loads.
- Guarded exact-ID states are deterministic and tested for missing, ambiguous,
  repeated, invalid-chronology, and amount-mismatch cases.
- QRIS rows excluded by classification cannot appear in monthly invoice output.
- Provisional results never mutate final snapshots.
- Final publication blocks incomplete source or reversal evidence.
- Preview and publication require the same source fingerprint.
- Monthly snapshot uniqueness includes report month.
- Daily parity and idempotent rerun checks pass.
- A real September and cross-month reversal fixture is validated before release.

## 13. 2026-09-19 E2E Replay

The local Kestra/PostgreSQL replay used cluster `411311` and the 48 Excel files
from `2026-08-01` through `2026-09-17`:

```text
rows loaded: 61,215
transaction timestamps: 2026-08-01 04:16:29 through 2026-09-17 22:16:36
current source generations: 48
current ledger rows: 61,215
reversal event groups: 62
complete reversal links: 62
unresolved reversal links: 0
```

The first August generation was retried after a Kestra upload-handle filename
issue; the superseded generation remains in `finpay_source_loads`, while only
the latest 48 generations are current.

The August preview passed all gates and was published as a PostgreSQL `FINAL`
snapshot with 31 covered source days, zero unresolved reversals, and zero
blocking exceptions. The September 1-17 preview is `READY_FOR_APPROVAL` with
zero blockers but remains provisional because the calendar month is incomplete.

The first Google-enabled attempt hit a transient TLS read timeout while
refreshing the service-account token at `oauth2.googleapis.com:443`. A retry
after network reachability recovered succeeded through the unusual, QRISDUWIT,
Reversal, daily summary, and monthly worksheet writes using the authorized
`Salinan dari Monitoring Finpay & LinkAja` spreadsheet. The other 47 daily
replays intentionally remained database-only; the monthly August publication
was Google-enabled after its refreshed source fingerprint passed the gate.

The Google client now performs a discovery-endpoint preflight, uses bounded
connect/read timeouts, and retries transient HTTP/network failures. The August
monthly worksheet was rewritten with the daily monitoring palette, frozen header,
section fills, alternating category rows, numeric formats, borders, and output
protection.

## 21. 2026-09-24 Simulation Refresh

After the August daily Sheet replay, a final category audit found 346
`FeeTransaksi` rows were excluded because in-memory labels were normalized to
uppercase while the known-label and remark-pattern maps still used mixed case.
Both keys are now canonical uppercase and a regression fixture verifies the
observed remark structure.

The corrected local simulation then:

```text
FinPay daily August uploads: 31/31 SUCCESS with Google writes
FinPay post-month reversal-window data: Sep 1-17, DB-only
FinPay August source coverage: 31 report days
FinPay reversal evidence window: 48 days (Aug 1-Sep 17)
FinPay unresolved/multiple reversals: 0 / 0
FinPay FEETRANSAKSI restored: 346 daily/monthly eligible rows
FinPay monthly snapshot: FINAL in the local simulation DB
FinPay monthly Google worksheet: COMPLETE

LinkAja July source bundle: 43 files loaded DB-only
LinkAja latest snapshot generations: 204 ranges loaded through Sep 20
LinkAja August daily Google writes: 120 range executions SUCCESS
LinkAja August monthly summaries: 6 cluster snapshots and worksheets COMPLETE
LinkAja unresolved reversal targets: 878 across the six monthly close windows
```

The LinkAja monthly worksheets are explicitly marked
`PUBLISHED WITH EXCEPTIONS - REVIEW REQUIRED`; they are not Odoo-final because
no missing-history waivers were approved. The August source bundle is rendered
in `Salinan dari Monitoring Finpay & LinkAja` using the existing daily monitoring
styles plus dedicated monthly tabs.

All writes were to the local simulation database/Kestra stack and the named
spreadsheet copy. They are not production deployment or Finance approval. The
local Google writer tasks use host networking because Google egress from
`mmpp-finance-network` timed out while host-network OAuth/Sheets calls succeeded.

## 14. P0 Hardening Implementation Plan

P0 is the work required before a monthly result can be treated as an
authoritative Odoo settlement. It protects amount correctness, source
completeness, cutoff reproducibility, and publication consistency. It does not
replace the daily cash-flow workflow or introduce direct Odoo writes.

### P0.1 Canonical transformation ownership

The PostgreSQL transaction model becomes the canonical monthly transformation
source. Kestra remains orchestration and delivery; Python remains an adapter for
inputs, small aggregations, and artifacts.

The implementation must remove the current duplicate reversal definitions:

```text
finpay_ledger_events
-> SQL event grouping
-> SQL guarded reversal status
-> cutoff-aware monthly facts
```

`monthly.py` must consume the canonical event/status result rather than
reimplementing event grouping and reversal matching in Pandas. The SQL views and
the Python resolver must not be allowed to disagree about an original's active
state.

### P0.2 Eligible reversal enforcement

Add eligibility and disposition fields to the canonical event result:

```text
eligible
disposition
reversal_resolution_status
blocking_exception
```

Only a reversal that is:

- classified and eligible;
- in the same cluster;
- linked by exact transaction ID;
- chronologically valid;
- amount-inverting;
- associated with exactly one prior original;

may cancel an original.

An ineligible, unknown, or quarantined reversal remains evidence but cannot
reduce monthly payable.

### P0.3 Multiple-reversal blocking

Add a blocking `MULTIPLE_REVERSALS` state at original-event grain. The release
gate must count both:

```text
unresolved reversal events
multiple-reversal originals
```

An original may be cancelled only once for amount calculation, but the month
cannot become `READY_FOR_APPROVAL` while repeated reversal evidence is present.

### P0.4 Monthly deduplication contract

Define one monthly deduplication key and apply it before event grouping:

```text
cluster_id
+ exact transaction ID
+ original/reversal role
+ transaction timestamp
+ source content hash
```

Rows removed as duplicates must be retained in an exception count and exposure
summary. Daily and monthly deduplication must share the same contract and tests.

### P0.5 Manifest-based source coverage

Add an explicit expectation relation:

```text
finpay_monthly_source_expectations
```

Suggested grain:

```text
cluster_id
report_month
expected_source_date
expected_file_key
required
```

The monthly gate must compare expectations against current source manifests,
not only against dates containing transaction rows. A zero-activity source day
is complete only when its required source generation is present and validated.

Add a partial unique current-generation constraint for:

```text
cluster_id + report_date
```

and reject overlapping current source generations unless the replacement is an
explicit supersession.

### P0.6 Complete-period validation

Before final publication:

- expected dates must cover the complete approved settlement period;
- report month must be a complete closed period, unless Finance explicitly marks
  the result provisional;
- every required source date must have a validated current generation;
- row counts and source hashes must match the manifest;
- a caller must not be able to pass an arbitrary partial expected-date subset
  and obtain `READY_FOR_APPROVAL`.

The status model becomes:

```text
PROVISIONAL
INCOMPLETE_SOURCE
INCOMPLETE_REVERSALS
READY_FOR_APPROVAL
FINAL
RESTATEMENT_REQUIRED
SUPERSEDED
```

### P0.7 QRIS settlement-month correctness

Persist the parsed `disbursement_date` in the monthly fact. For QRIS:

```text
settlement_month = disbursement_date month
```

not the transaction-posting month. Missing or invalid disbursement dates block
only after confirming the row is otherwise payable; quarantined/reversed rows
must not create false QRIS blockers.

The daily cash-flow view remains posting-date based. This rule applies to the
monthly Odoo settlement layer only.

### P0.8 Durable preview records

Extend `finpay_monthly_publications` or add a dedicated preview relation with:

```text
preview_id
cluster_id
report_month
calculation_cutoff
expected-period fingerprint
source fingerprint
calculation fingerprint
classification version
coverage status
reversal status
category totals
approved_by
previewed_at
published_at
```

`publish=true` must reference a durable preview record rather than trusting an
unstructured caller-provided result dictionary.

### P0.9 Fingerprint and publication locking

Publication must revalidate the preview inside one PostgreSQL transaction:

1. acquire a cluster-month advisory lock;
2. lock or snapshot the current source-generation set;
3. recalculate the source and calculation fingerprints;
4. reject the publication if either fingerprint differs;
5. recheck every release gate;
6. replace the cluster-month snapshot;
7. commit the publication audit and status together.

The calculation fingerprint must include:

```text
source fingerprint
report month
cutoff
expected source period fingerprint
classification version
SQL/model version
```

### P0.10 Frozen reversal and exception evidence

The final monthly snapshot must contain both payable facts and the evidence that
explains their state. Add either event-role rows or a companion exception table
for:

```text
original events
valid reversal events
unresolved reversals
ambiguous originals
amount mismatches
multiple reversals
quarantined/duplicate rows
```

Each row must retain the source fingerprint and publication ID. A final report
must be explainable without querying mutable live views.

### P0.11 Separate database and Google publication status

Track these states independently:

```text
database_status
google_status
```

Examples:

```text
DB_READY_GOOGLE_PENDING
DB_FINAL_GOOGLE_FAILED
DB_FINAL_GOOGLE_COMPLETE
```

PostgreSQL remains the canonical settlement result. Google retries must rewrite
the same publication ID and worksheet without recalculating or changing the DB
snapshot. A Google failure must not hide a successful DB commit.

### P0.12 Enforce Asia/Makassar timestamps

Change event and cutoff semantics to explicit timezone-aware values:

```text
TIMESTAMPTZ in PostgreSQL
Asia/Makassar normalization at ingestion
exclusive cutoff comparison
```

Reject ambiguous or timezone-less API cutoffs unless the workflow explicitly
interprets them as Asia/Makassar. Add boundary tests for:

```text
one second before cutoff
exactly at cutoff
one second after cutoff
month-end local midnight
```

### P0.13 Restatement detection

When a current source generation or reversal status changes after a month is
`FINAL`:

1. identify affected report months;
2. mark their publication `RESTATEMENT_REQUIRED`;
3. record the changed source fingerprint and reason;
4. prevent silent Google/Odoo replacement;
5. require an explicit preview and approval before republish.

This applies to backdated originals, backdated reversals, source replacements,
and deleted/corrected rows.

## 15. P0 Files And Ownership

Expected FinPay-owned changes:

| File/area | Change |
|---|---|
| `finpay_pipeline/migrations/` | Tables, constraints, indexes, timezone columns, preview/publication metadata, expectations, frozen exceptions |
| `finpay_pipeline/monthly.py` | Consume canonical SQL facts, enforce status/gate contract, produce artifacts |
| `finpay_pipeline/database.py` | Migration runner, source-manifest helpers, bulk persistence, publication lock helpers |
| `finpay_pipeline/queries/` | Coverage, reversal, reconciliation, and restatement queries |
| `finpay_monthly_materialization.yml` | Preview/final inputs, approval/fingerprint contract, separate DB/Google status handling |
| `finpay_pipeline.yml` | Preserve daily behavior; add only compatible source-manifest and preflight changes |
| `finpay_pipeline/summary_sheets.py` | Read-only rendering of frozen monthly rows; no independent settlement formulas |
| `tests/test_refactor_contracts.py` | Unit and fixture-level correctness tests |
| `tests/test_finpay_workflow_contracts.py` | Kestra input/task/output contracts |
| `finpay_pipeline/README.md` and this plan | Operational and accounting contract |

No LinkAja calculation files are changed by the FinPay P0 implementation.

## 16. P0 Test Matrix

Required deterministic fixtures:

1. Eligible exact-ID reversal.
2. Ineligible reversal that must not cancel an original.
3. Missing original.
4. Invalid chronology.
5. Amount mismatch.
6. Two reversals for one original.
7. Multiple component rows with one event timestamp.
8. Duplicate source file replay.
9. Corrected source generation that removes a row.
10. Original in one month and reversal in the next.
11. Reversal exactly at the cutoff.
12. QRIS disbursement in a different month.
13. Missing QRIS disbursement on a payable row.
14. Complete source expectation with zero transaction rows.
15. Missing required source generation.
16. Partial expected-date input rejection.
17. Fingerprint change between preview and publish.
18. Google publication failure after DB commit.
19. Restatement detection after final publication.
20. Timezone boundary around Asia/Makassar month end.

Required checks:

```text
daily compatibility parity
monthly SQL/Python parity during migration
transaction-level reconciliation
publication idempotency
source-generation supersession
PostgreSQL rollback
Google retry without recalculation
EXPLAIN plan for monthly event query
```

## 17. P0 Rollout

### Stage 1: shadow calculation

- Keep daily flow unchanged.
- Populate new facts and P0 statuses.
- Produce monthly previews without final publication.
- Compare current monthly result with the shadow result by event and category.

### Stage 2: supervised provisional close

- Enable complete source expectations.
- Run a full historical replay.
- Require Finance to review unresolved and repeated-reversal exceptions.
- Write a provisional monthly Google worksheet.

### Stage 3: controlled final publication

- Enable advisory locking and fingerprint revalidation.
- Require non-empty approval identity.
- Require all P0 gates to pass.
- Publish one approved cluster-month.
- Verify PostgreSQL and Google status independently.

### Stage 4: backdated restatement drill

- Load a reversal after a month is final.
- Confirm the affected month becomes `RESTATEMENT_REQUIRED`.
- Confirm no silent Google/Odoo replacement occurs.
- Approve and republish explicitly.

## 18. P0 Acceptance Criteria

P0 is complete only when:

- an ineligible reversal cannot reduce payable;
- multiple reversals block final release;
- monthly duplicate source rows do not inflate totals;
- coverage is proven by manifests and complete expected periods;
- QRIS is assigned to disbursement month;
- preview and publication are durable and fingerprint-bound;
- final snapshots contain reversal and exception evidence;
- PostgreSQL and Google status are independently visible;
- all cutoff comparisons are Asia/Makassar deterministic;
- backdated changes trigger an explicit restatement state;
- a full 411311 historical replay passes all fixtures and parity checks;
- no Odoo-facing final release occurs until Finance signs off.

## 20. P0 Implementation Status

Implemented in the current FinPay lane:

- exact-ID reversal cancellation requires eligible reversal and original facts;
- repeated reversals produce a blocking original-event state;
- monthly exact source-row replay deduplication is applied before grouping;
- expected source dates are persisted and partial calendar periods cannot become
  ready;
- QRIS monthly facts use parsed disbursement dates when valid;
- preview metadata is persisted before publication;
- publication uses cluster-month advisory locking and source-fingerprint
  revalidation;
- monthly snapshots retain reversal evidence and linked original event IDs;
- PostgreSQL and Google publication states are tracked independently;
- cutoffs are normalized to Asia/Makassar semantics;
- changed source fingerprints mark finalized months as
  `RESTATEMENT_REQUIRED`.

Still required before production final release:

- deploy the migration and updated monthly flow to the on-premises Kestra
  environment;
- run the complete historical replay and Finance parity review;
- validate SQL/Python parity before removing duplicate transformation paths;
- perform the backdated-reversal restatement drill;
- obtain Finance approval for expected source calendars and Odoo category
  mappings;
- benchmark the monthly query and move high-volume event resolution into
  indexed PostgreSQL SQL before a 10x volume increase.

## 19. Rollback

Rollback keeps the current daily workflow active and disables monthly final
publication. P0 migration tables and ledger evidence remain read-only audit data.
Existing daily compatibility tables and Google worksheets are not reconstructed
from monthly snapshots. Any partially published cluster-month is marked
`SUPERSEDED` or `RESTATEMENT_REQUIRED`, never deleted without an audit record.
