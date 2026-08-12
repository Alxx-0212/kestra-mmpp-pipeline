# LinkAja Workflow and Calculation Guide

This document is the user-facing operational reference for the LinkAja
pipeline. It explains how the current daily and monthly workflows load CSV
exports, build PostgreSQL facts, calculate fees, handle reversals, expose
database-backed report results, and freeze a monthly close.

The approved read-only Taipy architecture, complete column lineage, report
contracts, schema suitability assessment, and delivery sequence are in
[`TAIPY_DASHBOARD_PLAN.md`](TAIPY_DASHBOARD_PLAN.md).

The source export is an account ledger, not one row per business transaction.
One transaction can have one or more ledger rows because LinkAja reports the
movement of each affected account. All calculations therefore start by
preserving ledger rows and then aggregating to transaction grain.

Do not expect a simple grouping by `Finalized Date` and `Transaction Scenario`
to reproduce the next withdrawal amount. Withdrawals follow their own posting
time and may span weekends, adjustments, late postings, and reversals.

## Workflow Inventory

| Workflow | Kestra ID | Purpose |
|---|---|---|
| `linkaja_fee_pipeline.yml` | `linkaja_fee_pipeline_v1` | Daily CSV ingestion, live database calculation, and downloadable Kestra result artifact. |
| `linkaja_monthly_materialization.yml` | `linkaja_monthly_materialization_v1` | Manual preview/publication of one frozen cluster-month snapshot plus downloadable daily raw and unresolved-reversal analysis. |

Both workflows run the `linkaja-fee-pipeline:3.11` image and import their
public functions through `linkaja_pipeline.py`.

The `dbt/` directory is an isolated learning project over the existing
PostgreSQL relations. It currently builds only a staging view and tests; it is
not called by either Kestra workflow. Follow `dbt/README.md` without changing
the production workflow until dbt output parity is established.

## Daily Workflow

### Inputs and side effects

The daily workflow accepts one CSV export for one cluster.

| Input | Meaning |
|---|---|
| `source_file` | Required LinkAja CSV. The filename normally identifies the cluster, for example `laporan-411311-...csv`. |
| `dry_run` | `true` validates and normalizes only. It does not run migrations or write PostgreSQL rows. |

For `dry_run=false`, the workflow does the following in order:

```text
source CSV
-> validate_and_normalize_linkaja_csv
-> normalized_linkaja.csv + linkaja_manifest.json
-> persist_and_summarize_linkaja
   -> apply pending schema migrations
   -> replace affected raw transaction IDs atomically
   -> calculate live daily views and daily summary rows
-> linkaja_database_result.json
```

The database task uses exponential retries. A dry run performs no database
write. The production workflow has no Google Sheets task or GCP credentials;
PostgreSQL and Kestra artifacts are the reporting boundary for dashboard and
API consumers.

`sheets.py` remains temporarily as an inactive, direct-import compatibility
module for historical renderer tests and any external migration work. It is no
longer exported by `linkaja_pipeline`, and the LinkAja runtime image no longer
installs Google client libraries. Neither production LinkAja workflow can call
that module through its supported public API. Delete the compatibility file
only as a separate cleanup after confirming that no external scripts import it
directly.

### CSV validation and normalization

The CSV must have the exact LinkAja export header and column order:

```text
No, Top Organization, Parent Organization, Organization, Transaction ID,
Original Transaction ID, Partner Reference Number, Invoice ID, Finalized Date,
Finalized Time, Initiate Date, Initiate Time, Transaction Type,
Transaction Scenario, Transaction Status, Transaction Statement, Account,
Counter Party, Debit, Credit, Balance, Fee
```

The normalizer enforces these rules:

- The input must be a CSV and `load_id` must be present.
- The cluster ID is read from the filename when possible and checked against
  the numeric prefix of `Top Organization` on every row.
- `Transaction ID`, `Transaction Scenario`, `Finalized Date`, and
  `Finalized Time` are required.
- Dates are parsed as `DD/MM/YYYY`; source timestamps are local business time
  in `Asia/Makassar` (WITA), with no automatic timezone conversion.
- Debit, credit, balance, and fee are normalized as decimal values. Blank
  optional values remain null where the schema permits it.
- A manifest records cluster ID, source file, row count, posting dates,
  scenario counts, and the Kestra execution ID used as `load_id`.

The normalized artifact has database column names and is the only file copied
into PostgreSQL. The original CSV is never modified.

