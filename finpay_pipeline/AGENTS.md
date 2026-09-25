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

`summary_sheets.py` and its tests remain FinPay-owned even though the report
integrates with the `LinkAja` worksheet. If the LinkAja agent proposes a new
reference-sheet column or header contract, record the requested shape in the
handoff and let the integrating agent coordinate the FinPay-side formula change.

## Public contracts

- Preserve flow ID and namespace, inputs, task IDs, package imports, image name,
  secret names, output names, Parquet filenames, and dry-run side effects in
  `finpay_pipeline.yml` unless the change is handled as a workflow migration.
- Preserve exports in `finpay_pipeline/__init__.py`, `pipeline.py`, and
  `pipeline_refactored.py` until all Kestra and externally ambiguous callers
  are accounted for.
- Treat PostgreSQL table names/grains and Google spreadsheet, worksheet,
  formula, protection, and row-layout behavior as public data contracts.

## Required invariants

- Keep Kestra imports on the `finpay_pipeline` public package API. The runtime
  image copies the package directory and does not provide root-level
  `pipeline.py` compatibility facades; update task imports and
  `finpay_pipeline/__init__.py` exports together.
- Preserve rerun idempotency for `cluster_id + report_date` in PostgreSQL and
  Google Sheets.
- Keep `dry_run=true` free of PostgreSQL, Google Sheets, and notification side
  effects.
- Keep unusual detection before calculation deduplication so duplicate source
  rows remain visible in the unusual report.
- Evaluate each transaction family once and derive unusual output and summary
  membership from the same disposition. Unknown labels, unknown remarks,
  retired post-cutoff SLSFEE, and malformed standalone ST families quarantine
  the complete family; missing or non-Rp100 ST fee totals remain flagged but
  included for finance review.
- The exact raw `RECHARGE` plus normalized `transaksi sellthru` case is a
  permanent no-companion exception. It does not require `SELLTHRUFEE` or
  `SELLTHRUSALESFEE`; direct processed `SELLTHRU` rows do not receive this
  exemption.
- Before `2026-09-01` Asia/Makassar normal ST requires SLSFEE; from that date
  normal ST requires only SELLTHRUFEE totaling Rp100 and any incoming SLSFEE
  quarantines the family. Historical reruns retain the legacy sheet layout.
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
- Keep `finpay_source_loads` and `finpay_ledger_events` append-only across daily
  reruns. A corrected upload supersedes a source generation; it must not delete
  the prior generation.
- Daily raw persistence applies only `migrations/001_finpay_daily_source_evidence.sql`.
  Do not add monthly snapshots, preview tables, expectations, or reversal views
  to the daily initializer. Monthly workflow initialization applies the daily
  evidence DDL plus `migrations/002_finpay_monthly_model.sql`.
- Keep `001_finpay_transaction_model_legacy_compat.sql` unchanged as a
  compatibility/rollback initializer. New daily workflow code must use the
  daily-only helper; monthly code must use the monthly-model helper.
- Monthly FinPay publication must use an explicit exclusive Asia/Makassar
  cutoff, guarded exact-ID reversal resolution, source fingerprint, and release
  gate. Preview must not mutate frozen monthly snapshots; final publication must
  block incomplete source coverage or unresolved reversal exposure.
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

## Kestra API Trigger Guide

FinPay flows are manual. They have no schedule trigger and are executed through
the Kestra API.

Set credentials in the calling shell without printing them:

```bash
export KESTRA_URL="http://localhost:8081"
export KESTRA_TENANT="main"
export KESTRA_USER="..."
export KESTRA_PASSWORD="..."
```

Execution endpoint:

```text
POST /api/v1/{tenant}/executions/{namespace}/{flow_id}
```

Poll execution state with:

```text
GET /api/v1/{tenant}/executions/{execution_id}
```

Only `SUCCESS` is a successful result. Inspect task states and logs for all
other terminal states.

### Daily flow

Flow:

```text
finance.finpay.finpay_daily_pipeline_v5
```

Inputs:

| Input | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `csv_file` | FILE | yes | - | FinPay CSV/XLSX upload. |
| `dry_run` | BOOLEAN | no | `false` | Validate/classify without normal PostgreSQL, Sheets, or notification side effects. |
| `source_filename` | STRING | no | empty | Original basename when Kestra stores the FILE under a temporary `.upl` path. |
| `write_gsheet` | BOOLEAN | no | `true` | Enable/disable Google Sheets side effects while retaining DB persistence. |
| `send_telegram` | BOOLEAN | no | `true` | Disable Telegram alerts for isolated simulations while retaining Sheet writes. |

Filename contract:

```text
finpay-<cluster_id>(<DD-MM-YYYY>to<DD-MM-YYYY>).csv|xlsx
```

