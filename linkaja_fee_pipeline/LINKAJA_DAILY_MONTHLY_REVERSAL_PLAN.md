# LinkAja Daily Evidence and Monthly Finalization Plan

Status: approved design plan; runtime implementation pending.

This plan defines how LinkAja should retain daily platform evidence and manually
produce a reversal-aware finalized result each month. It supplements the current
runtime contract in `README.md` and the historical July plan in
`reversal-close.md`.

## Executive Summary

The target is not to postpone every reversal calculation until month end.

```text
Daily workflow
  validate and normalize each source file
  persist versioned ledger evidence
  aggregate current transaction facts
  expose currently known reversal and unresolved states
  identify finalized months affected by late/corrected evidence

Monthly workflow, manually triggered after the close window
  select evidence using business and ingestion cutoffs
  freeze full original/reversal transaction facts
  freeze unresolved exceptions and approved waivers
  calculate Digipos/PPOB fees from the frozen facts
  publish an auditable final month
```

Daily processing still performs preprocessing and live reversal resolution.
Monthly processing performs cutoff-bound settlement, approval, waivers, and
final publication.

## 1. Reasoning and Evidence

LinkAja is an account ledger. One business transaction can produce multiple
rows because each affected account is reported separately. Calculations must
preserve ledger rows and aggregate them to:

```text
cluster_id + transaction_id
```

Reversals resolve only through:

```text
same cluster + exact Original Transaction ID
```

Amount, date, organization, counterparty, and scenario similarity are evidence
only and must never create an inferred relationship.

The source bundle under `data/linkaja_(07-2026)` contains:

| Measure | Observed value |
|---|---:|
| CSV files | 43 |
| Clusters | 6 |
| Ledger rows | 1,560,180 |
| Finalized range | 2026-07-01 through 2026-08-09 |
| Reversal targets | 2,425 |
| Exactly one original scenario | 1,837 |
| Missing original in the bundle | 588 |
| No original strictly earlier than reversal | 623 |
| Multiple original scenarios | 0 |

The folder contains August postings, so report months must be selected from
finalized timestamps, not folder names.

The broader `data/LinkAja` snapshots were searched for the 623 targets without
a prior original:

```text
22 acquired an original in a later snapshot
601 remained absent from all available snapshots
```

Waiting longer resolves some cases, but cannot resolve missing source history.
The close therefore needs source-coverage gates and a reviewed exception policy.

## 2. Approved Decisions

### Unresolved finalization

Unresolved reversals block finalization by default. Finance may waive only a
classified missing-history case after:

- the approved waiting window expires;
- required source coverage is complete;
- exposure is calculated;
- the relationship is not inferred;
- reviewer, reason, evidence, and timestamp are recorded.

The resulting publication status is `FINAL_WITH_WAIVERS`, not plain `FINAL`.

Chronology errors, self-references, cycles, cross-cluster references, multiple
reversals, and ambiguous originals remain blocking.

### Monthly scope

Freeze full transaction and reversal facts plus fee results. Do not freeze only
fee totals and continue using live exception queries.

### Cutoffs

Each monthly result uses two exclusive WITA cutoffs:

```text
business_cutoff
  event finalized timestamp must be earlier

evidence_cutoff
  source generation ingestion timestamp must be at or before the watermark
```

This makes an approved close reproducible as it was known at approval time.

## 3. Target Daily Workflow

### Step 1: validate and normalize

Preserve the exact 22-column contract, cluster validation, WITA date/time
parsing, decimal Debit/Credit/Fee handling, nullable Balance/Fee evidence, and
transaction-grain finalized-time coherence checks.

### Step 2: append source generations

Add:

```text
linkaja_source_loads
linkaja_ledger_versions
```

`linkaja_source_loads` records load ID, cluster, source file/hash, source date
range, row count, ingestion time, validity range, current/superseded state, and
validation status.

`linkaja_ledger_versions` retains every normalized ledger row under its load.
Old versions remain auditable after a corrected file replaces current state.

### Step 3: maintain compatibility current state

Continue updating `linkaja_raw_transactions` by affected transaction ID during
the migration. Replace it with a current-generation view only after parity is
proven. Daily reruns remain idempotent.

### Step 4: derive current transaction facts