## Data Grain and Account Rules

Use both levels of the source deliberately:

| Level | Meaning |
|---|---|
| Ledger-row level | What changed in one named account. |
| Transaction level | One completed business event grouped by `cluster_id + transaction_id`. |

The company-side purchase account is identified only when both conditions are
true:

1. `Organization = Top Organization`.
2. `Account` contains `Organization MFS Purchase Account`.

Do not hard-code an account number. Account numbers can vary between clusters.
Do not combine the company Purchase Account with a retailer or agent General
Account: they may be the two sides of the same internal transfer.

At transaction grain, the ledger signed amount is:

```text
signed_amount = ledger_debit_total - ledger_credit_total
```

This is an all-ledger measure, not a company Purchase Account measure. A
balanced two-sided internal transfer has `signed_amount = 0`. Company balance
reconciliation continues to use the account-side credit-minus-debit rule below.

For a chronological balance check on one account:

```text
closing balance = opening balance + sum(credit) - sum(debit)
```

`Balance` is an after-posting balance for one account. It is evidence for a
balance reconciliation, not an amount to sum.

## PostgreSQL Relations

| Relation | Type | Grain and responsibility |
|---|---|---|
| `linkaja_schema_migrations` | Table | Applied schema version, migration name, checksum, and timestamp. |
| `linkaja_raw_transactions` | Table | One normalized source ledger row. It retains both sides of internal transfers. |
| `linkaja_transaction_base_v` | View | One completed transaction fact before current reversal enrichment. |
| `linkaja_transactions_current_v` | View | Current transaction fact with reversal, transfer-scope, and unusual-state columns. Daily SQL reads this view. |
| `linkaja_transactions` | Table | Physical, immutable-in-practice monthly snapshot for a cluster and report month at a selected cutoff. Daily ingestion does not write it. |
| `linkaja_monthly_refreshes` | Table | Audit row for a successful monthly publication. |
| `linkaja_monthly_fee_summary_v` | View | Monthly Digipos and PPOB fee release summary calculated from the published snapshot. |

### Raw ledger table

`linkaja_raw_transactions` contains source identity and provenance
(`source_file`, `source_row_number`, `load_id`, `ingested_at`), the original
organization/account fields, transaction references, source timestamps,
scenario/status fields, and the `debit`, `credit`, `balance`, and `fee`
values.

Its physical primary key is `load_id + source_row_number`. Its business
replacement identity is `cluster_id + transaction_id`:

- Before inserting an uploaded transaction ID, existing raw rows for that
  cluster and transaction ID are deleted.
- Every staged ledger row for the uploaded transaction is then inserted.
- Corrected files can therefore replace an old posting date. Both the old and
  new dates become affected daily report dates.

### Transaction fact columns

The base and current transaction facts retain these important groups of data:

| Group | Columns and meaning |
|---|---|
| Identity and time | `cluster_id`, `transaction_id`, `report_date`, `finalized_at_local`, references, scenario, and status. |
| Source aggregation | `source_ledger_row_count`, `ledger_debit_total`, `ledger_credit_total`, `source_fee`, `source_fee_value_count`, and `source_files`; the current view and monthly snapshot also expose ledger-level `signed_amount`. |
| Company movement | Purchase-account organization/account, `company_debit`, `company_credit`, `company_balance`, and `balance_before`. |
| Transfer and reversals | `transfer_scope`, `is_reversal`, original scenario/resolution, `reversal_count`, reversal IDs, and first/latest reversal timestamps. |
| Exception state | `reversal_resolution_status`, `is_unusual`, and `unusual_reason_codes`. |
| Snapshot provenance | On `linkaja_transactions` only: `report_month`, `calculation_cutoff`, `materialization_id`, and `materialized_at`. |

The physical monthly table has generated or constrained fields for purchase
account presence, signed amount, balance before, reversal status, unusual
state, month validity, and cutoff validity. It accepts only completed
transaction facts.

### Live transaction views

`linkaja_transaction_base_v` filters raw rows to `Completed` and groups a
transaction's ledger rows. It preserves the source row count and sums all
ledger debit/credit values while separately summing only the company Purchase
Account movement.

`linkaja_transactions_current_v` adds current reversal enrichment from all
available completed raw data in the same cluster:

- `NOT_REVERSAL`: the transaction has no `Original Transaction ID`.
- `COMPLETE`: the transaction refers to exactly one completed original
  scenario.
