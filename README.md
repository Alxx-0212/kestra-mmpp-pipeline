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
| FinPay Top Up | `finpay_topup_pipeline/finpay_topup_pipeline.yml` | — | [`finpay_topup_pipeline/README.md`](finpay_topup_pipeline/README.md) |
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

## Recent Infrastructure Changes

The local stack was restructured to a single shared finance network and a
multi-database PostgreSQL instance. Summary of the changes:

| Change | Before | After | Rationale |
|--------|--------|-------|-----------|
| Compose network | `finpay-network` | `mmpp-finance-network` | Shared, domain-agnostic network for all finance pipelines (FinPay + LinkAja). All Kestra Docker task `networkMode` values were updated to match. |
| Databases | `kestra`, `finpay`, `linkaja` | `kestra`, `finpay`, `finpay_txn`, `linkaja` | Added a dedicated `finpay_txn` database for FinPay Transaction processing, kept separate from the existing `finpay` (Top Up + reconciliation) database. |
| pgAdmin | `dpage/pgadmin4` service on :5050 | Removed | Use DataGrip (or `psql`) against `localhost:5433` instead. Reduces attack surface and image weight. |
| Superset MCP | Implicit / unverified | Explicit `superset mcp run --host 0.0.0.0 --port 5008` on its own `superset-mcp` service with a `405` healthcheck | Makes the MCP server a first-class, observable component. |
| Top-up refresh trigger | `finpay-topup-refresh` service on :8095 | Retained as deprecated fallback | The dashboard now triggers the Kestra flow directly via webhook/API. The refresh service remains for local convenience and will be removed once the direct integration is validated in production. |

> Note: The `finpay` database name is a public contract consumed by the
> `FINPAY_DB_NAME` Kestra secret used across `finpay_pipeline.yml`,
> `finpay_topup_pipeline.yml`, and the LinkAja workflows. It was intentionally
> **not** renamed to preserve those contracts; the new transaction database is
> `finpay_txn`.

## Architecture Overview

### System Topology

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                     Docker Compose Network                               │
│  mmpp-finance-network (bridge)                                           │
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                │
│  │   Kestra    │    │  Superset   │    │  PostgreSQL │                │
│  │  (8081)     │◄───│  (8088)     │◄───│  (5433)     │                │
│  │             │    │             │    │             │                │
│  │ Orchestration│    │ Visualization│    │  Databases  │                │
│  │   Engine    │    │  (BI Tool)  │    │             │                │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘                │
│         │                  │                   │                       │
│         │                  │   ┌───────────────┼───────────┐           │
│         │                  │   │               │           │           │
│         ▼                  ▼   ▼               ▼           ▼           │
│  ┌─────────────┐    ┌─────────────┐  ┌───────────┐ ┌─────────┐ ┌─────────┐│
│  │  Kestra DB  │    │  Superset   │  │   finpay  │ │finpay_txn│ │ linkaja ││
│  │ (metadata)  │    │  Datasets   │  │  database │ │ database │ │database ││
│  └─────────────┘    └─────────────┘  └───────────┘ └─────────┘ └─────────┘│
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ Superset MCP server (5008) — programmatic chart/dataset access   │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Database Topology (Single PostgreSQL Instance)

The architecture uses a **single PostgreSQL 18.4 container** hosting four separate databases:

| Database | Owner | Purpose | Access Pattern |
|----------|-------|---------|----------------|
| `kestra` | `kestra` | Kestra metadata (workflows, executions, queue) | Kestra internal |
| `finpay` | `finpay` | FinPay Top Up + reconciliation data, views, snapshots | Pipeline writes, Superset reads |
| `finpay_txn` | `finpay_txn` | FinPay Transaction processing data (separate from top-up) | Future pipeline writes, Superset reads |
| `linkaja` | `linkaja` | LinkAja fee pipeline data (future) | Pipeline writes, Superset reads |

**Initialization**: Databases and roles are created idempotently by `docker/initdb/00-create-databases.sh` during PostgreSQL container startup. The script reads credentials from environment variables (no secrets in the script).

### Data Flow Architecture

