# FinPay Agent Guide

Own only `finpay_pipeline/**` and related FinPay tests. Read
[`README.md`](README.md) for current workflows, input/data contracts, and pending
features. Keep previous-format and cell-format entry points independent; share
existing pure utilities rather than duplicating financial rules.

## Change Rules

- Preserve existing public imports, compatibility facades, flow IDs, task artifacts, images, and secret names unless an explicit migration accounts for callers.
- Keep source validation and computation free of clients. Reporting dry runs must not connect to PostgreSQL, Google, or Telegram; acquisition dry runs still contact DigiPOS.
- Preserve raw labels, exact IDs, monetary values, and `Remarks`; write diagnostics to `unusual_reason`. Run unusual detection before calculation deduplication; unusual does not always mean excluded.
- Preserve effective-date family policy and the exact raw-RECHARGE standalone SELLTHRU exception described in the README. New labels or remark-dependent rules require source characterization and regression coverage; DISBURSEMENT remains label-authoritative in v5 and cell summaries.
- Keep immutable source generations and ledger rows append-only. Treat [`PRODUCTION_REPLICATION.md` Section 6.1](PRODUCTION_REPLICATION.md#61-one-time-development-reset-exception) as a historical record only; it grants no current or future authorization. Before any reset, repair, or destructive cleanup, require fresh, explicit authorization from the current user for the exact target, scope, operation, and backup plan; any no-backup exception also requires fresh, explicit current-user authorization. Current data and reports remain idempotent by cluster/cell/report date; incomplete acquisition must never clear current data.
- Keep daily, monthly, and cell migrations separate. Retain the legacy compatibility initializer; new schema changes need explicit migration, backfill, compatibility, and rollback decisions. `004_finpay_cell_remove_publication_state.sql` is destructive, targets only the `finpay` database, and must not run without current-user approval and a verified backup. Do not persist source `No` or introduce surrogate IDs.
- Bulk persistence preflights every member, uses a bounded checksummed cohort spool, and uses COPY only for new generations inside the existing per-cluster transaction. Preserve source-position order and duplicate rows.
- Cell-day and range persistence replace only selected complete date/cell cohorts while retaining prior source generations and ledger rows append-only. After all selected data is persisted, publish only requested date blocks from database history. Locate each block by `REPORT DATE` in column A and append or replace that date without rewriting other dates. Reassert the shared headers and Summary opening row from the same database-derived matrix. SALDO and combined MANDIRI are formulas; MANDIRI derives from the next date's raw `CASHOUT APOLLO` KREDIT, located by its displayed `TRANSFER MASUK DARI FINPAY` row, and displays `PENDING NEXT DAY` when unavailable. Manual MANDIRI inputs, publication snapshots, and write-state rows are unsupported. Bulk validates complete persisted history for calculation but publishes only the requested dates.
- Cell targets resolve by pinned spreadsheet ID or unique exact spreadsheet title. A missing pinned ID fails closed; name-only configuration creates the workbook if absent. Preflight creates missing exact account/role tabs, preserving configured worksheet IDs when supplied. A newly created workbook is shared only with the configured Finance editors; existing workbook sharing is unchanged.
- Maintain the managed display-only protection for each computed tab and remove only legacy MANDIRI input holes or publisher guards; preserve unmanaged protections and workbook sharing.
- Keep date-block writes sequential and retain the existing API pacing, Kestra retry/backoff policies, and 30-second cohort pause. DB advisory locks are transient process exclusion only; do not persist publication state. Each date's row mutations, values, formulas, spacer formatting, and managed protection updates across the four roles share bounded `spreadsheets.batchUpdate` requests; one targeted grid-data readback verifies the written blocks and invoice spacer fill. Mutation requests are not retried by the HTTP adapter.
- Queue or reject overlapping legacy writes to the same spreadsheet/worksheet/date across entry points until a shared exclusion policy is tested. A green cell tab is not database completion confirmation.
- Keep cell publication separate from legacy targets and LinkAja calculations. Legacy invoice writing must repair the stable `LinkAja` reference worksheet before formulas use it.
- Preserve monthly cutoff, source coverage/fingerprint, exact-ID reversal, approval, and frozen-snapshot gates. A daily backfill is not monthly/Odoo publication approval.
- Keep nested runtime packages included explicitly in their owning Dockerfiles. Credentials remain Kestra secrets. Workbook and role-tab creation is limited to the exact configured title/aliases; sharing changes only when the workflow creates a new workbook and only for configured Finance editors. Deployment remains a separate approval.
- Keep cell target defaults empty. Resolve a destination from explicit flow variables/Kestra secrets; require the legacy workbook ID, writer identity, and Finance editor identities. When Sheets output is disabled, persistence remains independent of Google clients. Reporting dry runs do not connect to Google or PostgreSQL.

## Checks

Use Python 3.11 and the owning cached runtime for the offline suite and pinned
Kestra `v1.3.26` for native flow validation; commands are in the README.
Check new flow imports against the package public API or explicit subpackage
entry point. Run focused regression tests, then the complete FinPay suite and
`git diff --check`. Report actual executed/skipped counts and unavailable lint,
native execution, image-build, or live-service checks; missing dependencies are
not a pass. Keep execution-result logs out of maintained operating docs.

Before runtime edits, checks, live data access, or builds, follow the relevant
[architecture](../docs/architecture.md), [validation](../docs/validation.md),
[security](../docs/security.md), [dependency](../docs/dependencies.md), and
[ownership](../docs/agent-lanes.md) rules. Preserve unrelated work; coordinate
root/shared infrastructure changes with the integrating owner.

For image release, CI/CD, or production commissioning, follow
[production replication](PRODUCTION_REPLICATION.md). Its procedure does not grant
authorization beyond its stated scope for deployment, resets, or sharing changes
to an existing workbook.