- `UNRESOLVED`: no completed original is available. The transaction is unusual
  and receives `{UNRESOLVED_REVERSAL}`.

For an original transaction, `is_reversed` becomes true when one or more
completed reversal transaction IDs reference it. The original remains on its
own posting date; the reversal remains on its actual later posting date.

## Daily Persistence and Rerun Behavior

The persistence task uses one database transaction and an advisory lock for
the target cluster.

```text
create temporary raw stage
-> COPY normalized rows
-> validate one coherent set of attributes per transaction ID
-> build affected transaction IDs and reversal-linked IDs
-> capture old affected dates
-> replace raw transaction IDs
-> expand reversal-linked IDs again
-> capture new affected dates
-> query current views for only the affected dates
-> commit
```

The stage rejects a transaction ID whose source rows disagree on posting date,
scenario, status, transaction type, original transaction ID, non-null fee
value, or company Purchase Account. This prevents silently aggregating an
ambiguous business event.

Daily reruns are idempotent at transaction identity. They replace the uploaded
raw transaction groups and return recalculated results for only the affected
report dates. The live views immediately expose the complete current state.
Daily ingestion does not alter an already published monthly snapshot.

## Daily Fee Calculations

The active daily fee report is a monitoring calculation from
`linkaja_transactions_current_v`. It has two deliberately different parts:

```text
expected recharge / out-cluster fee
    = count(Digipos B2B Transfer facts with positive company credit) * 200

in-cluster fee
    = count(Digipos B2B Transfer In Cluster facts that are not reversal rows) * 20

daily total fee
    = expected recharge / out-cluster fee + in-cluster fee
```

The calculation is at transaction grain, not source-row grain. A two-row
internal transfer produces one qualifying transaction, never two fees.

The expected out-cluster fee is derived monitoring information. It is not
proof that a 200 fee was posted, invoiced, or paid on that same date.

The daily fee SQL also returns source-row count and source-file provenance for
each report date.

### Daily report surfaces for Taipy

`linkaja_database_result.json` contains execution-scoped results for the
affected dates. Its `fee_rows` expose report date, cluster, expected
out-cluster fee, in-cluster fee, total fee, source-row count, and source-file
provenance. Its `detail_rows` expose reversal-aware scenario labels, company
debit/credit, source Fee, missing Fee count, expected and in-cluster fee
measures, completed in-cluster reversal count/fee, and unresolved reversal
count.

For a dashboard, use PostgreSQL as the canonical query source rather than
accumulating execution JSON files:

- `linkaja_transactions_current_v` for transaction detail and reversal state;
- `queries/linkaja_daily_fee_report.sql` for daily expected fee totals;
- `queries/linkaja_daily_detail_measures.sql` for daily scenario measures; and
- `queries/linkaja_unresolved_reversals.sql` for the exception queue.

Daily measures are gross posting-date monitoring values. When a later reversal
arrives, the original date is recalculated and the original fact becomes
`is_reversed = true`, but the existing expected/in-cluster daily fee query does
not remove that original. The reversal stays on its actual posting date. A
dashboard that needs daily net/active fees should present gross, reversal
adjustment, and net as separate measures instead of rewriting ledger history.

## Scenario-Specific Interpretation

### `Digipos B2B Transfer In Cluster`

This is usually a two-row internal transfer: company Purchase Account credit
and merchant General Account debit. Count it once for company principal
movement. The active daily in-cluster fee is one completed transaction times
20; retain any source `Fee` for audit, but do not add it to the principal.

### `Digipos B2B Transfer`

This usually represents an out-cluster company Purchase Account credit. A
transaction qualifies for the daily expected fee only when it has positive
company credit. The result is `1 * 200` per qualifying transaction. It is an
expected fee, not the posted `Digipos B2B Transfer Fee` amount.

### `Digipos B2B Transfer Fee`

This is commonly two source rows: company Purchase Account credit and merchant
General Account debit, often with a source fee of 200. Count the company credit
once for daily monitoring. Do not add the company credit, merchant debit, and
source fee together: they describe one fee event from different perspectives.

### `General to Purchase B2B Transfer Agent Telco`

This PPOB-like event can have no company Purchase Account row. The daily
DETAIL row shows its transaction-grain `source_fee` in DEBET only when the fee
is present; missing fees are counted as exceptions rather than treated as
zero. This scenario is excluded from the central daily fee total but is a
monthly release target.

### `Buy Goods Reversal for General Merchant`

