# LinkAja Taipy Dashboard and Reporting Contract

Status: PostgreSQL data-model checkpoint completed 2026-08-13 for daily LinkAja
withdrawal cycles and all-fee reconciliation. PostgreSQL remains the system of
record; Kestra remains the only ingestion and monthly-publication runtime.
Migration 008 adds the validated reporting views. The attempted Taipy prototype
rendered a blank browser page and is explicitly excluded from this checkpoint;
dashboard system design and implementation remain a separate phase.

## Decisions

1. LinkAja production workflows do not read or write Google Sheets.
2. The daily Kestra workflow accepts everyday uploads and returns affected-day
   results on every successful execution. The dashboard reads the full current
   history from PostgreSQL, so it is not limited to monthly output.
3. The monthly workflow is a separate manual preview/publication process. It
   freezes one `cluster_id + report_month` at an explicit local WITA cutoff.
4. Reversals are linked only by `Original Transaction ID` in the same cluster.
   Original and reversal rows keep their actual posting dates.
5. Daily ledger reporting distinguishes gross posting activity, reversal
   adjustments, and active/net values. It must not relabel the existing active
   daily fee monitor as payable.
6. Monthly payable fee comes only from the two approved source scenarios:
   `Digipos B2B Transfer Fee` and
   `General to Purchase B2B Transfer Agent Telco`.
7. Daily Mandiri reconciliation is required, but it is modeled as settlement
   cycles bounded by actual withdrawal timestamps. The legacy spreadsheet's
   next-available-withdrawal formula and a fixed D-to-D+1 rule are not the
   target contract.
8. LinkAja-side withdrawal evidence and an independently confirmed Mandiri bank
   credit are different facts. The dashboard must not label a settlement as
   bank-confirmed unless an approved bank-side source is available.
9. All fee views distinguish gross, reversed, active/net, missing, and invalid
   evidence. Expected out-cluster fees, posted Digipos fees, in-cluster fees,
   and PPOB fees remain separate measures.
10. The learning-only dbt project does not become a production dependency.

## Target Architecture

```text
LinkAja CSV exports
        |
        v
Kestra daily ingestion --------------------------+
  validate -> normalize -> atomic raw replace    |
        |                                         |
        v                                         |
PostgreSQL raw ledger -> live transaction views  |
        |                                         |
        +-> additive daily fee and Mandiri -------+
        |   reconciliation views/queries           |
        |
        +-> exception and freshness views --------+--> Taipy (read-only)
        |                                               /linkaja
        |
        +-> Kestra monthly preview/publish --------> frozen monthly views
                                                       ^
legacy company web -> authenticated redirect/proxy ---+
```

Deployment contract:

- Host Taipy behind the company reverse proxy at a stable route. The stable
  Taipy 4.1 API uses Flask and does not expose a documented subpath setting, so
  validate the proxy rewrite before choosing `/linkaja` instead of a dedicated
  dashboard host.
- The legacy web redirects users to that route. Prefer the existing company
  identity/session at the proxy boundary.
- Give Taipy a PostgreSQL role with `SELECT` on approved views and no write,
  migration, or raw-replacement permissions.
- Taipy must not duplicate Kestra scheduling, ingestion, or month-close logic.
- Taipy must not ingest a Mandiri statement. An independent bank feed, if
  approved later, is a separate ingestion contract exposed to Taipy through an
  approved read-only relation.
- Kestra JSON/CSV outputs remain execution evidence and downloads, not the
  dashboard's accumulated database.

The dashboard design must use parameterized PostgreSQL queries, connection
pooling, and client-local filter state so one operator's cluster, date,
transaction, or exception selection cannot affect another session. The Taipy
prototype is not accepted as an implementation baseline; select and validate
the presentation runtime separately before pinning a production dependency:

- <https://docs.taipy.io/en/latest/userman/advanced_features/configuration/gui-config/>
- <https://docs.taipy.io/en/latest/userman/scenario_features/data-integration/data-node-config/>
- <https://docs.taipy.io/en/latest/userman/scenario_features/data-integration/data-node-usage/>

Authentication and authorization must be enforced by the legacy application or
reverse proxy unless the selected, pinned Taipy edition is separately proven to
meet the company's identity and authorization requirements:

