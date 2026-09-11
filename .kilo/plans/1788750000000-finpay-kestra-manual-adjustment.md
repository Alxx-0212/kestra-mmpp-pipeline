# FinPay Missing-Data Correction Through Kestra

## Purpose And Latest Decision

Refined on 2026-09-08 to reflect the actual operating process:

1. The regular Kestra Top-Up workflow downloads DigiPOS CMS data into staging and calculates running saldo.
2. If running saldo differs from CMS BUCKET, finance does not accept the refresh. Finance opens an external ticket for the operator to investigate.
3. The operator examines the downloaded data, checkpoint, CMS snapshot, and finance evidence. Missing or incomplete DigiPOS transactions are one possible cause, not a conclusion inferred from the difference alone.
4. After confirming missing source data and the correction amount, the operator manually runs a dedicated Kestra correction workflow.
5. That workflow inserts an audited synthetic adjustment into its separate database table and releases the investigated pending refresh for a fresh download.
6. The next regular Top-Up workflow includes the active adjustment, downloads current source data, deduplicates real source rows, and recalculates running saldo.
7. Finance accepts the new regular refresh only after reviewing its new reconciliation evidence. That regular acceptance commits real source staging and updates the checkpoint.

The correction workflow succeeding means **correction recorded**, not **CMS reconciliation verified**. It does not guarantee that the next refresh will match.

## Changes From The Previous Plan

- Keep one new manually triggered flow: `finance.finpay.finpay_manual_adjustment_v1`.
- Remove `finpay_topup_reverify_v1`, automatic Subflow execution, and both Pause/Resume approval gates from this phase.
- Finance investigation and ticket authorization happen outside Kestra. The authorized operator explicitly confirms application when starting the correction flow.
- Do not require a second person to resume the correction inside Kestra. Preserve ticket authorization and actual operator identity; production maker/checker enforcement is a separate access-policy decision.
- The next independently triggered regular refresh performs validation. No correction task starts a refresh, downloads source data, fetches a new CMS snapshot, or approves source staging.
- Manual checkpoint editing is not part of missing-transaction correction. Existing checkpoint-override primitives remain separate and require their own investigated procedure.
- No new Telegram command, message, approval callback, or ticket-system integration is added. Existing regular refresh notifications remain unchanged.

## Existing Code And Gaps

Relevant existing files:

- `manual_adjustment.py`: proposal, approval, void, and checkpoint-override helpers.
- `schema.py`: separate adjustment and checkpoint-override tables.
- `saldo.py`: approved adjustments participate in the calculation alongside committed and current staging rows.
- `audit.py`: `start_refresh` blocks overlapping nonterminal refreshes.
- `review.py`: acceptance commits staging and advances the checkpoint; rejection purges only that refresh's staging.
- `workflows/finpay_topup_pipeline.yml`: the regular extraction, staging, verification, recovery, and notification stages.

The existing primitives are not a complete safe correction workflow. Individual helpers commit independently; there is no ticket-item retry key, atomic correction-plus-pending-refresh closure, durable evidence handoff, or complete late-source-arrival lifecycle. These are implementation requirements below, not claims about shipped behavior.

## Financial Contract

### An Adjustment Represents Missing Data

- The business term "dummy data" means an explicit synthetic correction, never a row masquerading as a DigiPOS transaction.
- Store it only in `finpay_topup_manual_adjustment`, not `finpay_topup_txn` or source staging.
- Only active `APPROVED` adjustments contribute to running saldo; `PROPOSED`, `REJECTED`, and `VOID` rows do not.
- Use `Kredit` to increase saldo and `Debit` to decrease saldo. Accept a strictly positive finite Decimal amount with at most two decimal places, within the database numeric range.
- Preserve real-source `row_hash` behavior. Synthetic correction identity is separate from source deduplication.
- Order by `transaction_date ASC` only, using the adjustment's `effective_at` as its calculation timestamp. Do not invent a different timestamp solely to force a row position; document that ties have no deterministic secondary order.

