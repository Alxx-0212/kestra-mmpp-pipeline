# FinPay Reporting Agent Guide

Read [`README.md`](README.md) before changing FinPay reporting behavior. It is
the maintained workflow, calculation, sheet-layout, and persistence contract.

## Scope

- Package: `finpay_pipeline/**`
- Workflow: `workflows/finpay_pipeline.yml`
- Compatibility entry points: `pipeline.py`, `pipeline_refactored.py`
- Runtime: `Dockerfile`, `requirements.txt`
- Operational SQL: `queries/`
- Tests: `tests/` and shared repository contract tests under `../tests/`

## Invariants

- Preserve package-local compatibility exports, workflow IDs/imports, artifact names, and dry-run behavior.
- Keep calculations, PostgreSQL relations, Parquet artifacts, and Google Sheets writes idempotent by cluster/report date.
- Keep unusual detection before calculation deduplication; preserve source `Remarks` and write diagnostics to `unusual_reason`.
- Do not assume every unusual row is excluded from summaries.
- Keep Google Sheets writes sequential and repair the stable `LinkAja` reference sheet before formulas depend on it.
- Keep FinPay calculations independent from LinkAja modules/dbt models; the shared worksheet is a reporting input only.
- New schema/column changes require an explicit migration, compatibility assessment, backfill decision, and rollback plan.

Use [shared validation](../docs/validation.md), [security](../docs/security.md), and [lane rules](../docs/agent-lanes.md) for repository-wide procedures.