- <https://docs.taipy.io/en/latest/userman/advanced_features/auth/authentication/>

## Processing and Column Lineage

### Source and normalized raw ledger

The exact source columns are:

```text
No
Top Organization
Parent Organization
Organization
Transaction ID
Original Transaction ID
Partner Reference Number
Invoice ID
Finalized Date
Finalized Time
Initiate Date
Initiate Time
Transaction Type
Transaction Scenario
Transaction Status
Transaction Statement
Account
Counter Party
Debit
Credit
Balance
Fee
```

Normalization maps them to the following raw-table contract and adds cluster,
file, row, and load provenance:

```text
cluster_id
source_file
source_row_number
load_id
source_no
top_organization
parent_organization
organization
transaction_id
original_transaction_id
partner_reference_number
invoice_id
finalized_date
finalized_time
initiate_date
initiate_time
transaction_type
transaction_scenario
transaction_status
transaction_statement
account
counter_party
debit
credit
balance
fee
```

PostgreSQL adds `ingested_at`. The table grain is one source ledger row. Daily
reruns replace all rows for an incoming `(cluster_id, transaction_id)` so a
corrected date cannot leave an older copy behind.

### Live transaction facts

`linkaja_transaction_base_v` groups completed ledger rows into one business
event per `(cluster_id, transaction_id)` and generates:

```text
cluster_id, transaction_id, report_date, finalized_at_local,
original_transaction_id, partner_reference_number, invoice_id,
transaction_type, transaction_scenario, transaction_status, counter_party,
source_ledger_row_count, ledger_debit_total, ledger_credit_total,
source_fee, source_fee_value_count, company_organization, company_account,
company_debit, company_credit, company_balance, source_files,
updated_load_id, updated_at
```

`linkaja_transactions_current_v` adds the current calculation and reversal
columns:

```text
signed_amount, has_company_purchase_account, balance_before, transfer_scope,
is_reversal, original_transaction_scenario, original_is_resolved,
reversal_category, reversal_count, is_reversed,
reversal_transaction_ids, first_reversal_at_local,
latest_reversal_at_local, reversal_resolution_status, is_unusual,
unusual_reason_codes
```

The measures have different meanings and must remain separate:

- `ledger_debit_total` and `ledger_credit_total` include every completed source
  ledger row in the transaction.
- `signed_amount = ledger_debit_total - ledger_credit_total`.
- `company_debit`, `company_credit`, and `company_balance` use only the top
  organization's Purchase Account row.
- `source_fee` is the single distinct source Fee at transaction grain. Blank
  and verified zero are different states.
- `Balance` is after-posting evidence for one account; it is never summed.

### Daily Kestra result

Every non-dry daily upload produces `linkaja_database_result.json` for all
affected dates. If an upload adds or changes an original/reversal relationship,
the affected scope includes the linked transaction dates.

`fee_rows` columns:

```text
REPORT DATE
CLUSTER ID
EXPECTED FEE
DIGIPOS B2B TRANSFER IN CLUSTER FEE
TOTAL FEE
SOURCE ROWS
SOURCE FILE
```

`detail_rows` columns:

```text
REPORT DATE
TRANSACTION SCENARIO
COMPANY DEBIT
COMPANY CREDIT
SOURCE FEE
SOURCE FEE MISSING COUNT
EXPECTED FEE
IN CLUSTER FEE
REVERSAL IN CLUSTER COUNT
REVERSAL IN CLUSTER FEE
UNRESOLVED REVERSAL COUNT
```

These are execution-scoped operational results. `EXPECTED FEE`, `IN CLUSTER
FEE`, and `TOTAL FEE` are active monitoring values: directly reversed
originals contribute zero, while the reversal event stays on its own date in
the detail evidence. They are not a Mandiri settlement or monthly payable
result. Gross and reversed fee values require the planned canonical daily fee
surface.

### Frozen monthly transaction facts

`linkaja_transactions` copies the transaction fact and reversal state at the
chosen cutoff, then adds:

```text
report_month
calculation_cutoff
materialization_id
materialized_at
```