```text
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  DigiPOS     │────►│  Kestra      │────►│  PostgreSQL  │────►│  Superset    │
│  CMS (API)   │     │  Workflow    │     │  (finpay)    │     │  Dashboard   │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
       │                    │                    │                    │
       ▼                    ▼                    ▼                    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ CSV Export   │     │  Ingest      │     │  Views &     │     │  Charts &    │
│ per Cluster  │     │  Pipeline    │     │  Snapshots   │     │  Filters     │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
```

### FinPay Top Up Workflow (`finpay_topup_pipeline_v1`)

**Trigger**: Finance-triggered via protected `/refresh` endpoint (or Kestra webhook)
- **Input**: `start` date, `end` date (always ends on `Asia/Makassar` today), `users` (6 clusters)
- **Output**: Refreshed transaction data, updated SALDO, BUCKET TOP UP comparison

#### Workflow Steps

| Step ID | Type | Description | Key Files |
|---------|------|-------------|-----------|
| `ingest_topup` | Python Script | Download CSV per cluster, validate row counts, idempotent load | `extract.py`, `loader.py`, `schema.py` |
| `refresh_saldo` | Python Script | Compute running SALDO, verify vs DigiPOS BUCKET TOP UP, retry on mismatch | `saldo.py`, `verify.py`, `audit.py`, `bucket.py` |

#### Key Data Models (PostgreSQL Tables/Views)

| Name | Type | Purpose | Grain |
|------|------|---------|-------|
| `finpay_topup_txn` | Table | Immutable transaction facts | One row per transaction |
| `finpay_topup_classification` | Table | Finance-owned category tags | One row per transaction |
| `finpay_cluster_balance` | Table | Opening balance checkpoints | One row per cluster/date |
| `finpay_topup_refresh` | Table | Refresh audit header | One row per refresh |
| `finpay_topup_refresh_cluster` | Table | Per-cluster refresh details | One row per refresh+cluster |
| `finpay_bucket_topup_snapshot` | Table | DigiPOS BUCKET TOP UP snapshots | One row per cluster/date |
| `finpay_topup_saldo` | View | Transaction detail with running saldo | One row per transaction |
| `finpay_topup_daily` | View | Daily aggregates with ending saldo | One row per cluster/date |
| `finpay_topup_cluster_summary` | View | Per-cluster KPI summary | One row per cluster |
| `finpay_topup_reconciliation_daily` | View | **Daily reconciliation mart** (carry-forward SALDO + BUCKET comparison) | One row per cluster/date |
| `finpay_topup_bucket_compare` | View | Snapshot comparison (SALDO vs BUCKET) | One row per cluster/date |
| `finpay_topup_refresh_status` | View | Latest refresh status for dashboard | One row per latest refresh |
| `finpay_topup_bucket_compare` | View | Snapshot comparison for Superset | One row per cluster/date |

#### Running SALDO Calculation

The running SALDO is computed in `finpay_topup_saldo` view using a window function:
```sql
COALESCE(opening_balance, 0) + SUM(
    CASE transaction_type
        WHEN 'Kredit' THEN amount
        WHEN 'Debit' THEN -amount
    END
) OVER (PARTITION BY cluster_id, opening_as_of_date, opening_balance
        ORDER BY transaction_date, txn_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_saldo
```

**Opening balance** comes from `finpay_cluster_balance` (latest `as_of_date` <= transaction date).

#### BUCKET TOP UP Verification

After loading transactions, `refresh_saldo`:
1. Scrapes DigiPOS CMS `/home` page for each cluster's BUCKET TOP UP value
2. Computes end-of-day SALDO (before today if window includes today)
3. Compares computed SALDO vs BUCKET TOP UP (tolerance: 0.01)
4. On mismatch: re-downloads once, reloads idempotently, re-verifies
5. Records result in `finpay_topup_refresh_cluster` + `finpay_bucket_topup_snapshot`

#### Reconciliation Mart (`finpay_topup_reconciliation_daily`)

