# MMPP Finance Monorepo

MMPP packages independent FinPay reporting, FinPay Top-Up, Telegram, and
LinkAja runtimes on shared local infrastructure. Root files are global
configuration, orchestration, validation, and agent tooling; project code lives
under its owning directory.

## Projects

| Project | Workflow(s) | Guide |
|---|---|---|
| FinPay reporting | `finance.finpay.finpay_digipos_acquisition_v1`, `finance.finpay.finpay_daily_pipeline_v5`, `finance.finpay.finpay_monthly_materialization_v1` | [`finpay_pipeline/README.md`](finpay_pipeline/README.md) |
| FinPay Top-Up | `finance.finpay.finpay_topup_pipeline_v1` | [`finpay_topup_pipeline/README.md`](finpay_topup_pipeline/README.md) |
| Telegram | Bot interface for FinPay | [`services/telegram_bot/README.md`](services/telegram_bot/README.md) |
| LinkAja | `finance.linkaja.linkaja_fee_pipeline_v1`, `finance.linkaja.linkaja_monthly_materialization_v1` | [`linkaja_fee_pipeline/README.md`](linkaja_fee_pipeline/README.md) |

## Shared Runtime

`docker-compose.yml` provides PostgreSQL 18.4, idempotent database provisioning,
Kestra, and an explicit one-shot deploy task for FinPay reporting and LinkAja
flows. Top-Up, its Telegram/ngrok runtime, and dashboard services are opt-in
profiles and do not start in the default deployment.

- Kestra: `http://localhost:18081` by default (`KESTRA_HOST_PORT`), loopback-bound
- PostgreSQL: `localhost:15433` by default (`POSTGRES_HOST_PORT`), loopback-bound
- Finance task network: `<COMPOSE_PROJECT_NAME>_mmpp-finance-network`
- Deploy reporting/LinkAja YAMLs after core startup: `docker compose --profile flow-deploy run --rm finpay-reporting-flow-deploy`
- Top-Up/Telegram/ngrok is excluded from this deployment; its legacy profile needs network review before use.
- Optional dashboards: `docker compose --profile optional-dashboard up -d`

Copy `.env.example` to `.env` and set a unique `COMPOSE_PROJECT_NAME` for each
coexisting deployment. Docker resources are daemon-global, not Linux-user
scoped. Compose named volumes and the finance network are project-scoped, and
host ports are configurable to avoid the existing production stack.

Set `KESTRA_TASK_WORKDIR` to an absolute host path (default
`${PWD}/tmp/kestra-wd`). Compose mounts that same path inside Kestra so sibling
Docker tasks using `MOUNT` can see their scripts and input files. The pinned
Kestra `v1.3.26` configuration explicitly enables Basic auth so its configured
account is initialized on a fresh metadata database.

Long-running Compose services use `restart: unless-stopped` so Docker restarts
them after a process exit or host reboot. One-shot setup/deploy tasks deliberately
use `restart: "no"`. Host reboot recovery requires Docker Engine itself to start
at boot; see [runtime architecture](docs/architecture.md#container-restart-behavior).

Superset, Superset MCP, and Grist are disabled by default. The Grist guide is
historical prototype documentation; the optional Superset profile is read-only.

## Workflow Boundary

- FinPay reporting uses `finpay_digipos_acquisition_v1` to scrape DigiPOS then call `finpay_daily_pipeline_v5`. The v5 flow accepts `csv_file` and `dry_run`, has no Kestra trigger or schedule of its own, and remains callable through the manual/API path, including the legacy Hermes integration. The acquisition parent's 05:00 Asia/Makassar schedule is disabled until production cutover.
- FinPay Top-Up owns checkpoint-driven extraction, staging, hash deduplication, CMS verification, review, manual adjustments, and Excel-ready reporting.
- Telegram calls domain stores and services; it does not own financial calculations or write ledger rows directly.
- LinkAja owns its migrations, daily/monthly calculations, reversals, and snapshots. Its dbt project is learning-only.
- `FINPAY_DB_*` names and all workflow IDs, task IDs/imports, image names, database relations, sheet layouts, formulas, and artifact names are public contracts.

## Build

Run from the repository root so Docker `COPY` paths stay stable:

The FinPay, LinkAja, Top-Up, and Telegram runtime Dockerfiles use builder/runtime
multi-stage builds. The DigiPOS scraper image intentionally extends the pinned
Playwright browser image so Chromium is bundled in the task runtime and does not
download at execution time.

```bash
docker build -f finpay_pipeline/Dockerfile -t finpay-pipeline:3.11 .
docker build -f linkaja_fee_pipeline/Dockerfile -t linkaja-fee-pipeline:3.11 .
docker build -f finpay_pipeline/Dockerfile.digipos-scraper -t finpay-digipos-scraper:1.0 .
```

For production CI/CD, run domain tests and pinned Kestra validation, then push
immutable image tags (release version plus commit SHA) to the approved registry.
Update Kestra workflow YAMLs to those tags. The Compose flow-deploy job updates
flow definitions only; it does not build or publish task images.

Build Top-Up/Telegram images only when enabling the `finpay-topup` profile.

The Compose `finpay-reporting-flow-deploy` one-shot synchronizes FinPay reporting
and LinkAja workflow YAMLs from their package `workflows/` directories. Run it
explicitly after the core stack is healthy; it updates definitions but launches
no workflow executions. Top-Up flow deployment is profile-gated and excluded.

## Production Cutover

Running old and new service copies concurrently is normal during a canary, but
they must use distinct `COMPOSE_PROJECT_NAME` values and non-conflicting host
ports. That isolates named volumes and networks. It does **not** synchronize
PostgreSQL data: a new project gets a separate database volume. Restore/migrate
the database deliberately or use a separately managed shared PostgreSQL service
before switching production traffic.

For this internal service, a simple manual CD on the deployment host is
appropriate: check out the exact merged commit, keep `.env`, `.env_encoded`, and
`secrets/` local, build the pinned images, validate Compose, start core services,
and explicitly deploy the flow definitions:

```bash
docker compose config --quiet
docker build -f finpay_pipeline/Dockerfile -t finpay-pipeline:3.11 .
docker build -f linkaja_fee_pipeline/Dockerfile -t linkaja-fee-pipeline:3.11 .
docker build -f finpay_pipeline/Dockerfile.digipos-scraper -t finpay-digipos-scraper:1.0 .
docker compose up -d
docker compose --profile flow-deploy run --rm finpay-reporting-flow-deploy
docker compose ps
```

CI should run unit/contract tests, Compose validation, and Kestra flow validation
before merge. Use immutable image tags (release version plus commit SHA) if
images are built/published by CI; local builds are fine for an internal system
if the deployment log records the exact Git commit and image IDs. Keep the
acquisition schedule disabled during canary. During cutover, stop the old writer
before enabling the new one; two active stacks must not write the same
cluster/date or Google worksheet.

Keep the new acquisition schedule disabled during validation. Before cutover,
back up and migrate PostgreSQL, deploy immutable image tags, run one daily
canary, verify DB/Sheet parity, disable the old writer/schedule, switch the
reverse proxy/traffic to the new Kestra endpoint, then enable exactly one active
daily schedule. Do not let both stacks write the same cluster/date or worksheet
simultaneously. Retain the old stack and backup through the rollback window.

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
