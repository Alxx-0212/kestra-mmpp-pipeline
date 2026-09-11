# LinkAja Agent Guide

Read [`README.md`](README.md) before changing LinkAja behavior. It is the
canonical workflow, schema, calculation, and operating contract.

## Scope

- Package: `linkaja_fee_pipeline/**`
- Daily workflow: `workflows/linkaja_fee_pipeline.yml`
- Monthly workflow: `workflows/linkaja_monthly_materialization.yml`
- Compatibility entry point: `linkaja_pipeline.py`
- Migrations: `migrations/`
- Operational queries: `queries/`
- Learning-only dbt: `dbt/`

The dbt project is not called by Kestra. PostgreSQL migrations, Python
persistence, and the published monthly snapshot remain the production path.

## Invariants

- Aggregate by `cluster_id + transaction_id`; never sum `Balance` or both sides of an internal transfer.
- Resolve reversals only through exact `Original Transaction ID` in the same cluster and retain finalized reversal timestamps.
- Daily loads replace affected transaction IDs; monthly publication replaces one `cluster_id + report_month` snapshot.
- Keep Digipos eligibility, PPOB fees, missing-fee nulls, and `signed_amount` definitions from the README unchanged.
- Keep unresolved reversals as unusual data; never infer them from amount/date/outlet.
- Never edit an applied migration. Add the next migration and document rollback.

Use [shared validation](../docs/validation.md), [security](../docs/security.md), and [lane rules](../docs/agent-lanes.md) for repository-wide procedures.
