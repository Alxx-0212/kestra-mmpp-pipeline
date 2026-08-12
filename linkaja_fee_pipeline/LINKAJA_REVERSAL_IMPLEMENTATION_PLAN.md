# LinkAja Reversal and July Close Implementation Plan

Status: proposed implementation and validation plan, 2026-08-11.

This document maps the current LinkAja runtime, records the weaknesses found in
the July 1 through August 9 source bundle, and defines a staged path to a tested
July close. It does not approve a production dependency on dbt and does not
change the existing calculation contract by itself.

The immediate target is LinkAja only. FinPay remains outside this work until the
LinkAja model, close process, and July acceptance run are complete.

## Executive decision

Use the following ownership boundaries:

```text
Kestra       ingestion, retries, task evidence, cutoff, approval, publication
Python       exact source validation, normalization, atomic raw replacement
PostgreSQL   system of record, live operational facts, frozen monthly snapshot
dbt Core     parallel analytical models, reversal edges, tests, parity, lineage
Taipy        later read-only operational and close-review presentation
```

dbt must first run in a separate shadow schema. The existing Python/PostgreSQL
monthly calculation remains authoritative until the complete July dataset has
zero unexplained parity differences and production use is explicitly approved.

The present exact reversal rule remains unchanged: a reversal resolves only
through `Original Transaction ID` in the same cluster. Amount, date,
counterparty, and outlet are evidence only and must never create a relationship.

## Empirical July baseline

The source bundle under `data/linkaja_(07-2026)` contains 43 CSV files for six
clusters, with posting data from July 1 through August 9, 2026. Windows metadata
files ending in `:Zone.Identifier` are not inputs.

| Measure | Observed value |
|---|---:|
| Source ledger rows | 1,560,180 |
| Transaction-grain facts | 999,898 |
| Completed reversal transactions | 2,425 |
| Resolved reversals | 1,837 |
| Unresolved reversals | 588 |
| July-posted unresolved reversals | 475 |
| August 1-9 unresolved reversals | 113 |
| Resolved July original -> August reversal | 0 |
| Resolved reversal maximum elapsed time | 215,664 seconds |

Resolved calendar-day lag distribution:

| Calendar-day lag | Reversal edges |
|---:|---:|
| 0 | 1,829 |
| 1 | 7 |
| 2 | 1 |

Per-cluster resolved and unresolved totals through the exclusive August 10
cutoff:

| Cluster | Resolved | Unresolved |
|---|---:|---:|
| 411311 | 366 | 30 |
| 421306 | 283 | 149 |
| 421307 | 345 | 102 |
| 421315 | 287 | 146 |
| 421318 | 376 | 141 |
| 421320 | 180 | 20 |

These are acceptance baselines, not replacement business rules. In particular,
the 588 missing originals cannot be repaired by dbt, Taipy, a longer cutoff, or
amount matching. They require official source history, an upstream transaction
lookup, or an authorized exception disposition.

## Current implementation map

### Daily runtime

```text
linkaja_fee_pipeline_v1
  source_file + dry_run
        |
        v
  validate_and_normalize_linkaja_csv
    exact 22-column CSV validation
    cluster/date/time/amount normalization
    -> normalized_linkaja.csv
    -> linkaja_manifest.json
        |
        v  when dry_run=false
  persist_and_summarize_linkaja
    apply/verify immutable migrations
    acquire schema and cluster advisory locks
    COPY into a temporary raw stage
    reject transaction-grain conflicts
    collect original/reversal-related IDs and old dates
    replace raw rows by cluster_id + transaction_id
    collect new affected dates
    query live daily fee/detail results
    -> linkaja_database_result.json
```

The workflow entry point is
[`../linkaja_fee_pipeline.yml`](../linkaja_fee_pipeline.yml). Kestra imports the
public package through the compatibility facade
[`../linkaja_pipeline.py`](../linkaja_pipeline.py). The active implementation is
owned by [`processing.py`](processing.py), [`database.py`](database.py),
[`sql.py`](sql.py), and the immutable files under [`migrations/`](migrations/).

The daily database write is atomic. It updates raw history and live views but
never changes a frozen monthly snapshot.