Its primary key is `(cluster_id, transaction_id)`. Publication atomically
replaces only the requested `(cluster_id, report_month)` and records an audit
row in `linkaja_monthly_refreshes`:

```text
materialization_id, cluster_id, report_month, calculation_cutoff,
transaction_rows, unresolved_reversal_count, refreshed_at
```

### Monthly artifacts and published summary

`linkaja_daily_raw_calculation.csv` columns:

```text
REPORT DATE
TRANSACTION SCENARIO
TRANSACTION COUNT
SOURCE LEDGER ROWS
LEDGER DEBIT
LEDGER CREDIT
SIGNED AMOUNT
COMPANY DEBIT
COMPANY CREDIT
SOURCE FEE
PPOB SOURCE FEE MISSING COUNT
REVERSED ORIGINAL TRANSACTION COUNT
COMPLETE REVERSAL COUNT
UNRESOLVED REVERSAL COUNT
```

`linkaja_unresolved_reversals.csv` columns:

```text
REPORT DATE
FINALIZED AT LOCAL
REVERSAL TRANSACTION ID
ORIGINAL TRANSACTION ID
REVERSAL SCENARIO
SOURCE LEDGER ROWS
LEDGER DEBIT
LEDGER CREDIT
SIGNED AMOUNT
COMPANY DEBIT
COMPANY CREDIT
SOURCE FEE
COMPANY BALANCE
BALANCE BEFORE
SOURCE FILES
UPDATED LOAD ID
UNUSUAL REASON CODE
```

`linkaja_monthly_fee_summary_v` columns:

```text
report_month
calculation_cutoff
materialization_id
cluster_id
fee_category
gross_transaction_count
reversed_transaction_count
active_transaction_count
gross_fee
reversed_fee
net_fee
monthly_payable_fee
gross_missing_fee_count
reversed_missing_fee_count
active_missing_fee_count
calculation_status
```

## Fee Contracts

### Daily active monitoring

```text
active expected out-cluster fee
  = count(active original Digipos B2B Transfer
          with company_credit > 0) * Rp200

active in-cluster fee
  = count(active original Digipos B2B Transfer In Cluster) * Rp20

active daily monitoring total
  = active expected out-cluster fee + active in-cluster fee
```

For the dashboard, the approved daily presentation is:

```text
gross fee       = fee attached to originals on their actual posting dates
reversed fee    = fee of originals that now have a completed linked reversal
active/net fee  = gross fee - reversed fee
reversal event  = shown separately on the reversal's actual posting date
unresolved      = shown separately; never guessed from amount or date
```

Migration 008 now supplies a canonical gross/reversed/active row for all four
fee types without altering raw facts or posting dates. Existing daily Kestra
artifact fields remain unchanged for compatibility.

### Monthly Digipos B2B transfer fee

Target scenario: `Digipos B2B Transfer Fee`.

```text
eligible original = source_fee is exactly Rp200
gross fee         = eligible original count * Rp200
reversed fee      = eligible reversed-original count * Rp200
active/payable    = eligible active-original count * Rp200
```

Company debit and credit are reconciliation evidence only. They do not decide
eligibility or payable value.

### Monthly General-to-Purchase B2B Transfer Agent Telco fee

Target scenario: `General to Purchase B2B Transfer Agent Telco`.

```text
gross fee      = sum(source_fee) over all original targets
reversed fee   = sum(source_fee) over reversed originals
active/payable = sum(source_fee) over active originals
```

If any active target has a missing Fee, `net_fee` and
`monthly_payable_fee` are null and status is `INCOMPLETE_MISSING_FEE`. Missing
must never be silently treated as zero.

## Reversal State Contract

For an original posted on day D and a linked reversal posted on day R:

1. The original remains on D.
2. The reversal remains on R.
3. The current original has `is_reversed = true` and lists the reversal ID.
4. The reversal has `reversal_resolution_status = COMPLETE` only if its
   original resolves in the same cluster.
5. An unresolved reversal is unusual and remains in the exception queue.
6. A monthly snapshot marks the original inactive only when the completed
   linked reversal is before that snapshot's exclusive cutoff.
7. Late data changes live views immediately but never silently changes a
   published monthly snapshot; preview and republish the affected month.

