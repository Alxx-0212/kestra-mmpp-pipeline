# MMPP Financial Pipelines

Kestra-managed financial data pipelines for FinPay and LinkAja. Both domains
share local infrastructure, but keep their ingestion, calculations, database
relations, reports, tests, and release behavior separate.

Runtime timestamps use `Asia/Makassar` unless a workflow explicitly states
otherwise.

## Domain map

| Domain | Daily workflow | Monthly workflow | Runtime guide |
|---|---|---|---|
| FinPay | `finpay_pipeline.yml` | — | [`finpay_pipeline/README.md`](finpay_pipeline/README.md) |
| LinkAja | `linkaja_fee_pipeline.yml` | `linkaja_monthly_materialization.yml` | [`linkaja_fee_pipeline/README.md`](linkaja_fee_pipeline/README.md) |

The domains use separate runtime images and workflows:

- FinPay uses `finpay-pipeline:3.11` and imports through `pipeline.py`.
- LinkAja uses `linkaja-fee-pipeline:3.11` and imports through
  `linkaja_pipeline.py`.
- LinkAja includes a learning-only dbt project under
  [`linkaja_fee_pipeline/dbt/`](linkaja_fee_pipeline/dbt/README.md). It is not
  part of the production Kestra path yet.

The current local/on-premises layout still shares the Compose network,
PostgreSQL service, and some `FINPAY_DB_*` secret names. Treat changes to that
shared runtime as integrator-owned even when only one domain consumes them.

## Repository structure

```text
kestra-mmpp-pipeline/
├── AGENTS.md
├── README.md
├── .dockerignore
├── .codex/
│   ├── config.toml
│   ├── agents/
│   └── prompts/
├── scripts/
│   └── codex-lanes
├── config/
│   └── kestra-validation.yml
├── finpay_pipeline.yml
├── linkaja_fee_pipeline.yml
├── linkaja_monthly_materialization.yml
├── pipeline.py
├── pipeline_refactored.py
├── linkaja_pipeline.py
├── finpay_pipeline/
│   ├── AGENTS.md
│   ├── README.md
│   ├── queries/
│   └── *.py
├── linkaja_fee_pipeline/
│   ├── AGENTS.md
│   ├── README.md
│   ├── dbt/
│   ├── migrations/
│   ├── queries/
│   └── *.py
├── tests/
├── Dockerfile
├── Dockerfile.linkaja
├── docker-compose.yml
├── requirements.txt
├── requirements-linkaja.txt
├── .env.example
└── .env_encoded.example
```

Domain-specific SQL no longer lives in a shared root directory:

- FinPay operational checks: `finpay_pipeline/queries/`
- LinkAja operational reports: `linkaja_fee_pipeline/queries/`

## Parallel Codex development

Use the checked-in launcher with one clean integration checkout and two sibling
Git worktrees. Each Codex TUI receives a separate filesystem, branch, and tmux
window, so FinPay and LinkAja changes cannot overwrite one another's working
files. Root/shared files remain in the integration lane.

The stable branch and worktree layout is:

| Lane | Branch | Default checkout |
|---|---|---|
| Integrator | `integration/mmpp-next` | this repository directory |
| FinPay | `agent/finpay-next` | sibling `kestra-mmpp-pipeline-finpay` |
| LinkAja | `agent/linkaja-next` | sibling `kestra-mmpp-pipeline-linkaja` |

Create the domain worktrees only from a clean integration checkout:

```bash
scripts/codex-lanes init
scripts/codex-lanes status
```

To preserve the plan and chat context from an existing LinkAja Codex session,
either open the all-project picker or supply its session UUID directly:

```bash
scripts/codex-lanes start --linkaja-picker
# or:
scripts/codex-lanes start --linkaja-fork <SESSION_ID>
tmux attach -t mmpp-agents
```

Forking leaves the original conversation intact and creates a continuation in
the isolated checkout. The picker deliberately includes sessions from other
working directories so it can find the former LinkAja checkout. After choosing
the conversation, send one message confirming that it should continue from
`linkaja_fee_pipeline/LINKAJA_REVERSAL_IMPLEMENTATION_PLAN.md` on
`agent/linkaja-next`. Do not commit the session UUID. If prior context is not
needed, opt out explicitly with `--linkaja-fresh`.

