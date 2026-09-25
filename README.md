# MMPP Finance Monorepo

MMPP packages independent FinPay reporting, FinPay Top-Up, Telegram, and
LinkAja runtimes on shared local infrastructure. Root files are global
configuration, orchestration, validation, and agent tooling; project code lives
under its owning directory.

## Projects

| Project | Workflow(s) | Guide |
|---|---|---|
| FinPay reporting | `finance.finpay.finpay_daily_pipeline_v5` | [`finpay_pipeline/README.md`](finpay_pipeline/README.md) |
| FinPay Top-Up | `finance.finpay.finpay_topup_pipeline_v1` | [`finpay_topup_pipeline/README.md`](finpay_topup_pipeline/README.md) |
| Telegram | Bot interface for FinPay | [`services/telegram_bot/README.md`](services/telegram_bot/README.md) |
| LinkAja | `finance.linkaja.linkaja_fee_pipeline_v1`, `finance.linkaja.linkaja_monthly_materialization_v1` | [`linkaja_fee_pipeline/README.md`](linkaja_fee_pipeline/README.md) |

## Shared Runtime

`docker-compose.yml` provides PostgreSQL 18.4, idempotent database and Top-Up
flow bootstrap, Kestra, the Telegram bot, ngrok, webhook registration, and
shared secrets/network configuration. Redis is disabled by default and belongs
to the optional dashboard profile.

- Kestra: `http://localhost:8081`
- Telegram health: `http://localhost:8096/healthz`
- PostgreSQL: `localhost:5433` from the host, `postgres:5432` in Compose
- Network: `mmpp-finance-network`
- Optional dashboards: `docker compose --profile optional-dashboard up -d superset superset-mcp grist`

Long-running Compose services use `restart: unless-stopped` so Docker restarts
them after a process exit or host reboot. One-shot setup/deploy tasks deliberately
use `restart: "no"`. Host reboot recovery requires Docker Engine itself to start
at boot; see [runtime architecture](docs/architecture.md#container-restart-behavior).

Superset, Superset MCP, and Grist are disabled by default. The Grist guide is
historical prototype documentation; the optional Superset profile is read-only.

## Workflow Boundary

- FinPay reporting accepts `csv_file` and `dry_run`; its active v5 flow has no Kestra trigger or schedule and is invoked by an operator/API client such as the legacy Hermes integration.
- FinPay Top-Up owns checkpoint-driven extraction, staging, hash deduplication, CMS verification, review, manual adjustments, and Excel-ready reporting.
- Telegram calls domain stores and services; it does not own financial calculations or write ledger rows directly.
- LinkAja owns its migrations, daily/monthly calculations, reversals, and snapshots. Its dbt project is learning-only.
- `FINPAY_DB_*` names and all workflow IDs, task IDs/imports, image names, database relations, sheet layouts, formulas, and artifact names are public contracts.

## Build

Run from the repository root so Docker `COPY` paths stay stable:

Each Dockerfile uses a builder/runtime multi-stage build; the final image does
not contain the builder's package manager caches or build toolchain.

```bash
docker build -f finpay_pipeline/Dockerfile -t finpay-pipeline:3.11 .
docker build -f finpay_topup_pipeline/Dockerfile -t finpay-topup-pipeline:3.11 .
docker build -f services/telegram_bot/Dockerfile -t mmpp-telegram-bot:local .
docker build -f linkaja_fee_pipeline/Dockerfile -t linkaja-fee-pipeline:3.11 .
```

## Validation

Read [`docs/validation.md`](docs/validation.md) for commands and database
guards. The pinned Kestra contract file is
[`config/kestra-validation.yml`](config/kestra-validation.yml). Verify
`.dockerignore` before image builds and run domain tests inside the owning
Python 3.11 image.

## Agent Guidance

- [`AGENTS.md`](AGENTS.md): universal boundaries and lanes
- [`INSTRUCTIONS.md`](INSTRUCTIONS.md): task preflight
- [`docs/architecture.md`](docs/architecture.md): project ownership and runtime topology
- [`docs/dependencies.md`](docs/dependencies.md): manifests and images
- [`docs/security.md`](docs/security.md): secrets and data safety
- [`docs/agent-lanes.md`](docs/agent-lanes.md): concurrent editing rules
- [`scripts/README.md`](scripts/README.md): optional worktree launcher

## Local State

`.env`, `.env_encoded`, `secrets/`, `data/`, `tmp/`, and generated caches are
local runtime state. They are excluded from images and must not be committed.
Create external backups before live database reset or repair.