Multiple linked reversal IDs are preserved through `reversal_count` and
`reversal_transaction_ids`. The dashboard must show this evidence even though
the current unusual-state rule only flags unresolved reversals.

## Required Daily Reconciliation Contract

The dashboard must expose three related grains without merging them into one
ambiguous daily total:

| Grain | Primary key | Purpose |
|---|---|---|
| Daily fee | `cluster_id + report_date + fee_type` | Compare gross, reversal adjustment, active/net, source evidence, and completeness for every approved fee type. |
| Mandiri settlement cycle | `cluster_id + company_account + withdrawal_transaction_id` | Reconcile actual Purchase Account movement between two observed withdrawal events. |
| Exception | `cluster_id + transaction_id + exception_code` | Explain why a date, fee, reversal, balance, load, or settlement cannot be approved. |

All timestamps are source-local WITA. Date aggregation is presentation only;
settlement assignment uses `finalized_at_local`, not a calendar assumption.

### Mandiri settlement-cycle rules

The LinkAja-side withdrawal fact is a completed original transaction whose
scenario is:

```text
Organization Withdraw of Funds with Next Working Day Payment
```

Build one cycle for each withdrawal transaction and company Purchase Account:

```text
cycle start = previous observed withdrawal finalized_at_local, exclusive
cycle end   = current withdrawal finalized_at_local, inclusive
```

The first observed withdrawal has no proven prior boundary and is marked
`PARTIAL_OPENING_HISTORY`. Activity after the latest observed withdrawal stays
in an open `PENDING_WITHDRAWAL` cycle. Multiple withdrawals at the same local
timestamp, a withdrawal without a Purchase Account row, or more than one
Purchase Account on one transaction are exceptions, not silently ordered
events.

Mandiri ledger reconciliation uses actual posting movement. It retains the
original on its original timestamp and the reversal on its reversal timestamp.
It must not remove an original's company movement merely because that original
is now `is_reversed`; doing that and also including the reversal would apply the
reversal twice.

Each closed cycle should expose at least:

```text
cluster_id
company_account
settlement_cycle_id
previous_withdrawal_transaction_id
withdrawal_transaction_id
interval_start_exclusive
interval_end_inclusive
opening_balance
non_withdrawal_company_credit
non_withdrawal_company_debit
withdrawal_company_debit
closing_balance
calculated_closing_balance
balance_variance
withdrawal_transaction_count
source_transaction_count
source_ledger_row_count
resolved_reversal_count
unresolved_reversal_count
source_fee_missing_count
source_files
latest_load_id
linkaja_reconciliation_status
mandiri_confirmation_status
```

The objective account-control equation is:

```text
calculated closing balance
  = opening balance
  + all company Purchase Account credits in the cycle
  - all company Purchase Account debits in the cycle

balance variance
  = reported closing balance - calculated closing balance
```

`withdrawal_company_debit` is the LinkAja withdrawal amount. It is not, by
itself, proof of the independent amount credited by Mandiri. Until an approved
bank-side feed exists, `mandiri_confirmation_status` remains
`PENDING_BANK_EVIDENCE`. If that feed is later approved, expose its statement
identity, posting timestamp, credited amount, match method, and:

```text
bank variance = Mandiri credited amount - LinkAja withdrawal company debit
```

Do not restore the legacy spreadsheet `TOTAL EXPECTED MANDIRI` formula without
a business-approved component mapping and tolerance. The recommended first
release shows the complete interval movement and account-control variance,
then labels any business expected-settlement result `NOT_EVALUATED` until that
mapping is approved.

### All-fee daily reconciliation

The fee surface includes four fee types with deliberately different sources:

