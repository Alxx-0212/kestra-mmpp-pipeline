# MMPP Monorepo Architecture

## Runtime map

| Project | Owned code and workflow | Runtime |
|---|---|---|
| FinPay reporting | `finpay_pipeline/**` | `finpay-pipeline:3.11`; acquisition, daily v5, and monthly flows under `finpay_pipeline/workflows/` |
| FinPay Top-Up | `finpay_topup_pipeline/*.py`, `workflows/**`, `tests/**` | `finpay-topup-pipeline:3.11`; opt-in `finance.finpay.finpay_topup_pipeline_v1` |
| Telegram | `services/telegram_bot/**` | `telegram-bot`; scoped commands, review callbacks, and Excel export |
| LinkAja | `linkaja_fee_pipeline/**` | `linkaja-fee-pipeline:3.11`; daily/monthly LinkAja flows |

## Shared infrastructure

`docker-compose.yml` owns a project-scoped `<COMPOSE_PROJECT_NAME>_mmpp-finance-network`,
PostgreSQL 18.4, Kestra, shared database provisioning, and idempotent deployment
of FinPay reporting and LinkAja workflows. FinPay flow YAMLs are mounted from
`finpay_pipeline/workflows/`. Top-Up/Telegram/ngrok requires the `finpay-topup`
profile; Redis/Superset/Superset MCP/Grist requires `optional-dashboard`. A
change to shared infrastructure is integrator-owned and requires both-domain
review.

Compose project names, named volumes, and networks are Docker-daemon global,
not Linux-user scoped. A separate Linux login alone does not isolate services.
Set a unique `COMPOSE_PROJECT_NAME` and distinct host ports for each coexisting
stack. The new project receives a separate PostgreSQL volume; data does not
follow a reverse-proxy/port cutover automatically. Restore/migrate it deliberately
or point to a separately managed shared database.

`KESTRA_TASK_WORKDIR` is an absolute path mounted identically on the Docker host
and Kestra container; the configured temporary task directory is its `tmp/`
child. This prevents missing-script/input errors with the Docker `MOUNT` file
handling strategy. Monthly Google writers inherit the project finance network
because they also audit published failures in PostgreSQL; allow Google HTTPS
egress on that network. DigiPOS scraping retains its explicit host-network egress.

## Deployment and cutover

Use CI to run domain tests, `docker compose config`, pinned Kestra flow
validation, and image builds. Publish immutable image tags (for example, a
release version plus commit SHA); avoid `latest` for production. Deploy the
isolated Compose project, run migrations and one dry-run/canary per platform,
verify database and Google Sheet parity, then switch the reverse proxy/traffic.
Keep the acquisition schedule disabled on the standby stack. Before enabling it
on the new active stack, stop/disable the old writer/schedule so two Kestra
instances cannot process the same date or overwrite the same sheet. Retain the
old services and a tested database backup through the rollback window.

PostgreSQL databases are separate logical contracts:

- `kestra`: Kestra metadata and executions.
- `finpay`: FinPay reporting, Top-Up ledger, classifications, checkpoints, and audit.
- `finpay_txn`: separate FinPay Transaction processing.
- `linkaja`: provisioned LinkAja database; verify the workflow DSN before assuming it is the active production target.

Superset is the current read-only dashboard/MCP runtime, but Superset,
Superset MCP, and Grist are disabled by the `optional-dashboard` Compose profile
until explicitly enabled. The Grist setup guide is historical prototype
documentation only and is not an active refresh path.

## Container restart behavior

Persistent services (`postgres`, `kestra`, Telegram bot, ngrok, and the optional
Redis/Superset/Superset MCP/Grist services) use `restart: unless-stopped`.
Compose one-shot initialization, provisioning, flow-deployment, and webhook
registration jobs use `restart: "no"`; they run through `depends_on` when the
stack is brought up and should not loop after completion.

The policy restarts a container after its process exits and brings it back when
the Docker daemon starts after a host reboot, unless an operator explicitly
stopped it. It does not restart a process that remains alive but only fails its
healthcheck. Host reboot recovery also requires Docker Engine to be enabled at
boot (for systemd Linux hosts, verify with `systemctl is-enabled docker`).

## Boundaries

- Keep calculations, schemas, migrations, queries, and operating docs in the owning project.
- Telegram calls domain stores/services but does not own financial calculations or database schema.
- FinPay may create/repair the shared `LinkAja` worksheet reference sheet; LinkAja calculations remain LinkAja-owned.
- The LinkAja dbt project is learning-only until production integration is explicitly approved.
- Preserve workflow IDs, task imports, image names, database relations, secret names, worksheet layouts, formulas, and artifact names.

## Project layout

Each runtime keeps implementation modules separate from orchestration:

```text
<project>/
  *.py           # Python/domain modules and package-local dependencies
  workflows/     # Kestra YAML only
  tests/         # project behavior and contract tests
  docs/          # project-specific operating notes/evidence
```

The current packages keep modules at the project root when a `modules/`
subdirectory would only add indirection; `workflows/` and `tests/` remain
separate. Root contains no runtime Dockerfile, workflow, compatibility facade,
or domain requirements manifest.

Read the closest guide before editing:

- [`finpay_pipeline/AGENTS.md`](../finpay_pipeline/AGENTS.md)
- [`finpay_topup_pipeline/AGENTS.md`](../finpay_topup_pipeline/AGENTS.md)
- [`services/telegram_bot/AGENTS.md`](../services/telegram_bot/AGENTS.md)
- [`linkaja_fee_pipeline/AGENTS.md`](../linkaja_fee_pipeline/AGENTS.md)