Example using illustrative amounts:

```text
Checkpoint opening              Rp 1,000,000
Real source movement net        Rp  -100,000
Computed saldo before fix       Rp   900,000
CMS BUCKET at investigated time Rp   950,000
Investigated missing Kredit     Rp    50,000
Expected saldo at that boundary Rp   950,000
```

The recorded difference is evidence, not an instruction to automatically generate the balancing amount. A missing Debit, wrong checkpoint, stale CMS capture, source timing issue, and missing Kredit require different diagnoses.

### Effective Window And Checkpoint

- Require a timezone-aware `effective_at`, interpreted against the cluster's local `Asia/Makassar` checkpoint boundary and the investigated CMS calculation cutoff.
- For this phase, the timestamp must be inside the original calculation window: not before the active checkpoint opening, and not after the original cutoff. Respect inclusive timestamp versus exclusive historical-date cutoff semantics.
- Compare the expected checkpoint date **and opening balance/version**, not only its date. A same-date checkpoint update invalidates the investigation context too.
- Refuse corrections whose effect is already folded into a newer checkpoint or whose date would be filtered out. Do not silently backdate, retimestamp, reset, or override a checkpoint.
- The correction workflow never advances a checkpoint. Normal source acceptance remains its owner.
- Once a later checkpoint incorporates an adjustment, count its effect through that opening balance, not again as a movement before the checkpoint.

## Operator Runbook

### 1. Regular Refresh And Finance Ticket

The regular flow retains a numeric `MISMATCH` as `PENDING_REVIEW`, including its staging and reconciliation audit. Finance opens a ticket with cluster, refresh/execution IDs, downloaded range, CMS capture time/value, computed saldo, signed difference, and relevant evidence.

Do not approve the investigated mismatched run. Current generic approval permits finance decisions on a pending mismatch; the correction path must refuse a run already committed. Changing the global acceptance policy is not silently included in this plan.

Technical download/CMS failures do not justify dummy data. Retry or fix those failures first. This first correction workflow requires a completed numeric mismatch with usable source and CMS evidence.

### 2. Operator Investigation

Confirm the source gap using finance records and retained download artifacts. Check date boundaries, opening balance, missing versus duplicate movements, previous approved corrections, and whether DigiPOS has already delivered the missing transaction.

Record a specific missing movement or evidenced net correction in the ticket. Multiple legitimate corrections under one ticket need distinct correction-item IDs. Do not guess sender/receiver or a source transaction reference that is unavailable.

### 3. Manual Kestra Correction

Run the dedicated flow with the ticket/evidence, expected audit state, movement details, and explicit apply confirmation. A dry-run/preview validates and reports but writes nothing.

On application, the flow archives the investigation evidence and atomically records the active adjustment and closes the old pending refresh as described below. No balance checkpoint or real ledger row is changed.

### 4. Next Regular Refresh

After the correction flow reports `APPLIED_AWAITING_REFRESH`, start the normal scoped Top-Up flow manually or let the next configured regular schedule run. It starts from the unchanged checkpoint, downloads again, deduplicates source rows, and calculates over the updated correction set.

If it matches, finance reviews and accepts this **new** refresh through the existing regular process. If it still mismatches, keep it pending, attach the new evidence to the ticket, and investigate again; do not repeatedly insert the difference as additional dummy rows.

## Dedicated Flow Contract

Flow: `finance.finpay.finpay_manual_adjustment_v1`.

Manual UI/API invocation only: no schedule, webhook, event trigger, Pause, or Subflow. Use the existing pinned Python image, Docker network, and database secrets. Recheck the installed Kestra version and relevant schemas through the Kestra MCP during implementation; documentation schemas alone are not runtime validation.

### Inputs