| Fee type | Gross population or amount | Reversed adjustment | Active/net value | Completeness rule |
|---|---|---|---|---|
| Expected out-cluster Rp200 | Original `Digipos B2B Transfer` with positive company credit, count × Rp200 | Same eligible originals with `is_reversed` | Eligible originals not reversed, count × Rp200 | Complete when transaction/reversal state is valid; this is derived monitoring, not a posted or payable fee. |
| In-cluster Rp20 | Original `Digipos B2B Transfer In Cluster`, count × Rp20 | Same originals with `is_reversed` | Eligible originals not reversed, count × Rp20 | Complete when transaction/reversal state is valid; preserve source Fee separately as evidence. |
| Posted Digipos fee | Original `Digipos B2B Transfer Fee` with transaction-grain `source_fee = 200`, count × Rp200 | Same eligible originals with `is_reversed` | Eligible originals not reversed, count × Rp200 | Count missing/non-Rp200 source Fee as exceptions; do not infer eligibility from company credit. |
| PPOB/agent-telco fee | Sum source Fee for original `General to Purchase B2B Transfer Agent Telco` | Sum source Fee for reversed originals | Sum source Fee for active originals | Any missing active Fee makes active/net null and status `INCOMPLETE_MISSING_FEE`; verified zero remains zero. |

For every `cluster_id + report_date + fee_type`, expose:

```text
gross_transaction_count
reversed_transaction_count
active_transaction_count
gross_fee
reversed_fee
active_fee
missing_fee_count
active_missing_fee_count
invalid_fee_count
resolved_reversal_event_count
unresolved_reversal_count
calculation_status
source_files
latest_load_id
```

The same measures should also aggregate by Mandiri settlement cycle. Daily
variance is useful for investigation but may reflect posting timing; a
settlement decision must use the actual cycle interval. In particular, the
derived expected out-cluster Rp200 and the posted Digipos fee are shown side by
side and may have a monitoring difference, but they must not be relabeled as
the same business measure.

### Status and exception vocabulary

Use stable machine codes and separate status dimensions:

- data: `COMPLETE`, `INCOMPLETE_MISSING_FEE`, `STALE_SOURCE`,
  `PARTIAL_OPENING_HISTORY`;
- reversal: `CLEAR`, `UNRESOLVED_REVERSAL`, `MULTIPLE_REVERSALS`,
  `INVALID_REVERSAL_CHRONOLOGY`;
- LinkAja settlement: `BALANCED`, `BALANCE_MISMATCH`, `PENDING_WITHDRAWAL`,
  `NOT_EVALUATED`;
- Mandiri confirmation: `PENDING_BANK_EVIDENCE`, `MATCHED`, `OVER_CREDITED`,
  `UNDER_CREDITED`, `AMBIGUOUS_MATCH`.

One status must not hide another. For example, a cycle can be account-balanced
and still have an unresolved reversal or pending bank evidence.

## Implemented PostgreSQL Reporting Surfaces

Migration 008 adds these reporting surfaces without editing migrations 001
through 007:

1. `linkaja_daily_fee_reconciliation_v` — stable rows for all four fee types,
   including zero-activity dates/categories when a requested date spine is
   applied by the repository.
2. `linkaja_withdrawal_events_v` — one completed LinkAja withdrawal event with
   account, timestamp, amount, balance, and provenance.
3. `linkaja_mandiri_settlement_cycles_v` — interval boundaries, actual account
   movement, balance control, exceptions, and LinkAja-side status.
4. `linkaja_reconciliation_exceptions_v` — normalized exception codes with
   drill-through identity and source provenance.
5. `linkaja_load_freshness_v` — successful PostgreSQL load, cluster, source
   file, row count, posting range, load ID, and completion timestamp. Failed or
   started Kestra executions remain a follow-up orchestration-status source.

Views calculate from `linkaja_transactions_current_v` and raw provenance. The
daily fee view uses active/reversal state; the Mandiri account-control view uses
gross movements on actual timestamps. That distinction requires database
fixtures so a late reversal cannot be applied twice.

The cycle view assigns chronologically ordered Purchase Account transactions to
one numbered settlement cycle in a single pass. Each withdrawal closes its
current cycle; transactions after the latest withdrawal remain in the one open
cycle for that account. This avoids reusing one movement across overlapping
withdrawal windows while preserving the public view columns.

An independent Mandiri statement relation is not part of this LinkAja
presentation change. Define it only through a separately approved ingestion
migration with ownership, retention, matching keys, access controls, and replay
behavior.

## Planned Dashboard Pages

The future presentation system should expose these six views:

