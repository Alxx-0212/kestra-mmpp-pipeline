# FinPay Top-Up Agent Guide

Read [`README.md`](README.md) before changing Top-Up behavior. It is the
operating contract for checkpoints, staging, verification, manual adjustments,
outlet mappings, Kestra tasks, and Telegram-facing evidence.

## Scope

- Package/workflow: `finpay_topup_pipeline/**`
- Runtime image and dependencies: `Dockerfile`, `requirements-finpay-topup.txt`
- Flow: `workflows/finpay_topup_pipeline.yml`
- Tests: `tests/`

## Invariants

- Preserve `finance.finpay.finpay_topup_pipeline_v1`, trigger IDs, task IDs, secret names, image name, and input/output contracts.
- Stage first. Never commit source rows before verification and finance approval.
- Keep `finpay_topup_txn.row_hash` unique and use idempotent conflict handling.
- Treat checkpoints as opening boundaries. Do not move a newer checkpoint backward.
- Include only approved manual adjustments in saldo; never insert dummy rows into the immutable source ledger.
- Keep cluster outlet mappings explicit and reject/leave ambiguous source labels unclassified.
- Keep per-cluster extraction, chunk retry, validation, deduplication, verification, notification, and summary as separate responsibilities.
- Any live reset or repair requires stopped writers, a target guard, and an external checksum-verified backup.

Use [Top-Up validation](../docs/validation.md), [security](../docs/security.md), and [lane rules](../docs/agent-lanes.md) for shared procedures.
