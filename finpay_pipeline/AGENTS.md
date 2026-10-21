# FinPay Agent Guide

Own only `finpay_pipeline/**` and related FinPay tests. Read
[`README.md`](README.md) for current workflows, input/data contracts, and pending
features. Keep previous-format and cell-format entry points independent; share
existing pure utilities rather than duplicating financial rules.

## Change Rules

- Preserve existing public imports, compatibility facades, flow IDs, task artifacts, images, and secret names unless an explicit migration accounts for callers.
- Keep source validation and computation free of clients. Reporting dry runs must not connect to PostgreSQL, Google, or Telegram; acquisition dry runs still contact DigiPOS.
- Preserve raw labels, exact IDs, monetary values, and `Remarks`; write diagnostics to `unusual_reason`. Run unusual detection before calculation deduplication; unusual does not always mean excluded.
- Preserve effective-date family policy and the exact raw-RECHARGE standalone SELLTHRU exception described in the README. New labels or remarks require source characterization and regression coverage.
- Keep immutable source generations and ledger rows append-only. Current data and reports remain idempotent by cluster/cell/report date; incomplete acquisition must never clear current data.
- Keep daily, monthly, and cell migrations separate. Retain the legacy compatibility initializer; new schema changes need explicit migration, backfill, compatibility, and rollback decisions. Do not persist source `No` or introduce surrogate IDs.
- Initialize the new cell Sheet running SALDO at zero on `2026-10-01`, not on every execution. Reject missing earlier coverage; create no opening transaction or separate balance table. Preserve source balance evidence and manual MANDIRI inputs.
- Cell worksheets use `cluster_alias` titles from `CLUSTER_ALIASES`. Permit only an exact numeric-code-to-alias title migration on the same approved workbook and worksheet IDs; preserve the prior epoch and manual inputs.
- Keep Sheet writes sequential and lock target/cluster state across guarded publication. A separated writer must reacquire locks and reject a stale persisted handoff before any Google mutation. Never bypass `IN_FLIGHT`/`UNKNOWN` recovery gates or add blind mutation retries.
- Queue or reject overlapping legacy writes to the same spreadsheet/worksheet/date across entry points until a shared exclusion policy is tested. A green cell tab is not database completion confirmation.
- Keep cell publication separate from legacy targets and LinkAja calculations. Legacy invoice writing must repair the stable `LinkAja` reference worksheet before formulas use it.
- Preserve monthly cutoff, source coverage/fingerprint, exact-ID reversal, approval, and frozen-snapshot gates. A daily backfill is not monthly/Odoo publication approval.
- Keep nested runtime packages included explicitly in their owning Dockerfiles. Credentials remain Kestra secrets. Existing-workbook sharing is never changed; first-time spreadsheet creation may share only with the explicitly configured Finance editor list when `create_spreadsheet_if_missing` is enabled. Deployment remains a separate approval.
- Keep cell target defaults empty. Resolve a destination from explicit flow variables/Kestra secrets; require the legacy workbook, writer, and Finance editor list. On a non-dry publish, create a missing workbook only when `create_spreadsheet_if_missing` is enabled and create missing alias tabs only when `create_worksheets_if_missing` is enabled, under the target-operation journal. Share Finance editors only on a newly created workbook; existing workbook sharing remains unchanged. Missing tabs with prior publication history block rather than rebind.

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