1. **Overview** — latest successful load by cluster, source freshness, open and
   closed Mandiri cycles, balance mismatches, unresolved reversals, incomplete
   fees, pending bank confirmations, and latest monthly publication.
2. **Fees** — four fee categories with gross, reversed, active/net,
   missing/invalid, status, and a daily trend.
3. **Mandiri cycles** — actual withdrawal-timestamp intervals, movement bridge,
   opening/calculated/reported closing balances, LinkAja variance, independent
   bank confirmation status, reversal status, and source provenance.
4. **Exceptions** — exact transaction ID, severity, exception code, detail,
   and source file.
5. **Transactions** — current transaction facts with scenario, account,
   movement, Fee, reversal state, resolution, and source provenance.
6. **Month close** — frozen monthly fee rows and publication cutoff; no write
   or materialization action.

The Overview daily table provides one row per cluster/date with withdrawal
amount, expected out-cluster Rp200, in-cluster Rp20, posted Digipos fee, PPOB
fee, exception count, and status text. Gross/reversed/active and missing/invalid
fee measures are available on the Fees route.

Planned global filters are cluster, WITA posting-date range, and fee type.

## Local Full-Bundle Integration Result (2026-08-13)

The July 1 through August 9 source bundle was replayed locally through the
unchanged Kestra workflow into a disposable PostgreSQL database, one file at a
time. All 43 workflow executions succeeded. After every upload, the harness
checked migration version, execution/load identity, monotonic raw growth, live
transaction facts, fee rows, withdrawals, settlement cycles, exceptions, load
freshness, and the untouched frozen-month relations. All 43 completed
post-upload data checks had zero invariant errors.

The final database state matches the independent July replay baseline:

| Measure | Result |
|---|---:|
| Source files / successful load IDs | 43 / 43 |
| Raw ledger rows | 1,560,180 |
| Base/current transaction facts | 999,898 / 999,898 |
| Reversal events | 2,425 |
| Resolved / unresolved reversals | 1,837 / 588 |
| Daily fee rows | 960 |
| Withdrawal events / closed cycles | 228 / 228 |
| Open account cycles | 6 |
| Duplicate cycle IDs | 0 |
| Frozen monthly facts / refresh rows | 0 / 0 |

Every account transaction was assigned once, every withdrawal had one closed
cycle, every Purchase Account had one open cycle, and every cluster/date had
four fee-category rows. The exception surface intentionally exposes 588
unresolved reversals, 27 invalid or missing posted-Digipos fee rows, and one
active missing PPOB Fee. These are finance/data-quality findings, not pipeline
failures.

One backend prototype query for all six clusters and July 1 through August 9
returned 240 daily rows, 960 fee rows, 234 cycles, 616 exceptions, 1,000 bounded
transaction rows, and six freshness rows. It completed in 175.61 seconds, and
the Taipy browser rendered a blank page. This does not constitute dashboard
acceptance. Treat presentation-system design plus read-model materialization or
equivalent refresh optimization as separate open work.

Resolved integration-harness issues were a read permission on the disposable
PostgreSQL init script, Kestra Basic Auth credential validation, and Docker
socket permission on one resumed read-only audit. The already committed upload
was not replayed. A SELECT-only dashboard role was verified to allow reads and
reject insert, update, and delete privileges.

The first presentation release must be read-only. Kestra execution or monthly
publication buttons require separate authorization, audit, and idempotency
design. Keep its dependency manifest and image separate from
`linkaja-fee-pipeline:3.11` so the accepted data model does not depend on one UI
framework.

## Schema Suitability Assessment

The current schema is suitable for LinkAja's core data nature:

- it preserves ledger-row grain before transaction aggregation;
- it does not discard unknown transaction scenarios;
- it isolates company Purchase Account movement from the opposite ledger side;
- it keeps source Fee nullable and detects conflicting transaction-level fees;
- it resolves reversals through explicit IDs and preserves actual timestamps;
- it supports late originals, unresolved reversals, daily idempotent replacement,
  and frozen cluster-month snapshots with publication audit;
- it stores source file, row, load, and update provenance.

The reconciliation model is implemented but no dashboard is accepted. Complete
the remaining items before using any presentation system as an approval or
settlement surface:

