# LinkAja Agent Guide

Read `README.md` in this directory before changing LinkAja behavior. It is the
canonical workflow, schema, calculation, and operating reference.

## Scope

- Daily workflow: `../linkaja_fee_pipeline.yml`
- Monthly workflow: `../linkaja_monthly_materialization.yml`
- Python compatibility entry point: `../linkaja_pipeline.py`
- PostgreSQL migrations: `migrations/`
- Read-only operational reports: `queries/`
- Learning-only dbt project: `dbt/`

The dbt project is not called by Kestra yet. PostgreSQL migrations, Python
persistence, and the published monthly snapshot remain the production path.

## Required invariants

- Treat the export as ledger rows and aggregate business events by
  `cluster_id + transaction_id`.
- Keep raw storage and derived views scenario-agnostic. New scenarios must not
  disappear merely because reporting does not classify them yet.
- Resolve reversals only through `Original Transaction ID` in the same cluster.
  Never infer a reversal from equal amount, date, organization, or outlet.
- Keep reversals on their actual finalized timestamps. Do not move them back to
  the original transaction date or apply them twice.
- Preserve rerun behavior: daily loads replace affected transaction IDs;
  monthly publication replaces only one `cluster_id + report_month` snapshot.
- Never edit an applied migration. Add the next numbered migration.
- Do not sum `Balance` or both sides of an internal transfer.
- Keep `signed_amount` ledger-scoped as
  `ledger_debit_total - ledger_credit_total`; do not derive it from company
  Purchase Account fields.
- Do not treat source `Fee` as universally additive to debit or credit.

## Monthly fee contract

- Digipos eligibility requires scenario `Digipos B2B Transfer Fee` and
  transaction-grain `source_fee = 200`.
- Digipos payable is Rp200 per eligible active original transaction.
- A completed linked reversal before the cutoff makes its original inactive.
- `company_credit` is reconciliation evidence only; it does not determine the
  Digipos payable amount.
- PPOB payable is the active `source_fee` for
  `General to Purchase B2B Transfer Agent Telco`.
- Missing active PPOB fees must produce null net/payable values,
  `INCOMPLETE_MISSING_FEE`, and data-quality counts, not zero.
- Unresolved reversals remain a separate unusual report and are not assigned to
  Digipos or PPOB from their amount.
- `linkaja_monthly_fee_summary_v` is the canonical published summary surface.
- Late raw data updates the live views, not the frozen monthly snapshot. Use the
  refresh-candidate query, then explicitly preview and republish the affected
  cluster-month.

## Validation

```bash
python3 -m compileall -q linkaja_pipeline.py linkaja_fee_pipeline tests
python3 -m unittest discover -s tests
git diff --check
docker build -f Dockerfile.linkaja -t linkaja-fee-pipeline:3.11 .
```

The PostgreSQL integration tests require a dedicated disposable
`LINKAJA_TEST_DSN`; they truncate LinkAja-owned test tables.

## Kestra API Trigger Guide

LinkAja flows are manual. They have no schedule trigger and are executed through
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

Only `SUCCESS` is successful. Inspect task states and logs for other terminal
states.

### Daily flow

Flow:

```text
finance.linkaja.linkaja_fee_pipeline_v1
```

Inputs:

| Input | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `source_file` | FILE | yes | - | One LinkAja CSV export for one cluster. |
| `dry_run` | BOOLEAN | no | `false` | Validate and normalize without PostgreSQL persistence. |

Filename contract:

```text
laporan-<cluster_id>-....csv
```

Trigger one CSV:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "files=@/absolute/path/laporan-411311-july.csv;filename=source_file" \
  -F "dry_run=false" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.linkaja/linkaja_fee_pipeline_v1"
```

The daily flow validates the CSV, writes normalized artifacts, applies pending
schema migrations, replaces affected current transaction IDs, refreshes daily
fee/detail facts, and records append-only source evidence after the versioned
source migration is deployed. Submit chronological source generations one at a
time when replacement order matters.

### Monthly flow

Flow:

```text
finance.linkaja.linkaja_monthly_materialization_v1
```

Current inputs:

| Input | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `cluster_id` | STRING | yes | - | LinkAja cluster. |
| `report_month` | STRING | yes | - | `YYYY-MM` or `YYYY-MM-01`. |
| `calculation_cutoff` | STRING | yes | - | Exclusive local Asia/Makassar cutoff, on or after the next month. |
| `publish` | BOOLEAN | no | `false` | Preview when false; replace one cluster-month snapshot when true. |

Preview a month:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "cluster_id=411311" \
  -F "report_month=2026-07" \
  -F "calculation_cutoff=2026-08-10T00:00:00" \
  -F "publish=false" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.linkaja/linkaja_monthly_materialization_v1"
```

Review these artifacts before publication:

```text
linkaja_monthly_result.json
linkaja_daily_raw_calculation.csv
linkaja_unresolved_reversals.csv
```

Publish only after source coverage, unresolved reversals, fee completeness, and
Finance approval have been reviewed:

```bash
curl --fail-with-body --silent --show-error \
  --user "$KESTRA_USER:$KESTRA_PASSWORD" \
  -X POST \
  -F "cluster_id=411311" \
  -F "report_month=2026-07" \
  -F "calculation_cutoff=2026-08-10T00:00:00" \
  -F "publish=true" \
  "$KESTRA_URL/api/v1/$KESTRA_TENANT/executions/finance.linkaja/linkaja_monthly_materialization_v1"
```

The current monthly flow publishes database analysis artifacts only. The
hardened plan adds explicit evidence cutoffs, source expectations, unresolved
waivers, frozen reversal edges, and a `FINAL_WITH_WAIVERS` state before this
flow is treated as an authoritative Odoo settlement.
