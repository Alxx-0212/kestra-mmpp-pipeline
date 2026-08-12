# FinPay Agent Guide

Read `README.md` in this directory before changing FinPay behavior. It is the
maintained workflow, calculation, sheet-layout, and persistence contract. The
workflow and code remain runtime truth; update the guide in the same change when
they disagree.

## Scope

- Workflow: `../finpay_pipeline.yml`
- Public compatibility entry point: `../pipeline.py`
- Legacy compatibility entry point: `../pipeline_refactored.py`
- Runtime image and dependencies: `../Dockerfile`, `../requirements.txt`
- Contract tests: `../tests/test_refactor_contracts.py`
- Workflow contracts: `../tests/test_finpay_workflow_contracts.py`
- Operational SQL: `queries/`

## Concurrent ownership

When working beside a LinkAja agent, edit only the FinPay lane defined in the
root `AGENTS.md`. Do not edit LinkAja workflows, package files, migrations,
dependencies, or tests. Send required changes to root/shared files to the
integrating agent instead of editing them concurrently.

`summary_sheets.py` and its tests remain FinPay-owned even though the report
integrates with the `LinkAja` worksheet. If the LinkAja agent proposes a new
reference-sheet column or header contract, record the requested shape in the
handoff and let the integrating agent coordinate the FinPay-side formula change.

## Public contracts

- Preserve flow ID and namespace, inputs, task IDs, task imports, image name,
  secret names, output names, Parquet filenames, and dry-run side effects in
  `finpay_pipeline.yml` unless the change is handled as a workflow migration.
- Preserve exports in `finpay_pipeline/__init__.py`, `pipeline.py`, and
  `pipeline_refactored.py` until all Kestra and externally ambiguous callers
  are accounted for.
- Treat PostgreSQL table names/grains and Google spreadsheet, worksheet,
  formula, protection, and row-layout behavior as public data contracts.

## Required invariants

- Keep the `from pipeline import ...` Kestra contract stable unless every task
  import and `finpay_pipeline/__init__.py` are updated together.
- Preserve rerun idempotency for `cluster_id + report_date` in PostgreSQL and
  Google Sheets.
- Keep `dry_run=true` free of PostgreSQL, Google Sheets, and notification side
  effects.
- Keep unusual detection before calculation deduplication so duplicate source
  rows remain visible in the unusual report.
- Do not assume every unusual row is excluded from the summary.
- Keep Google Sheets writes sequential unless retry and rate-limit behavior is
  deliberately redesigned.
- Do not allow overlapping writes to the same spreadsheet, worksheet, and
  report date. Queue or reject that concurrency until a tested lock and retry
  policy exists.
- Preserve source `Remarks`; write diagnostics to `unusual_reason`.
- Do not persist source `No` or introduce surrogate IDs into FinPay-owned
  tables without an explicit schema migration decision.
- Treat the current automatic database schema reconciliation as legacy runtime
  behavior; do not extend it with new implicit schema changes. New table or
  column changes require an explicit migration, compatibility assessment,
  backfill decision, and rollback plan.
- Keep FinPay calculations independent from LinkAja modules and dbt models.
- Preserve the intentional LinkAja reporting integration in
  `summary_sheets.py`: create or repair the stable `LinkAja` reference sheet
  before writing FinPay invoice formulas, and preserve its headers, cluster
  mapping, columns, and formula references. A missing reference sheet causes
  the FinPay invoice formulas to fail.

## Validation

Run FinPay checks with Python 3.11 and dependencies from `requirements.txt`:

```bash
python3 -m compileall -q pipeline.py pipeline_refactored.py finpay_pipeline tests/test_refactor_contracts.py tests/test_finpay_workflow_contracts.py
python3 -m unittest -v tests.test_finpay_workflow_contracts
python3 -m unittest -v tests.test_refactor_contracts
git diff --check
git diff --cached --check
```

Do not report the FinPay suite as passing when imports failed or all relevant
tests were skipped. When workflow imports change, verify every
`from pipeline import ...` symbol in the YAML is exported by
`finpay_pipeline/__init__.py` and validate the flow against the deployed Kestra
version. Use the root `config/kestra-validation.yml` for the pinned local
validation command documented in the root and FinPay README files.

Before a production release, after verifying the root `.dockerignore`, build
`finpay-pipeline:3.11`, smoke-test its public imports, and run a representative
workflow dry run plus a serialized rerun/idempotency check. Leave the full
repository suite and Compose validation to the root guide.