| Input | Contract |
|---|---|
| `cluster_id` | Required configured cluster. |
| `refresh_id` | Required investigated `PENDING_REVIEW` refresh with numeric mismatch. |
| `ticket_reference` | Required external ticket identifier. |
| `correction_item_id` | Required stable identifier for this correction within the ticket. |
| `evidence_reference` | Required durable investigation evidence reference. |
| `effective_at` | Required timezone-aware timestamp inside the investigated calculation window. |
| `transaction_type` | `Kredit` or `Debit`. |
| `amount` | Required decimal string, strictly positive, finite, two decimal places maximum. |
| `currency` | `IDR` only for this phase. |
| `sender`, `receiver`, `source_transaction_reference` | Optional investigated source attributes; never fabricate missing information. |
| `remarks`, `reason` | Required explanation distinguishing synthetic from real source data. |
| `expected_checkpoint_date`, `expected_opening_balance` | Required optimistic checkpoint comparison. |
| `expected_refresh_version` | Required audit version or `updated_at` from the investigation. |
| `apply` | Boolean, defaults to false; true is an explicit authorized operator decision. |

Derive execution identity from Kestra's authenticated execution context where supported. A caller-supplied `operator` string is attribution, not authentication. If the installed edition/shared account cannot prove individual identity, enforce access at the service boundary and document that limitation; do not claim role enforcement from a free-text input.

### Visible Tasks

| Task | Responsibility | Side effects |
|---|---|---|
| `validate_correction_context` | Read the ticket payload, allowlisted scope, numeric mismatch, checkpoint, existing correction, and staging fingerprint. Reject stale or incompatible context. | None. |
| `preview_correction` | Show evidence, signed adjustment, projected original-boundary result, and the exact old-refresh closure that application requires. Projection is not new CMS verification. | None. |
| `archive_correction_evidence` | On apply, preserve the old staging and audit evidence in durable execution-scoped storage with checksums. | Evidence artifact only. |
| `apply_correction` | Revalidate under locks; insert/activate correction and retire the investigated pending run atomically. | Audited financial correction and old staging closure only. |
| `report_correction` | Report `DRY_RUN`, `APPLIED_AWAITING_REFRESH`, or identical-retry result with IDs and next regular-run instructions. | Structured output only. |

Pass dynamic values as serialized input files or environment values, never by interpolating them into Python source. Prefer an artifact reference to a large JSON payload. Log IDs/counts/statuses, not full private rows or credentials. Export durable evidence references and checksums explicitly.

### Atomic Application And Pending Staging

Keeping the old run `PENDING_REVIEW` would block the next overlapping regular refresh. Therefore application must deliberately release that run without accepting its incomplete source data:

1. Acquire the existing refresh-start advisory lock and target refresh row lock, in a documented consistent order shared with relevant writers.
2. Verify the requested database target, operator authorization, original scope/status/version, checkpoint date/value, staging content fingerprint, and other overlapping refreshes/corrections. A row count alone is insufficient.
3. Verify the evidence archive is available and matches the locked staging fingerprint. Failure must leave the adjustment and old refresh untouched.
4. Insert the ticket-item correction and make it `APPROVED` in this transaction. Here `APPROVED` means operator-authorized application backed by a ticket, not a separate finance Pause gate or a confirmed CMS match.
5. Close the old refresh as `REJECTED`, with an explicit closure reason such as `superseded by correction <id>, ticket <reference>`. Purge only that refresh's staging after its archive is verified. Preserve its original mismatch audit, download references, and review history.
6. Commit once. The normal next run receives a new `refresh_id`; real ledger/checkpoint data remains unchanged until its normal acceptance.

Do not chain existing helpers that independently commit. Refactor or add one transaction-owning domain operation with rollback on any failure. A simultaneous approval/rejection/checkpoint change must either serialize safely or make correction application abort as stale.

Initial scope is a single-cluster investigated refresh. Refuse multi-cluster pending runs: rejecting one would discard other clusters' staging. Resolve such a run explicitly through normal finance review and reproduce the target cluster in a scoped regular run before applying this correction. Never silently reject unrelated clusters.