Production upload:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "files=@/absolute/path/finpay-411311(01-08-2026to01-08-2026).xlsx;filename=csv_file" \
  -F "source_filename=finpay-411311(01-08-2026to01-08-2026).xlsx" \
  -F "dry_run=false" \
  -F "write_gsheet=true" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.finpay/finpay_daily_pipeline_v5"
```

Use `write_gsheet=false` only for an isolated database replay. Submit the next
chronological file after the previous execution reaches `SUCCESS`.

### Production daily backfill gate

Before a production historical backfill:

1. Apply/verify only the daily evidence schema. Do not run the monthly flow or
   mark a month Odoo-final as part of daily backfill.
2. Take a verified PostgreSQL backup and record current row/load counts.
3. Run a dry-run canary, then one database + Sheet canary for one cluster/date.
4. Confirm the canary source load, raw row count, daily summary, unusual count,
   Sheet update, and rerun behavior.
5. Process files in ascending report date, one execution at a time. Stop on the
   first non-`SUCCESS` state; do not submit later files over a failed date.
6. Compare source file/row/date coverage with the backfill inventory and verify
   all generations remain retained, including superseded generations.

Before enabling the pipeline DB role in production, run a read-only privilege
preflight using that role against the target database:

```sql
SELECT current_user,
       has_schema_privilege(current_user, 'public', 'USAGE') AS public_usage,
       has_schema_privilege(current_user, 'public', 'CREATE') AS public_create;

SELECT has_table_privilege(current_user, 'finpay_raw_transactions', 'SELECT,INSERT,UPDATE,DELETE'),
       has_table_privilege(current_user, 'finpay_source_loads', 'SELECT,INSERT,UPDATE,DELETE'),
       has_table_privilege(current_user, 'finpay_ledger_events', 'SELECT,INSERT,UPDATE,DELETE');
```

The first daily canary must apply only daily evidence DDL; it must not create or
change monthly snapshot/publication relations. Keep the production monthly flow
disabled for this daily backfill rollout.

Backup verification example (use the approved production connection secret or
`PGSERVICE`, never place credentials in the command text):

```bash
pg_dump --format=custom --file="$BACKUP_FILE" "$PROD_DATABASE_URL"
pg_restore --list "$BACKUP_FILE" >/dev/null
```

After migration, verify the deployed role can use/create in the target schema
and can select/insert/update/delete the daily raw and source-evidence relations.
Run these checks using the production pipeline role, not a DBA/superuser role.

The daily schema supports evidence collection and daily reporting. Reversal
resolution in the monthly model is not authorization to settle Odoo; keep monthly
publication disabled until Finance approves its close gates and waiver policy.

### Monthly flow

Flow:

```text
finance.finpay.finpay_monthly_materialization_v1
```

Inputs:

| Input | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `cluster_id` | STRING | yes | - | FinPay cluster. |
| `report_month` | STRING | yes | - | `YYYY-MM` report month. |
| `calculation_cutoff` | STRING | yes | - | Exclusive Asia/Makassar cutoff. |
| `expected_source_dates` | STRING | yes | empty | Complete comma-separated expected dates. |
| `publish` | BOOLEAN | yes | `false` | Preview when false; publish snapshot when true. |
| `expected_source_fingerprint` | STRING | yes | empty | Approved preview fingerprint required for publication. |
| `approved_by` | STRING | yes | empty | Finance/operator approval identity. |
| `write_gsheet` | BOOLEAN | yes | `true` | Write the monthly worksheet after PostgreSQL publication. |

Preview:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "cluster_id=411311" \
  -F "report_month=2026-08" \
  -F "calculation_cutoff=2026-09-18T00:00:00" \
  -F "expected_source_dates=$EXPECTED_DATES" \
  -F "expected_source_fingerprint=" \
  -F "publish=false" \
  -F "approved_by=" \
  -F "write_gsheet=false" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.finpay/finpay_monthly_materialization_v1"
```

Review the preview and publish only with its exact fingerprint:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "cluster_id=411311" \
  -F "report_month=2026-08" \
  -F "calculation_cutoff=2026-09-18T00:00:00" \
  -F "expected_source_dates=$EXPECTED_DATES" \
  -F "expected_source_fingerprint=$APPROVED_PREVIEW_FINGERPRINT" \
  -F "publish=true" \
  -F "approved_by=finance@example.com" \
  -F "write_gsheet=true" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.finpay/finpay_monthly_materialization_v1"
```

Publication blocks incomplete source coverage, unresolved or repeated reversals,
QRIS date exceptions, fingerprint changes, and incomplete months.

Before a production release, after verifying the root `.dockerignore`, build
`finpay-pipeline:3.11`, smoke-test its public imports, and run a representative
workflow dry run plus a serialized rerun/idempotency check. Leave the full
repository suite and Compose validation to the root guide.