Resolve it only through `Original Transaction ID`. A resolved reversal keeps
its own posting timestamp and is labeled `Complete Reversal - <original
scenario>` in the daily detail report. Do not infer a relationship from amount,
date, organization, or counterparty.

Do not move a late reversal back to the original date. Its company debit
belongs to the withdrawal interval in which it actually posts.

### Withdrawals and other scenarios

`Organization Withdraw of Funds with Next Working Day Payment` is a real
company Purchase Account debit at its own timestamp. Reconcile it between
actual withdrawal timestamps and account balances, not by adding a prior
calendar day's scenarios.

Other known and future scenarios remain available in the views and snapshot.
They are scenario-agnostic facts and are displayed in `OTHER` unless an
explicit business rule places them elsewhere.

## Monthly Materialization

Monthly close is manual. The daily workflow does not populate
`linkaja_transactions`.

Use `linkaja_monthly_materialization.yml` for one cluster and one month:

| Input | Rule |
|---|---|
| `cluster_id` | Required cluster ID. |
| `report_month` | `YYYY-MM` or the first day of the month. |
| `calculation_cutoff` | Required exclusive local WITA timestamp without a UTC offset. It must be on or after the first day of the following month. |
| `publish` | `false` previews only. `true` atomically replaces that cluster-month snapshot. |

The month-close flow is:

```text
apply and verify schema migrations
-> select completed base facts posted in the requested month
-> keep facts finalized before the exclusive cutoff
-> enrich originals and reversals using only completed raw data before cutoff
-> create temporary month snapshot stage
-> calculate daily raw rows, monthly fee rows, and unresolved-reversal detail
-> preview: return result only
-> publish: delete old cluster-month facts, insert stage, record audit row
```

The cutoff allows a deliberate late-reversal window. A reversal loaded in the
following month changes the monthly snapshot only when it was finalized before
the selected cutoff and the month is explicitly republished. Later daily CSV
uploads never silently change a frozen monthly snapshot.

### Daily raw and unresolved-reversal artifacts

Every monthly preview or publication writes three Kestra output files:

| Artifact | Contents |
|---|---|
| `linkaja_monthly_result.json` | Complete machine-readable result, including daily rows, monthly fee rows, and unresolved-reversal details. |
| `linkaja_daily_raw_calculation.csv` | Report-month totals grouped by actual posting date and reversal-aware transaction scenario. |
| `linkaja_unresolved_reversals.csv` | Closing-window reversal facts whose `Original Transaction ID` does not resolve before the selected cutoff. |

The daily raw calculation comes from the same temporary cutoff-consistent
snapshot used by the monthly preview. It exposes transaction and source-ledger
row counts; ledger debit, credit, and signed amount; company debit and credit;
source Fee; missing PPOB Fee counts; reversed-original counts; and complete or
unresolved reversal counts.

Original transactions remain on their original posting dates. Reversal rows
remain on their actual posting dates and are labeled `Complete Reversal -
<original scenario>` only when `Original Transaction ID` resolves in the same
cluster before the cutoff. An unresolved reversal is labeled separately and is
also written at transaction detail grain to the unresolved CSV. The calculation
does not look up a later withdrawal, build a MANDIRI or Cash section, or read or
write Google Sheets.

### Monthly fee release calculation

The monthly release report has two categories and is intentionally different
from the daily expected/in-cluster fee monitor:

```text
PPOB fee
    = sum(source_fee)
      for active original General to Purchase B2B Transfer Agent Telco facts

Digipos fee
    = count(active original Digipos B2B Transfer Fee facts
            whose transaction-grain source_fee is exactly 200) * 200
```

Only original transaction facts are targets. The report keeps gross totals,
then excludes `is_reversed` facts from active, net, and payable totals.

For each category, the result includes:

```text
REPORT MONTH
CLUSTER ID
FEE CATEGORY
GROSS TRANSACTION COUNT
REVERSED TRANSACTION COUNT
ACTIVE TRANSACTION COUNT
GROSS FEE
REVERSED FEE
NET FEE
MONTHLY PAYABLE FEE
GROSS MISSING FEE COUNT
REVERSED MISSING FEE COUNT
ACTIVE MISSING FEE COUNT
CALCULATION STATUS
```

