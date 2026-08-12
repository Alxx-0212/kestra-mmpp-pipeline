# LinkAja Taipy Dashboard and Reporting Contract

Status: approved direction for the first dashboard release. PostgreSQL remains
the system of record; Kestra remains the only ingestion and monthly-publication
runtime. Taipy is a read-only presentation application.

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
   adjustments, and active/net values. It must not relabel the existing gross
   daily fee monitor as payable.
6. Monthly payable fee comes only from the two approved source scenarios:
   `Digipos B2B Transfer Fee` and
   `General to Purchase B2B Transfer Agent Telco`.
7. There is no MANDIRI, Cash, next-available-withdrawal, or spreadsheet layout
   in the target reporting model.
8. The learning-only dbt project does not become a production dependency.

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
        +-> daily/current report queries ---------+--> Taipy (read-only)
        |                                              /linkaja
        +-> Kestra monthly preview/publish --------> frozen monthly views
                                                       ^
legacy company web -> authenticated redirect/proxy ---+
```

Deployment contract:

- Host Taipy behind the company reverse proxy at a stable path such as
  `/linkaja`; configure Taipy's base URL to match that path.
- The legacy web redirects users to that route. Prefer the existing company
  identity/session at the proxy boundary.
- Give Taipy a PostgreSQL role with `SELECT` on approved views and no write,
  migration, or raw-replacement permissions.
- Taipy must not duplicate Kestra scheduling, ingestion, or month-close logic.
- Kestra JSON/CSV outputs remain execution evidence and downloads, not the
  dashboard's accumulated database.

Taipy can run with an external FastAPI server and supports a configured base
URL. A PostgreSQL SQL data node is also available, but the first version may
use a small read-only repository layer if that makes parameterized queries and
connection pooling clearer:

- <https://docs.taipy.io/en/latest/userman/advanced_features/configuration/gui-config/>
- <https://docs.taipy.io/en/latest/userman/scenario_features/data-integration/data-node-config/>
- <https://docs.taipy.io/en/latest/userman/scenario_features/data-integration/data-node-usage/>

Taipy's documented application authentication is an Enterprise feature. When
using Community edition, authentication and authorization must be enforced by
the legacy application or reverse proxy:

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
FEE`, and `TOTAL FEE` are currently gross monitoring values. They are not a
reversal-adjusted settlement or monthly payable result.

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

### Daily monitoring

```text
gross expected out-cluster fee
  = count(original Digipos B2B Transfer with company_credit > 0) * Rp200

gross in-cluster fee
  = count(original Digipos B2B Transfer In Cluster) * Rp20

gross daily monitoring total
  = gross expected out-cluster fee + gross in-cluster fee
```

For the dashboard, the approved daily presentation is:

```text
gross fee       = fee attached to originals on their actual posting dates
reversed fee    = fee of originals that now have a completed linked reversal
active/net fee  = gross fee - reversed fee
reversal event  = shown separately on the reversal's actual posting date
unresolved      = shown separately; never guessed from amount or date
```

The current daily SQL supplies gross and event/exception measures, but does not
yet expose a single canonical gross/reversed/active row. That additive report
surface is a pre-dashboard schema task; it must not alter raw facts or dates.

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

## Dashboard Pages

1. **Overview** — latest successful load by cluster, raw/live row counts,
   unresolved reversal count, incomplete fee count, and last monthly publish.
2. **Daily ledger** — date/cluster/scenario filters; ledger debit, credit,
   signed amount, company movement, and source provenance.
3. **Daily fees** — gross, reversed, active/net, and reversal-event measures
   for expected out-cluster, in-cluster, posted Digipos fee, and PPOB fee.
4. **Reversal investigation** — unresolved and unusual rows with original ID,
   posting time, source file, load ID, and transaction drill-through.
5. **Monthly close** — preview/download links, frozen cutoff, publication audit,
   gross/reversed/active/payable fee, missing Fee counts, and refresh candidates.
6. **Transaction detail** — all ledger rows and generated transaction/reversal
   attributes for one cluster and transaction ID.

The first release is read-only. Kestra execution or monthly publication buttons
should be considered later and require explicit authorization, audit, and
idempotency handling.

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

It is not yet fully dashboard-release-ready. Complete these items before using
the dashboard as an approval or settlement surface:

### Required before dashboard release

1. Add a canonical read-only daily fee summary view or repository query with
   gross, reversed, active/net, reversal-event, and unresolved measures. Keep
   the existing gross execution artifact for backward compatibility.
2. Correct blank `Balance` normalization so a missing balance remains null
   instead of becoming numeric zero.
3. Make monthly `calculation_status` depend on
   `active_missing_fee_count`, matching the payable-null rule. The current SQL
   uses the stricter gross-missing count for status even when only a reversed
   PPOB original lacks Fee.
4. Return stable zero rows for both monthly fee categories when a cluster-month
   has no matching transactions, so dashboard categories do not disappear.
5. Expose data freshness explicitly. Add a load-audit relation containing
   execution/load ID, cluster, source file, start/end state, row count, affected
   dates, and error state, or read equivalent execution status from Kestra.
6. Create and test a read-only dashboard database role and approved-view grants.

All database changes must be additive numbered migrations. Do not edit already
applied migrations 001 through 006.

### Follow-up hardening

- Add unusual-state rules for multiple reversals, reversal chains/cycles, and
  any source pattern confirmed to be invalid by operations.
- Validate empirically that transaction IDs never collide inside one cluster.
- Label all `_local` timestamps as WITA in the UI and convert only at display
  boundaries.
- Measure query volume before adding materialized summaries or indexes.
- Rename shared `FINPAY_DB_*` and Docker network configuration only as a
  coordinated infrastructure change; it is coupling, not a calculation error.

## Delivery Sequence

1. **Current repository change** — remove LinkAja Sheet tasks and credentials;
   keep daily and monthly Kestra database/artifact outputs.
2. **Reporting contract migration** — implement the required daily summary,
   monthly status/category stability, balance null fix, freshness audit, and
   read-only grants with integration tests.
3. **Taipy MVP** — build Overview, Daily ledger, Daily fees, Reversals, Monthly
   close, and Transaction detail against read-only PostgreSQL queries.
4. **Legacy integration** — deploy behind the existing authenticated reverse
   proxy at `/linkaja`, then add the redirect/navigation entry.
5. **Operational acceptance** — reconcile representative clusters against raw
   exports, complete and unresolved reversals, late-original resolution, a
   missing PPOB Fee, an empty fee category, reruns, and monthly republication.

The dashboard can be released for analytical use after steps 2 through 4. It
should be treated as a payable approval surface only after step 5 passes with
business owners.