### Database grain and derivation

```text
linkaja_raw_transactions
  grain: one source ledger row
        |
        v
linkaja_transaction_base_v
  grain: one completed cluster_id + transaction_id fact
  ledger totals + company Purchase Account movement + nullable Fee
        |
        v
linkaja_transactions_current_v
  live exact-ID reversal resolution and unusual state
```

Published month relations are separate:

```text
linkaja_transactions
  frozen report-month transaction facts at an exclusive WITA cutoff

linkaja_monthly_refreshes
  publication audit

linkaja_monthly_fee_summary_v
  canonical published Digipos/PPOB fee surface
```

### Monthly runtime

```text
linkaja_monthly_materialization_v1
  cluster_id + report_month + calculation_cutoff + publish
        |
        v
  apply/verify migrations
  acquire cluster lock
  create cutoff-consistent temporary month stage
    only facts posted inside report_month
    originals/reversals may resolve from all raw facts before cutoff
        |
        +-> daily report-month aggregates
        +-> unresolved rows from month_start through cutoff
        +-> monthly fee summary
        |
        +-> preview: no business snapshot write
        |
        `-> publish: replace one cluster-month + write audit
```

The workflow and artifact contract is defined by
[`../linkaja_monthly_materialization.yml`](../linkaja_monthly_materialization.yml):

- `linkaja_monthly_result.json`
- `linkaja_daily_raw_calculation.csv`
- `linkaja_unresolved_reversals.csv`

These names, the workflow/task IDs, existing inputs, existing output keys,
public Python imports, relation names, and fee semantics are public contracts.
New outputs must be additive.

## What is correct and must be preserved

- Raw ledger rows remain distinct from transaction-grain facts.
- Unknown scenarios remain available instead of disappearing from storage.
- Company Purchase Account movement remains separate from the opposite ledger
  side.
- `signed_amount` remains ledger debit minus ledger credit.
- Blank source Fee remains distinct from verified zero.
- Reversals link only through exact same-cluster IDs.
- Originals and reversals remain on their actual finalized timestamps.
- Daily replacement remains idempotent by `cluster_id + transaction_id`.
- A published cluster-month remains frozen until explicit republication.
- The monthly cutoff remains an exclusive local WITA timestamp.
- Digipos and PPOB monthly rules remain unchanged until the business decisions
  at the end of this document are answered.

## Confirmed weaknesses

| Priority | Weakness | Consequence | Planned response |
|---|---|---|---|
| Critical | 588 referenced originals are absent from all supplied facts | Their original scenario, interval, and fee relevance are unknowable | Obtain official source history/lookup and introduce a reviewed exception disposition, never inferred matching |
| Critical | `monthly_payable_complete` checks missing fee rows only | A result may appear complete while unresolved reversal review is open | Preserve the old key, add separate fee, parity, reversal-review, and release-readiness states |
| High | There is no one-row-per-reversal edge relation | Exact intervals, multiple reversals, and cross-month analysis are awkward | Build a dbt reversal-edge fact with one row per reversal transaction |
| High | Monthly scopes are mixed | July facts and July-through-August unresolved rows share one count | Split report-month exceptions, relevant late reversals, unrelated next-month events, and closing-window unresolved exceptions |
| High | No source-coverage/freshness contract proves the entire close bundle was ingested | A valid cutoff can still be based on incomplete exports | Record expected files, hashes, clusters, date coverage, row counts, and a source watermark |
| High | Publication is not blocked by unresolved or missing-fee state | `publish=true` can release an unreviewed close | Add a close-readiness validation gate after shadow parity is approved |
| High | A preview and later publication are fresh calculations | Raw state can change between approval and publication | Add a source fingerprint/high-water mark and recheck it inside publication |
| High | Refresh-candidate detection relies on current replacement rows | A correction moved out of July can leave a stale July snapshot undetected | Persist load/change audit evidence covering old and new affected months |
| Medium | Resolution has no chronology anomaly state | A self-reference, negative lag, chain, or cycle can look resolved | Add dbt tests and explicit review reason codes; use synthetic fixtures |
| Medium | Stage conflicts compare finalized date but not finalized time | A transaction can silently use the minimum inconsistent timestamp | Add finalized-time coherence validation |
| Medium | Blank Balance normalizes to zero | Balance evidence loses null/unknown semantics | Preserve blank Balance as null and add normalization tests |
| Medium | PPOB status uses gross missing count while payable uses active missing count | A reversed missing-fee row can incorrectly mark status incomplete | Make status depend on active missing count, with parity tests |
| Medium | Empty monthly categories disappear | Consumers cannot distinguish zero from missing result generation | Emit stable zero rows for both approved fee categories |
| Medium | dbt currently reads only the published snapshot | It cannot validate live facts or a preview candidate | Expand sources and build a shadow fact/mart DAG |
| Medium | PostgreSQL integration tests skip without `LINKAJA_TEST_DSN` | Default green tests do not prove database behavior | Make a disposable test database mandatory for the candidate acceptance run |

The existing current-flow gaps concerning Balance nulls, monthly status, stable
categories, freshness, and dashboard grants are also recorded in
[`TAIPY_DASHBOARD_PLAN.md`](TAIPY_DASHBOARD_PLAN.md).

## Required close scopes

Every reversal result must be assigned a scope without changing its actual
posting timestamp:

| Scope | Definition |
|---|---|
| `REPORT_MONTH_REVERSAL` | Reversal posted inside July |
| `RELEVANT_LATE_REVERSAL` | Reversal posted after July whose resolved original was posted in July |
| `NEXT_MONTH_REVERSAL` | Reversal and resolved original both posted after July |
| `REPORT_MONTH_UNRESOLVED` | Unresolved reversal posted inside July |
| `CLOSING_WINDOW_UNRESOLVED` | Unresolved reversal posted after July but before the cutoff; original month is unknown |

For the supplied bundle and an August 10 exclusive cutoff, the expected split
is 475 `REPORT_MONTH_UNRESOLVED` and 113
`CLOSING_WINDOW_UNRESOLVED`. There are zero known
`RELEVANT_LATE_REVERSAL` edges, so that behavior must also be proven with a
synthetic fixture.

## Target dbt model

dbt initially writes only to a dedicated schema such as
`linkaja_dbt_shadow`. Its database role receives `SELECT` on approved LinkAja
sources and create/write privileges only in that shadow schema.

```text
sources
  linkaja_raw_transactions
  linkaja_transaction_base_v
  linkaja_transactions_current_v
  linkaja_transactions
  linkaja_monthly_refreshes
  linkaja_monthly_fee_summary_v
        |
        v
