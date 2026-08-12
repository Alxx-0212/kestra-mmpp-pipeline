# LinkAja dbt Learning Project

This is a minimal dbt Core project for learning against the existing LinkAja
PostgreSQL model. It is deliberately not connected to Kestra and does not
replace Python ingestion, schema migrations, reversal enrichment, or monthly
publication.

## What it currently does

```text
production PostgreSQL linkaja_transactions
    -> dbt source: linkaja.monthly_transactions
    -> view: stg_linkaja_monthly_transactions
    -> documentation and data tests
```

The first model is a read-only projection of the frozen monthly snapshot. The
included tests check required identifiers/statuses and the unique grain:

```text
report_month + cluster_id + transaction_id
```

The operational monthly calculation remains the production-owned PostgreSQL
view `linkaja_monthly_fee_summary_v`, reported by
`../queries/linkaja_monthly_fee_summary.sql`, until a future dbt mart produces
verified result parity. This dbt project does not create or replace that view.

## Install dbt separately

Use a dedicated virtual environment rather than adding dbt to either runtime
Docker image while this project is experimental:

```bash
python3 -m venv .venv-dbt
source .venv-dbt/bin/activate
python -m pip install dbt-core dbt-postgres
```

## Configure a local profile

From this directory:

```bash
cp profiles.example.yml profiles.yml
```

`profiles.yml` is ignored because it can contain credentials. The example uses
the same `FINPAY_DB_*` variables as the current LinkAja Python connection and
adds two dbt-specific options:

| Variable | Purpose |
|---|---|
| `LINKAJA_SOURCE_SCHEMA` | Schema containing production LinkAja relations; defaults to `public`. |
| `LINKAJA_DBT_SCHEMA` | Development schema where dbt may create models; defaults to `linkaja_dbt`. |

Point these variables only at a local or dedicated development database while
learning.

## Run the scaffold

```bash
dbt debug --project-dir . --profiles-dir .
dbt parse --project-dir . --profiles-dir .
dbt build --project-dir . --profiles-dir . --select tag:linkaja
```

`dbt build` creates `stg_linkaja_monthly_transactions` in the configured dbt
schema and runs its tests. It does not modify the production-owned source
relations.

## Suggested learning sequence

1. Run `dbt debug` and inspect the resolved development connection.
2. Build the staging view and browse it in pgAdmin.
3. Run `dbt docs generate` and inspect source-to-model lineage.
4. Recreate the monthly fee query as a separate dbt mart.
5. Compare every cluster/category result with the operational SQL.
6. Add Kestra integration only after parity, rerun, and cutoff behavior are
   explicitly tested.

Do not use a dbt snapshot as a drop-in replacement for the published monthly
snapshot. dbt snapshots track row history; the current release table encodes a
specific business month and exclusive reversal cutoff.