This is the **primary chart-ready dataset** for the Superset dashboard. It provides:
- **Continuous date spine** per cluster (no gaps on weekends/holidays)
- **Carried-forward SALDO**: `LAST_VALUE(...) IGNORE NULLS OVER (...)` carries forward last known ending_saldo across gaps
- **BUCKET TOP UP comparison**: Joins with `finpay_bucket_topup_snapshot` on `(cluster_id, report_date)`
- **One row per cluster per calendar date** from first transaction to latest

### Superset Dashboard Architecture

**Dashboard**: `FinPay Top Up Monitoring` (slug: `finpay-topup`)

| Chart | Type | Dataset | Purpose |
|-------|------|---------|---------|
| Header + Refresh Button | Markdown | — | Title + link to `/refresh` endpoint |
| Cluster KPI Cards | Table | `finpay_topup_reconciliation_daily` | 6 cards showing latest BUCKET TOP UP, SALDO, diff, status, timestamp per cluster |
| Running SALDO Trend | Line (xy) | `finpay_topup_reconciliation_daily` | `series=cluster_id`, x=report_date, y=ending_saldo |
| BUCKET vs SALDO Trend | Line (xy) | `finpay_topup_reconciliation_daily` | Two lines: bucket_topup_value vs ending_saldo |
| Reconciliation Table | Table | `finpay_topup_reconciliation_daily` | All columns, sorted by report_date DESC |

**Native Filter**: `cluster_id` (single-select, required=false) scoped to all charts.

**Refresh Button**: Markdown link to `http://localhost:8095/refresh` (protected endpoint). Clicking triggers Kestra workflow for all 6 clusters.

### Kestra Workflow Integration

| Component | Configuration |
|-----------|---------------|
| Flow ID | `finpay_topup_pipeline_v1` |
| Namespace | `finance.finpay` |
| Trigger | Webhook (`FINPAY_TOPUP_WEBHOOK_KEY`) or `/refresh` endpoint |
| Runner | Docker (`finpay-topup-pipeline:3.11`, network `mmpp-finance-network`) |
| Secrets | `FINPAY_DB_HOST`, `FINPAY_DB_PORT`, `FINPAY_DB_NAME`, `FINPAY_DB_USER`, `FINPAY_DB_PASSWORD`, `DIGIPOS_PASSWORD` |
| Network | `mmpp-finance-network` (shared with PostgreSQL) |

### Local Development Setup

```bash
# 1. Copy environment templates
cp .env.example .env
cp .env_encoded.example .env_encoded

# 2. Fill in real values in .env and .env_encoded (base64-encoded)
# Required secrets for .env_encoded:
# SECRET_FINPAY_DB_HOST=postgres
# SECRET_FINPAY_DB_PORT=5432
# SECRET_FINPAY_DB_NAME=finpay
# SECRET_FINPAY_DB_USER=finpay
# SECRET_FINPAY_DB_PASSWORD=<real-password>
# SECRET_DIGIPOS_PASSWORD=<digipos-password>
# SECRET_KESTRA_BASIC_AUTH_PASSWORD=<kestra-password>

# 2. Start all services
docker compose up -d

# 3. Build runtime images (first time or after code changes)
docker build -t finpay-pipeline:3.11 .
docker build -f Dockerfile.topup -t finpay-topup-pipeline:3.11 .

# 4. Access services
# Kestra:       http://localhost:8081
# Superset:     http://localhost:8088
# Refresh page: http://localhost:8095/refresh
# Superset MCP: http://localhost:5008/mcp
# PostgreSQL:   localhost:5433 (host) / postgres:5432 (internal)
```

### Testing & Validation

```bash
# Static contract checks (host Python)
python3 -m unittest -v \
  tests.test_repository_setup_contracts \
  tests.test_finpay_workflow_contracts \
  tests.test_codex_lanes

# Docker-based behavior tests (Python 3.11 environment)
docker run --rm \
  -e PYTHONPYCACHEPREFIX=/tmp/finpay-pycache \
  -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 \
  python -m unittest -v tests.test_refactor_contracts

# YAML syntax
git diff --check
docker compose config --quiet

# Kestra flow validation (uses in-memory H2)
docker run --rm \
  -v "$PWD/finpay_topup_pipeline/finpay_topup_pipeline.yml:/flows/finpay_topup_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml
```

