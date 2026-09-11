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