staging
  stg_linkaja_raw_ledger
  stg_linkaja_transaction_base
  stg_linkaja_current_transactions
  stg_linkaja_monthly_transactions
  stg_linkaja_monthly_refreshes
        |
        v
intermediate/facts
  int_linkaja_transaction_facts_from_raw
  fct_linkaja_reversal_edges
  int_linkaja_original_reversal_rollup
        |
        v
marts
  mart_linkaja_reversal_intervals
  mart_linkaja_unresolved_reversals
  mart_linkaja_daily_activity
  mart_linkaja_daily_fee
  mart_linkaja_monthly_fee
  mart_linkaja_close_readiness
        |
        v
audit
  audit_linkaja_raw_to_base_parity
  audit_linkaja_current_transaction_parity
  audit_linkaja_monthly_fee_parity
  audit_linkaja_publication_parity
```

`fct_linkaja_reversal_edges` has one row per reversal transaction and should
expose at least:

```text
cluster_id
reversal_transaction_id
original_transaction_id
reversal_finalized_at_local
original_finalized_at_local
reversal_scenario
original_scenario
resolution_status
resolution_reason
elapsed_seconds
calendar_day_lag
lag_bucket
original_report_month
reversal_report_month
is_cross_month
close_scope
reversal_company_debit
original_company_credit
amount_matches
source_files
updated_load_id
```

`amount_matches` is audit evidence only. It must never participate in
`resolution_status`.

The first dbt implementation should use full-refresh views/tables. Incremental
models, source-freshness selectors, and pre-aggregation are optimization work
after correctness and rerun behavior are proven.

## Close-readiness contract

Keep `monthly_payable_complete` for compatibility, but do not present it as the
overall release decision. Add these states:

```text
fee_data_complete
source_bundle_complete
dbt_tests_passed
parity_passed
report_month_unresolved_count
closing_window_unresolved_count
open_reversal_review_count
reversal_review_status
source_fingerprint
release_ready
```

Recommended release rule:

```text
release_ready =
    fee_data_complete
    AND source_bundle_complete
    AND dbt_tests_passed
    AND parity_passed
    AND open_reversal_review_count = 0
    AND source_fingerprint_matches_approved_preview