### Documentation Tooling (Context7 MCP)

Up-to-date documentation for libraries (Kestra, Polars, pandas, psycopg, etc.) is fetched via the hosted [Context7](https://context7.com) MCP server. The server runs remotely over streamable HTTP, so no local process or `npm` install is required.

- Codex: registered in `.codex/config.toml` under `[mcp_servers.context7]` (remote `url = "https://mcp.context7.com/mcp"`).
- Other MCP clients (Cursor, Claude Code, Kilo, VS Code): registered in the root `.mcp.json` under `mcpServers.context7`.
- Both configs send `CONTEXT7_API_KEY` (when present) as a bearer token for higher rate limits and private-repo access. Anonymous access still works with reduced limits.

Cache docs locally:
```bash
# fetch docs for the whole stack (Kestra, Polars, pandas, psycopg, ...)
python scripts/context7_docs.py

# limit to a few libraries
python scripts/context7_docs.py --only kestra polars

# use an API key for higher rate limits
CONTEXT7_API_KEY=xxx python scripts/context7_docs.py
```

Cache stored in `docs/context7/` (gitignored). Treat as a convenience cache, not a source of truth; re-run the helper to refresh.

---

## Domain Documentation Ownership

| Document | Purpose |
|---|---|
| `README.md` | Repository setup, architecture, domain navigation |
| `AGENTS.md` | Repository boundaries, shared editing rules |
| `finpay_pipeline/README.md` | Canonical FinPay technical/operational guide |
| `finpay_pipeline/AGENTS.md` | Concise FinPay editing invariants |
| `linkaja_fee_pipeline/README.md` | Canonical LinkAja technical/operational guide |
| `linkaja_fee_pipeline/TAIPY_DASHBOARD_PLAN.md` | LinkAja Taipy architecture and reporting contract |
| `linkaja_fee_pipeline/AGENTS.md` | Concise LinkAja editing invariants |
| `linkaja_fee_pipeline/dbt/README.md` | LinkAja dbt learning setup and limitations |

Keep business rules in the owning domain guide. The root README remains an index and shared setup guide.

---

## Development Checks

Run root static contracts on host Python, then behavior tests in provisioned Python 3.11:

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

Validate changed Kestra YAML against the Compose-pinned Kestra version:

```bash
docker run --rm \
  -v "$PWD/finpay_topup_pipeline/finpay_topup_pipeline.yml:/flows/finpay_topup_pipeline.yml:ro" \
  -v "$PWD/config/kestra-validation.yml:/validation.yml:ro" \
  kestra/kestra:v1.3.26 \
  flow validate /flows --local --config /validation.yml
```

The validation configuration uses an in-memory H2 repository. It is not a production Kestra configuration. Before release, also validate server-side against the actual on-premises instance and run the full repository suite in a Python 3.11 environment containing both domains' dependencies.

---

## Documentation Tooling (Context7 MCP)

Up-to-date, version-specific documentation for the libraries used in this
project is fetched through the hosted [Context7](https://context7.com) MCP
server. The server runs remotely over streamable HTTP, so no local process or
`npm` install is required.

- Codex: registered in `.codex/config.toml` under `[mcp_servers.context7]`
  (remote `url = "https://mcp.context7.com/mcp"`).
- Other MCP clients (Cursor, Claude Code, Kilo, VS Code): registered in the
  root `.mcp.json` under `mcpServers.context7`.

Both configs send `CONTEXT7_API_KEY` (when present in the environment) as a
bearer token for higher rate limits and private-repo access. Anonymous access
still works with reduced limits.

An optional, npm-free helper wraps the two Context7 tools
(`resolve-library-id`, `query-docs`) with the MCP Python SDK and caches the
results:

```bash
# fetch docs for the whole stack (Kestra, Polars, pandas, psycopg, ...)
python scripts/context7_docs.py

# limit to a few libraries
python scripts/context7_docs.py --only kestra polars

# use an API key for higher rate limits
CONTEXT7_API_KEY=xxx python scripts/context7_docs.py
```

Cached results are written under `docs/context7/` (gitignored). Treat as a
convenience cache, not a source of truth; re-run the helper to refresh.