## Data And Retry Contracts

- Reuse the separate adjustment table; do not create another ledger. Add missing ticket, correction-item, evidence checksum/reference, originating refresh, executing identity, and closure linkage fields with a reviewed additive migration.
- Add a unique retry identity such as `(cluster_id, ticket_reference, correction_item_id)` plus a canonical payload digest. Identical retry returns the existing outcome without another adjustment; different payload under the same key fails visibly. The existing hash over descriptive text alone does not satisfy this contract.
- Persist the complete original evidence before purging staging. An artifact failure aborts application. A task failure after the database commit is recovered by lookup of the retry identity, not by compensating automatically or duplicating the adjustment.
- Keep correction status (`APPROVED`/active) distinct from reconciliation outcome (`awaiting next refresh`, `matched`, or `still mismatched`). Record the adjustment-set version/IDs used by subsequent verification. Do not mutate an old verified report to pretend it used newer corrections.
- Existing financial helper APIs, source hashes, classification data, real ledger identifiers, and the requested timestamp-only ordering are preserved unless an explicit migration is documented.

## Late Source Arrival And Undo

Source `row_hash` deduplication does **not** deduplicate a synthetic adjustment against a subsequently delivered real source row. Without special handling, both could affect saldo.

- Store the strongest investigated source identity available, preferably a stable source transaction reference. The next regular run checks newly fetched movements against active corrections before reconciliation.
- If an identity match or plausible ambiguous replacement is found, retain the source evidence, flag the ticket/correction, and block automatic acceptance for operator review. Do not silently delete the real row or auto-void the correction by amount/date similarity.
- Where source identity is unavailable, reliable automatic replacement detection is a limitation. Require ticket follow-up and correction review on subsequent source loads; never promise universal duplicate protection across synthetic and real data.
- If no accepted checkpoint has incorporated the correction, an audited void can exclude it, followed by a fresh regular refresh.
- If a checkpoint has incorporated it, setting `VOID` alone does not unwind that checkpoint. Use an investigated compensating movement within the active window or a separately authorized checkpoint-repair procedure, then a regular verification. Never delete ledger history or claim that voiding automatically reverses every historical balance.
- If the original missing event is before the current checkpoint, refuse this standard flow and route it to that separate historical/checkpoint investigation.

## Pre-Mortem

### Tigers

| Risk | Urgency | Mitigation / owner / deadline |
|---|---|---|
| Blindly balancing a timing or checkpoint error | Launch-blocking | Require investigated ticket/evidence; finance and operator, before each application. |
| Old pending run blocks the next refresh | Launch-blocking | Archive and close only the same scoped run atomically with correction; data engineer, before rollout. |
| Concurrent approval applies stale evidence | Launch-blocking | Shared locks, versions, staging fingerprint, and checkpoint recheck at commit; data engineer, before rollout. |
| Retry inserts the correction twice | Launch-blocking | Ticket-item unique key, canonical payload conflict detection, atomic write; data engineer, before rollout. |
| Real source transaction arrives after synthetic correction | Launch-blocking | Source-identity/conflict review gate and explicit replacement procedure; data engineer and finance, before rollout. |
| Void does not undo an already incorporated checkpoint | Launch-blocking | Track checkpoint/correction lineage and refuse unsafe undo; data engineer, before any void operation. |
| Caller spoofs operator identity | Launch-blocking | Restrict flow and DB mutation access; platform owner, before deployment. |
| Equal timestamps change the displayed match row | Track | Preserve required timestamp-only order and document tie ambiguity; data engineer, in tests/docs. |

### Paper Tigers

- A second automatic re-verification flow is not required: the next regular refresh already owns download, dedupe, calculation, and normal finance acceptance.
- A Telegram correction UI or ticket-system API integration is unnecessary for this phase; ticket references and restricted Kestra inputs suffice.
- Duplicate real-source downloads already have ledger hash protection. That protection is distinct from synthetic correction replacement.

