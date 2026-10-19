# Validation Guide

## Baseline

Use Python 3.11 and the owning runtime image for behavior tests. Record executed
and skipped counts. Missing dependencies or a fully skipped domain is not a
passing validation result.

## Repository checks

```bash
python3.11 -m unittest -v \
  tests.test_repository_setup_contracts \
  tests.test_codex_lanes
python3.11 -m unittest discover -s tests
git diff --check
git diff --cached --check
docker compose config --quiet
```

## Domain checks

- FinPay reporting: `python3.11 -m unittest discover -s finpay_pipeline/tests`
- FinPay Top-Up: run `python -m unittest discover -s finpay_topup_pipeline/tests` inside `finpay-topup-pipeline:3.11`.
- Telegram: run `python -m unittest discover -s services/telegram_bot/tests` inside `mmpp-telegram-bot:local`.
- LinkAja: `python3.11 -m unittest discover -s linkaja_fee_pipeline/tests` with `linkaja_fee_pipeline/requirements.txt` installed.

## Database tests

Run truncating/integration tests only against a dedicated disposable database
with a code-level target guard. Never use `finpay`, `finpay_txn`, `linkaja`, or
`kestra` for destructive tests. Live reset/repair requires an external,
checksum-verified backup and stopped mutation paths.

## Kestra and release

Validate changed flows with the pinned on-premises Kestra version and
`config/kestra-validation.yml`; YAML parsing alone is insufficient. Build and
smoke-test the affected image, validate against the actual Kestra server, then
run representative dry-run and idempotent-rerun checks. Deploy non-destructively
and verify the deployed revision before running a live refresh.

## Local verification (2026-10-08)

Status: local implementation and regression verification complete for the
uncommitted FinPay/LinkAja fixes on baseline `1ffa8e7`; not committed, merged,
deployed, or production-approved. Existing unrelated workflow relocation and
infrastructure changes remain preserved in the worktree.

Superpowers MCP systematic-debugging, test-driven-development, review, and
verification guidance was used. No Kestra MCP tools were exposed in the session;
the pinned CLI and an isolated native Kestra server supplied the fallback.
Context7/official plugin references were used for thread isolation and runner
contracts. No installs, production credentials, or live business records were
needed.

| Check | Result |
|---|---|
| FinPay full suite in `finpay-pipeline:3.11`, networking disabled | 111 passed, 0 skipped. |
| LinkAja full suite in `linkaja-fee-pipeline:3.11` against isolated PostgreSQL 18.4 | 82 passed, 0 skipped; exact disposable-target guard enabled. |
| `tests.test_repository_setup_contracts` | 15 passed, including resolved mount paths and pinned Basic-auth initialization. |
| Five FinPay/LinkAja flows validated by Kestra `v1.3.26` | Accepted; CLI/input deprecations and local subflow-resolution/library warnings remain. |
| Both daily workflows on isolated native Kestra, two runs each | `SUCCESS`; database/Sheet/notification effects skipped. |
| Actual monthly writer scripts in synthetic native retry probes | Intended missing-key errors propagated to `FAILED` after 3 attempts each; retry intervals shortened only in the probes. |
| Rebuilt browser image, network-disabled real Chromium smoke | Six isolated sessions, 3 concurrent workers; exact IDs/literal text and XLSX validation passed. |
| Docker `MOUNT` regression | Mismatched paths reproduced missing script; identical absolute paths succeeded. |
| `docker compose config --no-env-resolution --quiet` and both diff checks | Passed. Secret-bearing `.env_encoded` resolution was intentionally excluded. |

Fresh domain commands (run from the repository root):

```bash
docker run --rm --pull never --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace \
  finpay-pipeline:3.11 python -m unittest discover -s finpay_pipeline/tests
docker run --rm --pull never --network "container:$TEST_POSTGRES_CONTAINER" \
  -e PYTHONDONTWRITEBYTECODE=1 -e LINKAJA_TEST_DSN="$DISPOSABLE_TEST_DSN" \
  -e LINKAJA_TEST_DATABASE_GUARD=linkaja_review_test \
  -v "$PWD:/workspace:ro" -w /workspace \
  linkaja-fee-pipeline:3.11 python -m unittest discover -s linkaja_fee_pipeline/tests
python3.11 -m unittest -v tests.test_repository_setup_contracts
docker compose config --no-env-resolution --quiet
```

The database command was run against a newly created network-isolated, tmpfs
test container with database `linkaja_review_test`. Both disposable PostgreSQL
and Kestra containers were stopped and removed after verification. Scratch
smokes are evidence of local execution, not deployment or durable infrastructure.

FinPay fix review was independent and found no remaining actionable regressions.
LinkAja had an independent initial audit; follow-up review hit its usage limit.
The lead reproduced and fixed its remaining corrected-reversal-reference issue
and performed the final LinkAja self-review. No human release approval is implied.

The full shared suite still fails on the absent legacy `.codex` configuration and
launcher fixtures (7 errors and 1 failure). Those unrelated tests were neither
silenced nor satisfied by creating new Codex tooling; the repository-wide suite
is not claimed green.

Remaining release checks: live portal throughput/throttling and native export
representations; Google HTTPS/production PostgreSQL reachability and DB/Sheet
parity; failed-task artifact recovery; shared API quota coordination across
separate flows/stacks; backups, rotated portal secret, and immutable image/flow
deployment. Upstream already-rounded numeric IDs cannot be reconstructed.
Keep the acquisition schedule disabled until the controlled production canary.