The launcher runs every Codex TUI with `--no-alt-screen`, enables tmux mouse
support, and gives each new pane 50,000 lines of history. After restarting the
managed session, the mouse wheel scrolls tmux history instead of searching only
the current Codex view. Keyboard navigation remains available: press `Ctrl-b [`
to enter copy mode, use `PageUp`, `PageDown`, `Ctrl-u`, or `Ctrl-d`, and press
`q` to exit. Set `CODEX_LANES_TMUX_HISTORY_LIMIT` to a positive integer before
`start` if a different limit is needed. These settings apply only to the
launcher-managed tmux session; they do not modify the user's global tmux
configuration.

Before handing a domain branch to the integrator, enforce its file boundary:

```bash
scripts/codex-lanes check finpay
scripts/codex-lanes check linkaja
```

The launcher never merges, cherry-picks, deploys, runs migrations, or deletes
worktrees. `stop` only terminates its tmux session:

```bash
scripts/codex-lanes stop
```

The project-scoped `.codex/agents/` roles are available to a parent Codex
session for bounded subagent review. They do not replace the worktree boundary;
the three tmux windows are independent top-level Codex sessions. Compose,
Docker, live Kestra, database, Sheets, and Telegram operations belong to the
integrator lane.

## Local setup

### Prerequisites

- Docker and Docker Compose
- A GCP service account with Google Sheets and Google Drive access
- Spreadsheet sharing appropriate to the configured service account
- Local environment and Kestra secret values

### Environment

Copy the safe templates and fill in local-only values:

```bash
cp .env.example .env
cp .env_encoded.example .env_encoded
```

`.env` contains Docker Compose settings. `.env_encoded` contains base64 values
exposed to Kestra with the `SECRET_` prefix. Both real files are ignored by
Git.

Create a base64 value with:

```bash
printf '%s' 'real-secret-value' | base64 -w0
```

Never commit credentials, service-account JSON, spreadsheet IDs, private email
addresses, or database passwords.

### Start services

```bash
docker compose up -d
```

Default local endpoints:

| Service | Address |
|---|---|
| Kestra | `http://localhost:8081` |
| pgAdmin | `http://localhost:5050` |
| PostgreSQL from host | `localhost:5433` |

Inside the Compose network, PostgreSQL is available as `finpay-postgres:5432`.

### Build runtime images

```bash
docker build -t finpay-pipeline:3.11 .
docker build -f Dockerfile.linkaja -t linkaja-fee-pipeline:3.11 .
```

Rebuild the relevant image whenever its Python code, Dockerfile, or dependency
file changes. The root `.dockerignore` keeps local financial exports, real
environment files, credentials, caches, tests, and generated artifacts out of
the Docker build context. Preserve those exclusions when adding new local data
or secret paths.

## Workflow behavior

### Current orchestration boundary

The workflows in this repository process files supplied to Kestra. The current
FinPay flow has no Kestra trigger or schedule; an operator or API client must
start it and provide the input file. Website acquisition, Hermes agents, and the
existing external scrape cron are not implemented in this repository. Keep that
acquisition boundary separate until an immutable file and metadata handoff has
been designed and tested.

### FinPay

The manual flow `finance.finpay.finpay_daily_pipeline_v5` declares a required
`csv_file` (`CSV` or `XLSX`) plus optional `dry_run`. The filename selects
one of six configured clusters and supplies the report date. The workflow
validates and classifies rows, creates nine persisted Parquet task artifacts,
writes five date-scoped PostgreSQL tables, and publishes summary, QRISDUWIT,
Reversal, and Unusual Google Sheets outputs when `dry_run=false`. Its
compatibility import surface remains `from pipeline import ...`.