For `Digipos B2B Transfer Fee`, a transaction must have `source_fee = 200` to
be eligible. Gross, reversed, net, and payable values all use this Rp200 source
Fee. Company `DEBIT` and `KREDIT` remain transaction evidence but never affect
the Digipos eligibility or payable amount. Rows with another or missing Fee are
from outside this calculation and are excluded.

For PPOB, all original `General to Purchase B2B Transfer Agent Telco` facts are
targets and their source Fee is the fee amount. If an active PPOB target has no
Fee, `NET FEE` and `MONTHLY PAYABLE FEE` are null rather than silently converted
to zero. Missing counts are populated and `CALCULATION STATUS` becomes
`INCOMPLETE_MISSING_FEE`. A verified zero remains different from missing data.
The monthly result artifact and Kestra outputs expose `incomplete_fee_rows` and
`monthly_payable_complete` so this condition is visible before release.

Unresolved reversals are counted separately for the closing window and are not
assigned to PPOB or Digipos monthly fee categories until their originals are
available.

### Monthly publication and reporting

`publish=false` creates no snapshot and no audit row. It returns a preview in
the Kestra output artifact.

`publish=true` is atomic for the selected `cluster_id + report_month`:

1. Delete the existing cluster-month rows from `linkaja_transactions`.
2. Insert the newly staged facts with report month, cutoff, and execution
   materialization ID.
3. Insert or update the corresponding `linkaja_monthly_refreshes` audit row.

The flow does not read or write Google Sheets. After publication, run
`queries/linkaja_monthly_fee_summary.sql` in pgAdmin or another PostgreSQL
client. Update only the report month and optional cluster; the report reads each
publication's recorded cutoff. Its fee result comes from
`linkaja_monthly_fee_summary_v`. The unresolved-reversal section is live so a
late original can disappear from the exception list, but the frozen snapshot
changes only after explicit republication.

Run `queries/linkaja_monthly_refresh_candidates.sql` after any later daily
uploads. A returned cluster-month may have changed facts or reversal resolution.
Preview and republish it atomically, normally with the same cutoff. Extend the
cutoff only when the close window itself is deliberately extended.

## Schema Migration Process

Schema DDL belongs only in the immutable SQL files under
`linkaja_fee_pipeline/migrations/`.

| Version | Migration | Result |
|---:|---|---|
| 001 | `initial_schema` | Creates raw ledger storage and the original derived transaction table. |
| 002 | `all_completed_transaction_facts` | Moves to transaction-grain completed facts with source-row and company-account invariants. |
| 003 | `reversal_resolution_and_unusual` | Adds explicit reversal resolution, unusual state, and reason codes. |
| 004 | `live_views_and_monthly_snapshots` | Splits live daily views from the physical monthly snapshot and adds the monthly audit table. |
| 005 | `monthly_fee_summary_view` | Adds the canonical PostgreSQL monthly Digipos/PPOB fee summary view. |
| 006 | `ledger_signed_amount` | Redefines `signed_amount` as total ledger debit minus total ledger credit and places it beside those totals in the live view. |

The migration runner:

1. Reads contiguous numbered migration files.
2. Hashes each migration file.
3. Acquires a schema advisory lock.
4. Creates or checks `linkaja_schema_migrations`.
5. Rejects unknown versions or checksum/name drift.
6. Applies missing migrations in order and records their checksums.
7. Verifies the current tables, views, primary keys, indexes, and constraints
   before any persistence or materialization proceeds.

Never edit an applied migration file. Add a new numbered migration for any
schema change. Do not create a competing schema through ad hoc SQL in a Kestra
task.

## Operating Procedure

### Daily CSV run

1. Confirm the file is a single-cluster LinkAja CSV with the expected header.
2. Run with `dry_run=true` when validating a new file shape.
3. Run with `dry_run=false` to migrate, replace raw transaction IDs, and
   calculate the affected daily dates.
4. Check task output for raw row count, impacted transaction IDs, current
   transaction rows, affected dates, and ledger measure rows; download
   `linkaja_database_result.json` when an execution artifact is required.
5. Review unresolved reversal measures before treating a daily result as
   complete.

### Monthly close

1. Ensure all source files for the month and the agreed late-reversal window
   have completed daily ingestion.
2. Run the monthly flow with `publish=false`.
3. Review `linkaja_daily_raw_calculation.csv`,
   `linkaja_unresolved_reversals.csv`, the transaction row count, missing
   amounts, monthly fee rows, and cutoff.
4. Run again with `publish=true` only after approval.
5. Run the published-snapshot SQL report and confirm every payable category has
   `CALCULATION STATUS = COMPLETE`.