### Required before dashboard release

The 2026-08-12 operational correction pass completed blank Balance handling,
active daily reversal treatment, active-missing PPOB status, and stable monthly
fee-category rows. Migration 007 carries the published-view changes. The
remaining dashboard-release work is:

1. Migration 008 now provides all four fee categories, withdrawal events,
   actual-timestamp settlement cycles, exceptions, and raw-load freshness.
   Keep the existing Kestra artifact keys for backward compatibility.
2. Extend load freshness with failed/started Kestra execution state; the first
   implementation can prove successful PostgreSQL loads only.
3. Create and test a read-only dashboard database role and approved-view grants.
4. Approve the settlement component mapping, tolerance, bank-evidence source,
   and exception ownership before showing `MATCHED`, `OVER_CREDITED`, or
   `UNDER_CREDITED` as operational conclusions.

All database changes must be additive numbered migrations. Do not edit already
applied migrations 001 through 007.

### Follow-up hardening

- Add unusual-state rules for multiple reversals, reversal chains/cycles, and
  any source pattern confirmed to be invalid by operations.
- Validate empirically that transaction IDs never collide inside one cluster.
- Label all `_local` timestamps as WITA in the UI and convert only at display
  boundaries.
- Measure query volume before adding materialized summaries or indexes.
- Rename shared `FINPAY_DB_*` and Docker network configuration only as a
  coordinated infrastructure change; it is coupling, not a calculation error.

## Reviewable Delivery Plan

### Pass 0: approve reconciliation semantics (finance approval pending)

Current behavior:

- Before migration 008, daily PostgreSQL reports exposed active
  expected/in-cluster fees and scenario movement without a canonical all-fee
  row or Mandiri interval.
- The inactive Sheets compatibility module contains a next-withdrawal formula;
  it is not a production or dashboard contract.

Structural improvement:

- Approve the actual-withdrawal interval grain, business expected-settlement
  component mapping, amount tolerance, bank-side evidence source, WITA cutoff,
  and exception ownership.
- Freeze the July source fingerprint and representative expected outputs.

Validation:

- Finance and operations sign a worked example for normal, weekend-spanning,
  reversal, missing-fee, no-withdrawal, and multiple-withdrawal cases.
- Every UI status has one written formula and evidence source.

### Pass 1: add database characterization tests (initial coverage complete)

Current behavior:

- Existing tests now cover reversal-aware daily fees, monthly fee categories,
  actual withdrawal intervals, account-control balances, and empty dashboard
  date spines.

Structural improvement:

- Add guarded disposable-PostgreSQL fixtures before adding new relations.
- Cover first/open cycles, multiple withdrawals, actual-timestamp boundaries,
  multiple company accounts, late reversals, unresolved reversals, blank
  Balance, active missing PPOB Fee, and corrected reruns.

Validation:

- All guarded tests execute with zero skips against a database whose exact name
  matches `LINKAJA_TEST_DATABASE_GUARD`.
- Current relation and Kestra artifact contract tests remain unchanged and pass.

### Pass 2: add reporting relations (migration 008 complete)

Current behavior:

- Before migration 008, Taipy would have needed to assemble business formulas
  from several operational queries.

Structural improvement:

- Add the approved fee, withdrawal-cycle, exception, and freshness surfaces in
  one or more additive numbered migrations.
- Centralize financial calculations in PostgreSQL; Taipy only filters,
  formats, and navigates the returned facts.

Validation:

- SQL grain, nullability, zero-category stability, status vocabulary, and
  read-only grants have schema-contract tests.
- Reversal movement is counted once in Mandiri account control and once as a
  fee adjustment, never twice in the same measure.
- Query plans are recorded for six clusters over a full month before adding
  indexes or materialization.

### Pass 3: choose and build the read-only presentation system (pending)

Current behavior:

- The PostgreSQL views are accepted as the data boundary. The attempted Taipy
  shell rendered a blank browser page and is not part of this checkpoint.

Structural improvement:

- Select the presentation architecture independently of the LinkAja ingestion
  and reporting model.
- Implement validated configuration, pooled parameterized queries, Decimal and
  WITA serialization, client-local filters, error handling, pagination, and
  explicit refresh.