### Elephants

- A manually entered balancing amount does not prove which individual source transaction is missing; preserve that distinction in reason/evidence.
- Archived staging, workbook candidates, and a past CMS snapshot do not prove current source completeness. The next refresh may still mismatch.
- Shared Kestra credentials cannot establish a separate human approver. Any future maker/checker requirement needs actual authenticated identities, not two input strings.

## Implementation And Validation

1. Harden domain correction validation, retry identity, atomic apply/old-run closure, and evidence lineage in `manual_adjustment.py`, `schema.py`, `audit.py`, and `review.py` as needed. Preserve old standalone helpers only where an existing caller requires them.
2. Add the one dedicated manual correction YAML with explicit tasks and no triggers, Pause, Subflow, or Telegram imports.
3. Extend regular verification evidence to identify included adjustments and flag late-source conflicts. Keep normal extraction/staging/calculation/notification separation.
4. Update `finpay_topup_pipeline/README.md` and the root workflow overview with the external ticket runbook, correction authorization, pending closure, next-refresh behavior, and limitations.
5. Run unit and guarded disposable PostgreSQL tests before any deployment. Test the actual Kestra inputs/rendered task data on the installed pinned version, not YAML parsing alone.

Required regression coverage:

- Mismatch -> ticket -> preview -> apply -> old run rejected -> next regular download -> matching calculation -> normal finance acceptance.
- Adjustment remains separate; source staging and ledger contain real source rows only; checkpoint does not move during correction application.
- Dry run writes no proposal, archive, balance, or lifecycle transition.
- Both missing Kredit and missing Debit; nonfinite/negative/zero/overprecision amounts; naive timestamps; incorrect cluster/cutoff/checkpoint.
- Same-day checkpoint update, changed staging content with identical row count, concurrent source approval, and multi-cluster source run all fail safely.
- Repeated task execution after commit, conflicting same ticket-item payload, and concurrent duplicate application produce one correction at most.
- Archive failure and database failure do not partially close a pending run or activate a correction.
- The next regular run may remain mismatched; it is not auto-approved or recursively corrected.
- Late real-source arrival is flagged, and unsafe void after checkpoint incorporation is refused.
- Dynamic values containing quotes, triple quotes, and template-like strings remain data, not executable code.
- No new Telegram calls, automated refresh triggers, or Subflow invocations.

Use a disposable database with a code-level target guard for destructive tests. Production-facing mutations require an explicit target check, backup/evidence procedure, and separate release authorization. Run narrow FinPay checks, the full relevant suites with executed/skipped counts, pinned Kestra validation, Compose validation, and staged/unstaged diff checks. Validate the new flow against the target server before release.

Deploy non-destructively without submitting an execution. A live correction requires an explicitly approved ticket and exact input values; implementation/testing must not invent balancing rows or reset live data.

## Acceptance And Rollback

- The operator can apply an investigated missing-data correction through Kestra without Telegram or an in-flow human approval chain.
- The successful correction run reports `APPLIED_AWAITING_REFRESH`, releases only its investigated pending scope, and does not claim the balance is already verified.
- The next regular refresh includes the active correction and obtains fresh evidence; normal finance acceptance remains separate.
- Retries are idempotent and stale investigations abort without financial changes.
- Adjustment provenance, old staging evidence, and subsequent verification linkage remain auditable.
- No manual checkpoint advancement, fake DigiPOS ledger insertion, or automatic new refresh occurs in the correction flow.
- Roll back unapplied code/flow changes using previous versions. For an applied financial correction, use an audited void/compensation procedure appropriate to checkpoint lineage; do not restore a backup over later valid activity as routine rollback.

This revision changes the plan only. Runtime helpers, database rows, checkpoints, and deployed Kestra flows are not changed by this planning task.
