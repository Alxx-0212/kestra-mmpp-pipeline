# LinkAja Production Database Separation Plan

## Status

**Target cutover is staged in this worktree; it is not deployed.** No production
database migration, workflow update, or write was performed during review.

## Verified Production Configuration (2026-11-24)

- The SSH `prod` profile reached `~/projects/kestra-mmpp-pipeline`.
- Docker metadata showed PostgreSQL 18.4 and Kestra 1.3.26 under Compose project
  `kestra-mmpp-local-20261020`; a transient FinPay task used a `dev`-tagged image.
  Confirm this is the intended production environment before writes.
- Remote daily and monthly LinkAja workflow sources use `FINPAY_DB_*`, and their
  DSN helper defaults to `FINPAY_DB_`.
- A sanitized check of the remote encoded Kestra environment matched
  `SECRET_FINPAY_DB_NAME` to `finpay`; no `SECRET_LINKAJA_DB_NAME` key was found.
  Secret values were not printed or copied.
- Compose defines one PostgreSQL service and LinkAja database/role provisioning
  inputs (`LINKAJA_DB_NAME`, `LINKAJA_DB_USER`, `linkaja_db_password`).

The checked-in production source and encoded DB-name setting therefore direct
LinkAja to `finpay`; the registered Kestra revision was not fetched. The live
database catalog, presence of the `linkaja` database, LinkAja relation
inventory, schema version, row counts, and backup/restore state remain
**unverified**. The SSH policy denied `docker exec`, and host `psql` is not
installed.

## Target Architecture

Keep one PostgreSQL instance and existing Postgres volume. FinPay remains in
`finpay`; LinkAja targets a separate logical database named `linkaja`. The current
worktree's daily and monthly workflows use `LINKAJA_DB_*`; deploying that change
will move the LinkAja workflow off `finpay`. Schema migrations create tables only
inside the database selected by the workflow DSN.

This preserves instance-level resources but not cross-database data continuity.
Per the latest direction, the initial rollout provisions the LinkAja schema only;
it does not copy rows from `finpay`. Existing LinkAja history, if present in
`finpay`, remains there, and `linkaja` reporting starts with post-cutover loads.
The live catalog is unverified. If historical continuity is required instead,
stop and approve a separate transfer/replay plan before switching the writer.

## Production Preflight (Read-Only)

Before switching the workflow, query the database selected by the **currently
active** LinkAja flow using its existing `FINPAY_DB_*` mapping. After the
`LINKAJA_DB_*` target is provisioned, run the same check against that target.
Do not infer either database from Compose or `.env.example` defaults.

```sql
BEGIN READ ONLY;
SELECT current_database(), current_user, current_setting('server_version');
SELECT to_regclass('public.linkaja_schema_migrations') AS migrations_table,
       to_regclass('public.linkaja_raw_transactions') AS raw_table,
       to_regclass('public.linkaja_transactions_current_v') AS current_view;
ROLLBACK;
```

If the migration table exists, separately read its latest version and bounded
aggregate row counts. Production catalog checks are still pending because the
SSH policy denied container execution and host `psql` is unavailable. Do not
claim the `linkaja` database exists or contains data until these checks are
recorded.

## Cutover Plan

1. Confirm the SSH host and Compose project are the intended production
   environment. The observed project name contains `local`, and a `dev`-tagged
   FinPay task container was present.
2. Run the read-only preflight against the active `FINPAY_DB_*` connection.
   Confirm the current database identity, LinkAja relations, migration version,
   and aggregate row counts.
3. Treat this rollout as schema-only/fresh-start per the current direction. Record
   the cutover date and history boundary; do not claim prior `finpay` reports are
   present in `linkaja`.
4. Take an external, checksum-verified backup and test its restore before any
   production data or role change.
5. If `linkaja` and its role already exist, do not run the shared provisioner;
   apply only the LinkAja schema migrations after the target is verified. The
   current Compose project has no LinkAja-only provisioner. If the database or
   role is missing, obtain a scoped DBA operation or add and review a dedicated
   provisioner. Do not run `finpay-db-provision` for a LinkAja-only change: it
   reapplies passwords and ownership for FinPay and other databases too.
6. Configure `LINKAJA_DB_*` Kestra secrets. With the env provider, these are
   base64 `SECRET_LINKAJA_DB_*` entries in protected `.env_encoded`; the
   inspected production file has no `SECRET_LINKAJA_DB_NAME` entry. The Kestra
   password must match the Docker secret used by the provisioner.
7. No history transfer is included in this schema-only rollout. If that scope
   changes, inventory exact LinkAja relations, dependencies, owners, grants, and
   sequences; take a custom-format backup and test a reviewed restore/replay in
   a disposable clone. Do not use the former wildcard `pg_dump | psql` example;
   it was not tested and is removed.
8. Build/publish the reviewed image and update only the LinkAja flow definitions.
   The Compose `finpay-reporting-flow-deploy` service updates both FinPay and
   LinkAja namespaces; do not use it for a LinkAja-only release unless the
   FinPay flow update is also intended.
9. Run a dry-run canary and then one approved database/Sheets canary. Compare
   schema version, source-load/current row counts, dates, and fee totals. Keep
   the active FinPay-targeted flow available until the canary passes.

## Current Worktree Status

- The worktree DSN helper and daily/monthly flows use `LINKAJA_DB_*`.
- Local Kestra validation, unit/schema tests, compilation, diff checks, and the
  runtime image build passed in the previous implementation turn.
- The production `.env_encoded` file was inspected only through sanitized
  presence/value matching; no secret bytes were printed or changed.
- Production database contents, migrations, backup, target database existence,
  LinkAja row counts, secret provisioning, flow registration, and cutover have
  **not** been changed or verified. No production SQL query, migration, or data
  write was executed.

## Rollback

Keep the current production LinkAja flow and `FINPAY_DB_*` configuration intact
until the target database is populated and verified. If the canary fails, keep
the production flow on its previous revision; do not remove or overwrite the
source database. Any rollback that modifies database contents requires a
current approved restore plan.