Maintain one fact per `cluster_id + transaction_id`. Preserve ledger totals,
company Purchase Account movement, source Fee, nullable evidence, scenario,
status, source row count, and source files.

### Step 5: derive current reversal states

Use exact same-cluster source references and expose:

```text
NOT_REVERSAL
COMPLETE
UNRESOLVED_MISSING_ORIGINAL
LATE_ORIGINAL
INVALID_CHRONOLOGY
AMBIGUOUS_ORIGINAL
CROSS_CLUSTER_REFERENCE
SELF_REFERENCE
MULTIPLE_REVERSALS
AMOUNT_DIAGNOSTIC_MISMATCH
WAIVED_MISSING_HISTORY
```

`COMPLETE` requires the original finalized timestamp to be earlier. Amount
inversion remains diagnostic and never creates the edge.

### Step 6: record affected months

Add `linkaja_monthly_impacts` with old/new report months, affected transaction
IDs, impact reason, source loads, and timestamps. If a changed transaction can
affect an already published month, mark it `RESTATEMENT_REQUIRED`.

### Step 7: expose daily results

Continue returning daily live fee/detail results and unresolved counts through
PostgreSQL and Kestra artifacts. Daily ingestion never modifies a frozen monthly
snapshot.

## 4. Target Monthly Workflow

The monthly workflow remains a manual flow after month end and the approved
late-data waiting window.

### Inputs

```text
cluster_id
report_month
business_cutoff
evidence_cutoff
publish
preview_id
approved_by
approved_waiver_ids
```

### Step 1: lock cluster-month

Acquire a PostgreSQL advisory lock so preview/publication cannot overlap for the
same cluster-month.

### Step 2: verify source coverage

Compare required source bundle/range expectations with source generations valid
at the evidence cutoff. Validate hashes, row counts, clusters, date coverage,
and replacement state. Do not infer completeness from transaction activity.

### Step 3: build bitemporal facts

Use evidence satisfying:

```text
finalized_at_local < business_cutoff
ingested_at <= evidence_cutoff
```

Report-month facts use finalized month. Reversal edges may come from the closing
window while retaining actual timestamps.

### Step 4: freeze reversal edges

Create one edge per reversal with original/reversal IDs and timestamps, report
months, resolution status/reason, close scope, lag, diagnostic amount match, and
source load IDs.

### Step 5: apply waivers

Only approved `MISSING_IN_ALL_HISTORY` exceptions may become
`WAIVED_MISSING_HISTORY`. Preserve the missing relationship and exposure; never
invent an alternative original.

### Step 6: calculate active facts

A directly referenced completed reversal makes its original inactive. The
reversal contributes no fee. A principal reversal does not cancel a separately
posted fee without an exact source link.

### Step 7: calculate fees

Preserve the existing Digipos/PPOB contracts. Derive gross, reversed, active,
net, missing-fee, unresolved, and waived metrics only from frozen candidate
facts.

### Step 8: calculate release status

Use:

```text
PROVISIONAL
INCOMPLETE_SOURCE
INCOMPLETE_REVERSALS
READY_FOR_APPROVAL
FINAL
FINAL_WITH_WAIVERS
RESTATEMENT_REQUIRED
SUPERSEDED
```

`READY_FOR_APPROVAL` requires complete sources, passing data-quality/parity
checks, no blocking reversal state, and stable fingerprints.

### Step 9: persist preview

Save preview ID, cutoffs, fingerprints, totals, exceptions, model version, and
approval state. Preview must not mutate final snapshots.

### Step 10: publish atomically

Inside one PostgreSQL transaction:

1. reacquire cluster-month lock;
2. load approved preview and waivers;
3. revalidate source and calculation fingerprints;
4. recheck every release gate;
5. freeze transaction facts;
6. freeze reversal edges and exceptions;
7. freeze fee summary;
8. insert publication audit;
9. supersede prior publication;
10. commit.

## 5. Target Relations

Add immutable migrations for:

| Relation | Purpose |
|---|---|
| `linkaja_source_loads` | Versioned source generations and coverage. |
| `linkaja_ledger_versions` | Append-only normalized ledger evidence. |
| `linkaja_reversal_edges_current_v` | Current exact-ID reversal edges and states. |
| `linkaja_source_expectations` | Required source bundle/range coverage. |
| `linkaja_monthly_impacts` | Late/corrected evidence affecting published months. |
| `linkaja_monthly_previews` | Durable preview cutoffs, fingerprints, totals, and gates. |
| `linkaja_monthly_reversal_edges` | Frozen reversal edges. |
| `linkaja_monthly_exceptions` | Frozen unresolved and data-quality exceptions. |
| `linkaja_monthly_waivers` | Reviewed missing-history dispositions. |
| `linkaja_transactions` | Frozen full transaction facts. |
| `linkaja_monthly_refreshes` | Extended publication and supersession audit. |

`linkaja_monthly_fee_summary_v` must read only one frozen publication's facts
and exceptions. It must not combine frozen fee totals with live unresolved rows.

## 6. Waiver Contract

Default:

```text
unresolved reversal -> final publication blocked
```

Waiver eligibility:

```text
MISSING_IN_ALL_HISTORY
waiting window expired
source coverage complete
exposure calculated
reviewer and reason supplied
source fingerprint unchanged
```

Persist waiver ID, cluster/month, reversal and original references, exception
class, exposure, reason, reviewer, approval timestamp, and source fingerprint.

## 7. Cutoff Study

Measure separately:

```text
event reversal lag
source arrival lag
late-original evidence lag
```

Report P50/P75/P90/P95/P99/max, cross-month rate, later-resolution rate, and
permanently unresolved rate by cluster/scenario.

The 601 still-missing originals prove the waiting window cannot resolve every
case. It determines waiver eligibility; it does not create a match.

## 8. Validation

Required fixtures include exact link, missing/late original, chronology error,
cross-cluster collision, multiple scenario, self-reference, cycle, multiple
reversals, multiple ledger rows, amount diagnostic mismatch, source gaps,
zero-activity source, corrected/moved/deleted transaction, cutoff boundaries,
fingerprint race, waiver approval/rejection, `FINAL_WITH_WAIVERS`, restatement,
fee parity, and daily parity.

## 9. Rollout

1. Add the next immutable migration and backfill source metadata.
2. Dual-write versioned evidence in shadow mode.
3. Replay all 43 July files and classify all 588 missing originals plus 35
   chronology cases; verify the 22 later resolutions.
4. Run a supervised provisional close and review exposure/waivers.
5. Publish one controlled cluster-month.
6. Load a late original and prove `RESTATEMENT_REQUIRED` without silent change.
7. Cut over only after daily and fee parity is clean.

## 10. Acceptance and Rollback

Release requires append-retained evidence, reproducible dual cutoffs, complete
source coverage, explicit chronology/graph states, blocking unresolved cases or
audited waivers, frozen facts/edges/exceptions/fees, restatement detection, July
parity, and Finance approval of source calendar, waiting window, and waiver
policy.

Rollback disables hardened final publication while keeping daily ingestion and
live views operational. Versioned evidence remains audit data. Partially
published months are marked `SUPERSEDED` or `RESTATEMENT_REQUIRED`, not deleted
silently.

## 11. 2026-09-24 Simulation Result

The local simulation replayed the LinkAja July source bundle, the latest
LinkAja snapshots through 2026-09-20, then rewrote the August daily summary and
detail sheets from the newest date-range generations.

```text
July source files processed: 43 (database only)
Latest LinkAja source ranges processed: 204 across six clusters
August-overlapping daily Sheet range writes: 120 SUCCESS
August monthly snapshots: 6
Monthly Google status: COMPLETE for all six clusters
August unresolved reversal targets in close windows: 878
```

The monthly sheets are named `LINKAJA MONTHLY <cluster> 2026-08` and marked
`PUBLISHED WITH EXCEPTIONS - REVIEW REQUIRED`. No unresolved reversal waivers
were entered, so these results are simulation publications for review, not an
Odoo-final close.

The shared spreadsheet target was `Salinan dari Monitoring Finpay & LinkAja`.
The daily summary uses the existing `LinkAja` tab and cluster-specific detail
tabs. Google-only tasks ran on host networking because the host could reach
Google APIs while the database bridge network timed out. PostgreSQL work stayed
on `mmpp-finance-network`.
