# Repository Agent Guide

This repository is a monorepo with two separately packaged financial pipeline
domains. The local/on-premises stack may share infrastructure, so shared
runtime changes still require integration review.

| Domain | Runtime guide | Agent rules |
|---|---|---|
| FinPay | `finpay_pipeline/README.md` | `finpay_pipeline/AGENTS.md` |
| LinkAja | `linkaja_fee_pipeline/README.md` | `linkaja_fee_pipeline/AGENTS.md` |

Use these edit lanes when agents work concurrently. Paths not assigned to a
domain lane are integrator-owned by default:

| Lane | Files the lane may edit |
|---|---|
| FinPay agent | `finpay_pipeline/**`, `finpay_pipeline.yml`, `pipeline.py`, `pipeline_refactored.py`, `Dockerfile`, `requirements.txt`, `tests/test_refactor_contracts.py`, `tests/test_finpay_workflow_contracts.py` |
| LinkAja agent | `linkaja_fee_pipeline/**`, `linkaja_fee_pipeline.yml`, `linkaja_monthly_materialization.yml`, `linkaja_pipeline.py`, `Dockerfile.linkaja`, `requirements-linkaja.txt`, `tests/test_linkaja_*.py` |
| Integrating agent only | `AGENTS.md`, `README.md`, `.gitignore`, `.dockerignore`, `.codex/**`, `scripts/**`, `docker-compose.yml`, `.env*.example`, `config/**`, `tests/test_repository_setup_contracts.py`, `tests/test_codex_lanes.py` |

`finpay_pipeline/summary_sheets.py` stays in the FinPay lane even though it
creates the `LinkAja` worksheet and writes formulas that reference it. A LinkAja
agent that needs a different reference-sheet header, column, or formula contract
must hand the requested contract change to the integrating agent; the FinPay
owner implements and validates the FinPay-side adapter.

## Boundaries

- Keep FinPay and LinkAja business calculations inside their owning package.
- Shared infrastructure may remain at the repository root, but domain-specific
  SQL, documentation, migrations, and experiments belong under the domain.
- FinPay intentionally reads the shared `LinkAja` Google worksheet through
  invoice formulas. This is a reporting integration, not permission to move
  LinkAja calculations into FinPay. FinPay must create or repair that reference
  sheet before it writes formulas that depend on the sheet name and layout.
- Treat workflow IDs, task imports, Docker image names, database relations,
  worksheet names/layouts/formulas, secret names, and persisted artifact names
  as public contracts.
- Preserve unrelated worktree changes. This repository is often edited while
  files are already modified or untracked.
- Treat the domain README as the maintained operating contract and the workflow
  and code as runtime truth. Update documentation and implementation together
  whenever they disagree.
- The LinkAja dbt project is learning-only until Kestra integration is
  explicitly approved. Do not make production depend on it implicitly.
- File acquisition and scheduling are external today. Adding Hermes scraping,
  a cron replacement, or a Kestra trigger is a separate ingestion migration;
  do not mix it into a behavior-preserving calculation refactor.
- Shared documentation tooling is integrator-owned. The Context7 MCP server
  (remote, `https://mcp.context7.com/mcp`) is configured in `.codex/config.toml`
  and root `.mcp.json`, and the npm-free fetcher lives in `scripts/context7_docs.py`
  (caches results under `docs/context7/`). These are shared contracts; domain
  agents should not edit them.

## Multi-agent workflow

- Use `scripts/codex-lanes` from the clean `integration/mmpp-next` checkout to
  create and inspect the isolated FinPay and LinkAja worktrees. The launcher
  must never delete worktrees, merge or cherry-pick branches, deploy workflows,
  or run database migrations.
- Preserve an existing domain conversation by forking its Codex session UUID
  into the corresponding worktree. Do not resume one session concurrently in
  two checkouts. Session UUIDs are local operator state and must not be
  committed.
- Start with `git status --short` and read-only discovery. In the first progress
  update, declare one lane and the exact file allowlist before editing.
- Give each file to one editing agent at a time. Do not concurrently edit the
  same workflow, compatibility facade, schema, migration, or shared manifest.
- Use one integrating agent for root/shared contracts and cross-domain changes.
  Domain agents should not opportunistically edit the other domain.
- If work requires a file outside the declared lane, stop and send the proposed
  change to the integrating agent. Do not expand the lane implicitly.
- In a shared worktree, domain agents do not stage, commit, stash, reset, or
  clean. The integrating agent reviews and stages explicit paths after both
  domain handoffs. Agents using isolated Git worktrees may commit only their
  assigned lane.
- Integrate one domain handoff at a time, then update shared documentation and
  manifests. Run each domain's narrow checks before the final repository checks.
- Preserve public contracts unless the task explicitly includes a migration.
  Contract changes require an impact list, compatibility plan, validation, and
  rollback plan.
- Every handoff must list changed files, intentionally preserved contracts,
  requested cross-lane changes, checks run, failures, and exact executed/skip
  counts. Missing dependencies or skipped behavior tests are not a passing
  validation result.

## Production safety

- Use Python 3.11 with the relevant runtime dependencies for behavior tests;
  host-only syntax success is not equivalent to runtime validation.
- Run destructive database tests only against a dedicated disposable database
  with a code-level target guard. Never point them at production or shared
  development data.
- Before any Docker build, verify `.dockerignore` excludes local data, real
  `.env` files, credentials, Git metadata, caches, and generated artifacts.
- Validate changed flows against the same Kestra version used on-premises.
  YAML parsing alone does not validate task schemas, plugins, or templates.
- Keep production schema changes, dependency/runtime upgrades, workflow contract
  changes, and external-scraper migration as separate reviewable changes.

## Repository checks

Run the narrow domain checks first. Then run the repository checks in a fully
provisioned Python 3.11 environment:

```bash
python3 -m unittest -v \
  tests.test_repository_setup_contracts \
  tests.test_finpay_workflow_contracts \
  tests.test_codex_lanes
python3 -m unittest discover -s tests
git diff --check
git diff --cached --check
docker compose config --quiet
```

For a changed FinPay flow, run the pinned local Kestra validation documented in
`README.md`, then validate against the actual on-premises server before release.

Record the executed and skipped test counts. A suite that skipped a domain
because its runtime dependencies were unavailable does not validate that
domain. For a production release, also validate changed Kestra flows, build and
smoke-test the affected image, and run representative dry-run and idempotent
rerun checks before deployment.