Validation:

- Repository tests reject SQL mutation and unbounded date ranges.
- Browser tests prove rendering, filter isolation, empty states, null-vs-zero
  formatting, and database-error behavior.
- The dashboard role cannot insert, update, delete, create, or run migrations.

### Pass 4: implement daily Mandiri and fee pages (pending)

Current behavior:

- Operators can query the validated PostgreSQL views, but no browser dashboard
  has been accepted.

Structural improvement:

- Build Overview, Daily reconciliation, Mandiri cycles, Fee reconciliation,
  Daily ledger, Exceptions, Monthly close, and Transaction detail pages.
- Provide drill-through from every amount and status to transaction/source
  evidence. Keep monthly payable visually distinct from daily monitoring.

Validation:

- Golden view-model tests cover all four fee types and each status dimension.
- One selected daily/cycle total can be reproduced from transaction rows with
  exact Decimal arithmetic.
- Exported rows retain filters, WITA labels, freshness, and exception codes.

### Pass 5: security and deployment integration

Current behavior:

- The legacy application and on-premises reverse proxy do not yet route to a
  Taipy service.

Structural improvement:

- Deploy the dashboard-only image behind the authenticated proxy at `/linkaja`.
- Pass only approved identity context, enforce TLS and timeouts, redact secrets,
  and expose health/readiness without database credentials.

Validation:

- Validate the pinned Taipy GUI/Flask configuration against the on-premises
  runtime and subpath.
- Test unauthorized access, two simultaneous users with isolated filters,
  database read-only enforcement, restart behavior, and a stale-data warning.

### Pass 6: July data-model acceptance complete; UI acceptance pending

Current behavior:

- The full July-through-August-9 replay validates relation counts, transaction
  and fee semantics, and cycle assignment. No browser UI has been accepted by
  operations.

Structural improvement:

- Replay all 43 July-through-August-9 exports into a disposable PostgreSQL
  database and review every cluster through the August 10 exclusive cutoff.
- Compare Taipy, reporting relations, current Python outputs, and source
  transactions. If a Mandiri bank feed is approved, compare it independently.

Validation:

- Fee totals match the accepted July baseline or have transaction-level
  explanations.
- Every withdrawal belongs to exactly one cycle; every non-withdrawal movement
  belongs to at most one closed cycle or one open cycle.
- Balance mismatches, 588 unresolved reversals, the active missing PPOB Fee,
  first/open cycles, and any missing bank evidence remain visible and cannot be
  filtered into a false `MATCHED` state.
- Idempotent rerun and late-reversal tests update affected live dates/cycles
  without altering a frozen monthly snapshot.

### Pass 7: controlled release

Release the dashboard first as an analytical read-only surface. Promote it to
an operational reconciliation or approval surface only after Pass 6 has no
unexplained financial differences and the business owners approve the status
and exception policies. Rollback disables the dashboard route and reporting
views/grants without changing raw data, Kestra workflows, or monthly snapshots.

## Decisions Required Before Implementation

1. Is the independently confirmed Mandiri amount available from a bank
   statement/API, or is the LinkAja withdrawal event the only current source?
2. Is one settlement cycle per company account and actual withdrawal event the
   approved grain?
3. Which principal and fee components form the business expected Mandiri
   amount, and is a post-withdrawal reserve expected?
4. What amount tolerance produces `MATCHED`, `OVER_CREDITED`, and
   `UNDER_CREDITED`?
5. Should daily fee views use source posting date only, while operational
   settlement conclusions use the withdrawal interval as recommended?
6. Who owns unresolved reversals, missing PPOB Fee, balance mismatch, stale
   source, and ambiguous bank-match exceptions?
7. Which roles may view transaction identifiers, source filenames, balances,
   and downloads, and how long are exports retained?

Recommended defaults are actual withdrawal-timestamp cycles, exact Decimal
comparison with zero tolerance until Finance approves another value, posting-
date daily fee views plus cycle aggregates, `PENDING_BANK_EVIDENCE` without an
independent source, and no operational approval while any unresolved reversal,
active missing Fee, stale load, or balance mismatch affects the cycle.