```

An unresolved relationship need not be silently treated as resolved. It may be
closed only by loading official source data or by an authorized, auditable
disposition policy. Any review table must record disposition and evidence, not
allow an operator to invent an alternative original relationship.

## Reviewable implementation passes

### Pass 0: freeze decisions and baseline

Current behavior:

- The production rules are distributed across Python, migrations, runtime SQL,
  report SQL, and documentation.
- Existing July workbooks and current query results are evidence but are not an
  automated golden baseline.

Structural improvement:

- Confirm the business decisions at the end of this document.
- Freeze the cluster list, 43 source filenames, file hashes, date coverage,
  cutoff, runtime image, migration checksums, and current artifacts.
- Save current output as the baseline, clearly labeled as current behavior
  rather than approved truth.

Validation:

- Source bundle manifest accounts for every expected file exactly once.
- No Windows metadata stream is accepted as a CSV input.
- Baseline counts equal the empirical values in this document.

### Pass 1: add regression coverage for current behavior and defects

Current behavior:

- Unit/schema tests cover key contracts, but database integration tests are
  optional and the real July bundle has no test harness.

Structural improvement:

- Add normalization tests for blank Balance and finalized-time conflicts.
- Add PostgreSQL fixtures for negative lag, self-reference, multiple reversals,
  reversal chains, reversed missing-fee PPOB, empty fee categories, and a
  correction moved out of July.
- Add a full-bundle runner targeting only a disposable database.

Validation:

- Existing public contract tests remain green.
- All PostgreSQL integration tests execute rather than skip.
- Expected failures are captured before implementing fixes.

### Pass 2: build dbt in a shadow schema

Current behavior:

- The dbt scaffold projects only `linkaja_transactions`, an already-published
  result.

Structural improvement:

- Add the staging, reversal-edge, mart, and audit DAG above.
- Document grain and ownership on every model.
- Add generic and singular tests for IDs, uniqueness, chronology, resolution,
  cutoff scope, fee completeness, and parity.
- Keep runtime variables explicit for any cutoff-scoped analysis; never use the
  current clock implicitly.

Validation:

- `dbt parse` and `dbt build --select tag:linkaja` pass in the shadow schema.
- Production-owned relations receive no writes.
- Current operational facts and dbt facts have zero unexplained symmetric
  differences.

### Pass 3: correct operational reporting gaps additively

Current behavior:

- The existing runtime has the Balance, PPOB status, category stability, scope,
  readiness, and freshness gaps listed above.

Structural improvement:

- Fix blank Balance normalization.
- Extend stage time-coherence validation.
- Correct PPOB status to use active missing fees.
- Return stable zero rows for both approved monthly categories.
- Split unresolved counts by close scope.
- Add source bundle/load audit and source fingerprint evidence.
- Add new result keys and artifacts without renaming or removing existing ones.
- Implement database schema changes only as new numbered migrations; never
  modify migrations 001 through 006.

Validation:

- Before/after behavior differs only for explicitly approved defects and new
  additive outputs.
- Runtime preview and published-view calculations have data-level parity.
- Existing workflow IDs, inputs, task IDs, imports, artifacts, and keys remain
  valid.

### Pass 4: run the full July candidate test

Current behavior:

- Existing workbooks cover earlier analysis, but the whole close is not a
  reproducible pipeline acceptance test.

Structural improvement:

- Execute the test procedure below in a dedicated disposable database.
- Produce machine-readable parity, interval, scope, data-quality, and readiness
  artifacts for all six clusters.

Validation:

- Every acceptance gate in the July section passes or has an explicitly
  approved exception. Initial runs use `publish=false` only.

### Pass 5: add Kestra shadow validation

Current behavior:

- Neither production workflow invokes dbt.

Structural improvement:

- Add a separate `linkaja_dbt_validation_v1` workflow first.
- Run a pinned dbt/PostgreSQL image through Kestra's dbt CLI integration; do
  not add dbt to `linkaja-fee-pipeline:3.11`.
- Store `manifest.json`, `run_results.json`, `sources.json`, parity results,
  reversal intervals, and close readiness as execution evidence.
- During shadow operation, failure marks analytics stale but does not change or
  roll back a successful raw ingestion.

Validation:

- The validation flow is rerunnable and produces identical results on unchanged
  raw state.
- Disabling the flow restores the current production path with no data change.

### Pass 6: promote the monthly gate after explicit approval

Current behavior:

- `publish=true` calculates and publishes regardless of unresolved review or
  dbt parity.

Structural improvement:

- Run source coverage, dbt tests, parity, and readiness before publication.
- Recheck the approved source fingerprint while holding the publication lock.
- Keep the existing Python snapshot publication authoritative in this phase.
- Preserve the current fresh-calculation rule unless an approved-preview ID and
  fingerprint contract is explicitly adopted.

Validation:

- A failed gate cannot mutate `linkaja_transactions` or
  `linkaja_monthly_refreshes`.
- A successful publish still atomically replaces only one cluster-month.
- Republishing the same source state is idempotent.

### Pass 7: connect Taipy after the data contract is stable

Taipy reads only approved dbt/PostgreSQL marts through a SELECT-only role. It
does not ingest files, run dbt, schedule workflows, publish a month, or directly
write exception dispositions. Authentication remains at the company proxy or
approved application boundary as specified in
[`TAIPY_DASHBOARD_PLAN.md`](TAIPY_DASHBOARD_PLAN.md).

## Full July test procedure

### Environment

1. Provision a dedicated disposable PostgreSQL database and set
   `LINKAJA_TEST_DSN` only to that database.
2. Build or pin the current `linkaja-fee-pipeline:3.11` image.
3. Apply migrations 001 through the current candidate version.
4. Create an isolated `linkaja_dbt_shadow` schema and least-privilege dbt role.
5. Record source hashes for all 43 CSV files.

Never run the integration harness against production or a shared development
database; the existing integration suite truncates LinkAja-owned relations.

### Ingestion

1. Ingest every source window for clusters `411311`, `421306`, `421307`,
   `421315`, `421318`, and `421320` through the same normalization and
   persistence APIs used by Kestra.
2. Verify raw and transaction counts after every file.
3. Reingest the same files and prove that transaction-grain results are
   unchanged.
4. Repeat a candidate run in a different file order to prove replacement
   behavior is not order-dependent for this non-overlapping bundle.
5. Run dbt build and its data tests only after the full bundle is present.

### Cutoff cohorts

Run six `publish=false` previews for each cutoff:

| Cohort | Exclusive WITA cutoff | Purpose |
|---|---|---|
| Month boundary | `2026-08-01T00:00:00` | July postings only |
| Prior analysis window | `2026-08-05T00:00:00` | Compare the earlier through-August-4 state |
| Full supplied window | `2026-08-10T00:00:00` | Include all supplied postings through August 9 |

Expected aggregate reversal state by cohort:

| Cutoff | Reversals in scope | Resolved | Unresolved |
|---|---:|---:|---:|
| August 1 | 1,855 | 1,380 | 475 |
| August 5 | 2,109 | 1,579 | 530 |
| August 10 | 2,425 | 1,837 | 588 |

The current temporary snapshot contains July facts only, so transaction counts
should remain stable across the three cutoffs. Based on the supplied data, the
July monthly fee result should also remain stable because no resolved August
reversal references a July original. Any difference must be explained at edge
grain.

### Required parity checks

For every cluster and all-cluster aggregate:

- Raw ledger identity, row count, nullable values, and provenance.
- Transaction identity, timestamps, scenario, status, ledger totals, company
  movement, source Fee, and source row count.
- Exact resolved/unresolved reversal ID sets.
- Exact original/reversal pairs and timestamps.
- Daily rows by posting date and reversal-aware label.
- Monthly fee rows by cluster and category, including nulls and missing counts.
- Published-view results versus runtime preview results.
- No cross-cluster resolution and no amount-based resolution.
- No negative elapsed time in real resolved edges.
- The exact 1,829/7/1 calendar-day lag distribution.
- The exact 475/113 unresolved report-month/closing-window split.
- All cutoff differences attributable to facts finalized inside the added
  cutoff interval.
- Both approved fee categories present even when their values are zero.

### Required synthetic cases

The real bundle contains no resolved July-original/August-reversal pair, so it
cannot by itself prove the over-month rule. Retain or add disposable database
fixtures for:

- July original reversed in August: active at August 1, inactive at August 10.
- Reversal loaded before its original: unresolved, then resolved after official
  original ingestion, with refresh candidate and explicit republication.
- Reversal after the cutoff: must not affect the snapshot.
- Same transaction ID in another cluster: must not resolve.
- Multiple reversals of one original.
- Self-reference, negative lag, chain, and cycle anomaly states.
- Reversed PPOB original with missing Fee.
- Missing active PPOB Fee.
- Empty Digipos or PPOB category.
- Corrected July transaction moved outside July after publication.

### Initial July release result

The first full-bundle run must remain `publish=false`. With the current source
coverage, close readiness is expected to be `REVIEW_REQUIRED`, not complete,
until the unresolved policy is approved and the 475 July-posted unresolved
transactions are backfilled or dispositioned. The 113 closing-window unresolved
transactions must remain separately visible because their original month is
unknown.

## Promotion and rollback

### Promotion gates

1. Current unit and schema contract tests pass.
2. PostgreSQL integration tests execute without skips.
3. The full July baseline is reproducible.
4. dbt shadow models and tests pass.
5. Operational and dbt results have zero unexplained parity differences.
6. Fee semantics and unresolved release policy are approved.
7. Source bundle completeness and fingerprint are recorded.
8. Six cluster previews are reviewed before any publication.
9. Publication is tested only in the disposable database first.

### Rollback boundary

- Disable the shadow validation workflow or publication gate.
- Drop/rebuild only the dbt shadow schema if needed.
- Continue using the unchanged daily ingestion and Python monthly publication.
- Never edit an applied migration; correct it with the next numbered migration.
- Do not delete or rename existing artifacts, workflow IDs, tasks, outputs,
  relations, or public imports.
- If dbt later becomes a hard gate, make the gate explicitly switchable so the
  existing publication path can be restored without deleting raw or snapshot
  data.

## Decisions required before implementation

1. **Fee semantics:** Does reversal of `Digipos B2B Transfer` principal also
   cancel a separately posted `Digipos B2B Transfer Fee`, or does payable change
   only when the fee transaction itself is directly referenced by a reversal?
2. **Unresolved release policy:** Must every unresolved July reversal be backed
   by official source data, or can an authorized reviewer disposition and waive
   a frozen exception with evidence?
3. **Cutoff policy:** Confirm `2026-08-10T00:00:00` WITA as the July acceptance
   cutoff, and decide whether future cutoffs use a fixed number of days or a
   declared per-close date.
4. **Source recovery:** Can LinkAja supply earlier/missing original transaction
   history or an authoritative lookup endpoint?
5. **Approval scope:** Is monthly approval per cluster, as today, or one batch
   decision across all six clusters?
6. **Preview identity:** Must publication prove it is using the exact approved
   preview fingerprint, or is the current fresh recalculation intentionally
   retained?
7. **Anomaly severity:** Are multiple reversals, chains, cycles, self-reference,
   and negative lag hard blockers or review exceptions?
8. **dbt promotion:** After July parity, may dbt become a mandatory monthly
   validation gate while Python remains the publication implementation?

The recommended defaults are: direct fee reversal only until contrary source
evidence is approved; unresolved rows require documented review rather than
amount inference; August 10 is the frozen July test cutoff; approval remains
per cluster initially; and dbt begins as a shadow validator before becoming a
switchable publication gate.