6. After later daily uploads, run the refresh-candidate query and explicitly
   preview/republish any returned cluster-month before release approval.

A publication is a fresh calculation, not a promotion of the saved preview.
If raw data changes between preview and publication, the result can change.

## Verification Queries

The read-only, pgAdmin-ready files under `queries/` are:

| Query | Purpose |
|---|---|
| `linkaja_monthly_fee_summary.sql` | Official report from a published snapshot plus closing-window unresolved reversals. |
| `linkaja_monthly_refresh_candidates.sql` | Published cluster-months that may need republication after late raw ingestion. |
| `linkaja_daily_fee_report.sql` | Daily expected out-cluster and in-cluster values for database/dashboard reporting. |
| `linkaja_daily_detail_measures.sql` | Reversal-aware daily scenario and ledger measures. |
| `linkaja_live_transactions.sql` | Transaction-level live investigation. |
| `linkaja_unresolved_reversals.sql` | Current unresolved reversal exceptions. |
| `linkaja_monthly_snapshot_audit.sql` | Monthly publication history and snapshot counts. |
| `linkaja_relation_counts.sql` | Raw, live-view, and snapshot row counts. |

Edit only the parameter CTEs before running these reports. Transactional
replacement SQL remains in `sql.py`, while schema DDL remains in
`migrations/`.

Use the following queries for routine inspection. Set `cluster_id`, dates, and
month to the required values.

```sql
-- Raw rows, live facts, and published monthly facts.
SELECT 'raw' AS relation, COUNT(*) FROM linkaja_raw_transactions
UNION ALL
SELECT 'base view', COUNT(*) FROM linkaja_transaction_base_v
UNION ALL
SELECT 'current view', COUNT(*) FROM linkaja_transactions_current_v
UNION ALL
SELECT 'monthly snapshot', COUNT(*) FROM linkaja_transactions;

-- Live daily scenario and company-movement summary.
SELECT
    report_date,
    transaction_scenario,
    COUNT(*) AS transaction_count,
    SUM(company_debit) AS company_debit,
    SUM(company_credit) AS company_credit,
    SUM(source_fee) AS source_fee
FROM linkaja_transactions_current_v
WHERE cluster_id = '411311'
GROUP BY report_date, transaction_scenario
ORDER BY report_date, transaction_scenario;

-- Current unresolved reversals.
SELECT
    report_date,
    finalized_at_local,
    transaction_id AS reversal_transaction_id,
    original_transaction_id,
    company_debit,
    unusual_reason_codes
FROM linkaja_transactions_current_v
WHERE cluster_id = '411311'
  AND reversal_resolution_status = 'UNRESOLVED'
ORDER BY finalized_at_local, transaction_id;

-- Published snapshot audit.
SELECT *
FROM linkaja_monthly_refreshes
WHERE cluster_id = '411311'
ORDER BY report_month, refreshed_at DESC;
```

## Validation and Testing

Run these checks after changing LinkAja code, SQL, or workflow structure:

```bash
python3 -m compileall -q linkaja_pipeline.py linkaja_fee_pipeline tests
python3 -m unittest discover -s tests
git diff --check
docker build -f Dockerfile.linkaja -t linkaja-fee-pipeline:3.11 .
```

The PostgreSQL integration tests require `LINKAJA_TEST_DSN` to point to a
dedicated disposable database. They truncate LinkAja business and audit tables;
never target production or a shared development database.

## Non-Negotiable Rules

Do not:

- count CSV rows as transactions;
- sum both sides of an internal transfer as company movement;
- sum the `Balance` column;
- treat `Fee` as universally additive to debit or credit;
- infer reversal links by equal amount or date when `Original Transaction ID`
  exists;
- move a reversal to the original transaction date;
- calculate withdrawal settlement from a fixed D-to-D+1 calendar rule;
- treat expected out-cluster fees and posted Digipos fee credits as the same
  measure;
- use company DEBIT or KREDIT to calculate the monthly Digipos fee;
- convert a missing active PPOB Fee to zero;
- make daily ingestion rewrite `linkaja_transactions`; or
- edit a committed migration file after it has been applied.

When a reconciliation does not match, report the evidence: timing/cutoff,
missing history, unresolved reversal, duplicate, status, adjustment, account
scope, or another identifiable exception. Do not change a calculation merely
to force a sample day's totals to equal a withdrawal.
