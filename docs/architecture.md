# MMPP Monorepo Architecture

## Runtime map

| Project | Owned code and workflow | Runtime |
|---|---|---|
| FinPay reporting | `finpay_pipeline/**` | `finpay-pipeline:3.11`; `finance.finpay.finpay_daily_pipeline_v5` |
| FinPay Top-Up | `finpay_topup_pipeline/*.py`, `workflows/**`, `tests/**` | `finpay-topup-pipeline:3.11`; `finance.finpay.finpay_topup_pipeline_v1` |
| Telegram | `services/telegram_bot/**` | `telegram-bot`; scoped commands, review callbacks, and Excel export |
| LinkAja | `linkaja_fee_pipeline/**` | `linkaja-fee-pipeline:3.11`; daily/monthly LinkAja flows |

## Shared infrastructure

`docker-compose.yml` owns the `mmpp-finance-network` network, PostgreSQL 18.4,
Kestra, optional Redis/Superset/Grist, ngrok, webhook registration, secret bootstrap,
database provisioning, and Top-Up flow deployment. A change to shared infrastructure is
integrator-owned and requires both-domain review.

PostgreSQL databases are separate logical contracts:

- `kestra`: Kestra metadata and executions.
- `finpay`: FinPay reporting, Top-Up ledger, classifications, checkpoints, and audit.
- `finpay_txn`: separate FinPay Transaction processing.
- `linkaja`: provisioned LinkAja database; verify the workflow DSN before assuming it is the active production target.

Superset is the current read-only dashboard/MCP runtime, but Superset,
Superset MCP, and Grist are disabled by the `optional-dashboard` Compose profile
until explicitly enabled. The Grist setup guide is historical prototype
documentation only and is not an active refresh path.

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