The FinPay invoice report intentionally reads fee values from the shared
`LinkAja` worksheet. FinPay creates or repairs the stable reference headers
before appending invoice formulas, because Google Sheets formulas fail when the
referenced worksheet does not yet exist. LinkAja calculations remain owned by
the LinkAja domain.

See [`finpay_pipeline/README.md`](finpay_pipeline/README.md) for transaction
labels, fee rules, table grains, sheet layouts, and rerun behavior.

### LinkAja

The LinkAja daily workflow accepts one cluster CSV, normalizes ledger rows,
atomically replaces affected transaction identities, calculates current facts
through PostgreSQL views, and stores a downloadable Kestra result artifact. It
has no Google Sheets side effect and does not rewrite the frozen monthly table.

The manual monthly workflow previews or publishes one cluster-month snapshot
at an explicit exclusive local cutoff. The canonical PostgreSQL summary view
calculates Digipos from eligible `Fee = 200` transactions and PPOB from its
source Fee; published snapshots and live unresolved reversals are reported using
[`linkaja_fee_pipeline/queries/linkaja_monthly_fee_summary.sql`](linkaja_fee_pipeline/queries/linkaja_monthly_fee_summary.sql).
Late-data refresh candidates are listed by
[`linkaja_fee_pipeline/queries/linkaja_monthly_refresh_candidates.sql`](linkaja_fee_pipeline/queries/linkaja_monthly_refresh_candidates.sql).

See [`linkaja_fee_pipeline/README.md`](linkaja_fee_pipeline/README.md) for
ledger grain, reversal behavior, monthly fees, migrations, and operating
procedures.

See [`linkaja_fee_pipeline/TAIPY_DASHBOARD_PLAN.md`](linkaja_fee_pipeline/TAIPY_DASHBOARD_PLAN.md)
for the approved Sheet-free Taipy boundary, report contracts, schema
assessment, and delivery sequence.

## Documentation ownership

| Document | Purpose |
|---|---|
| `README.md` | Repository setup and domain navigation |
| `AGENTS.md` | Repository boundaries and shared editing rules |
| `finpay_pipeline/README.md` | Canonical FinPay technical/operational guide |
| `finpay_pipeline/AGENTS.md` | Concise FinPay editing invariants |
| `linkaja_fee_pipeline/README.md` | Canonical LinkAja technical/operational guide |
| `linkaja_fee_pipeline/TAIPY_DASHBOARD_PLAN.md` | LinkAja Taipy architecture and reporting contract |
| `linkaja_fee_pipeline/AGENTS.md` | Concise LinkAja editing invariants |
| `linkaja_fee_pipeline/dbt/README.md` | LinkAja dbt learning setup and current limitations |

Keep business rules in the owning domain guide. The root README should remain
an index and shared setup guide rather than duplicate domain calculations.

## Development checks

Run the root static contracts on any host Python, then run behavior tests in a
fully provisioned Python 3.11 environment. Missing dependencies and skipped
FinPay tests do not count as a passing result:

```bash
python3 -m unittest -v \
  tests.test_repository_setup_contracts \
  tests.test_finpay_workflow_contracts \
  tests.test_codex_lanes
docker run --rm \
  -e PYTHONPYCACHEPREFIX=/tmp/finpay-pycache \
  -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 \
  python -m unittest -v tests.test_refactor_contracts
git diff --check
git diff --cached --check
docker compose config --quiet
```

Validate changed workflow YAML against the same Kestra version deployed
on-premises. A generic YAML parse is only a syntax check and does not validate
task plugins, templates, or task properties. The current local FinPay check for
the Compose-pinned Kestra version is:

```bash
docker run --rm \
  -v "$PWD/finpay_pipeline.yml:/flows/finpay_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml
```

The validation configuration uses only an in-memory H2 repository. It is not a
production Kestra configuration. Before release, also validate server-side
against the actual on-premises instance and run the full repository suite in a
Python 3.11 environment containing both domains' dependencies.

LinkAja PostgreSQL integration tests require `LINKAJA_TEST_DSN` pointing to a
dedicated disposable database. Never point those tests at production or a
shared development database because the integration setup truncates
LinkAja-owned test tables.
