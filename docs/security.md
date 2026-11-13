# Security And Data Safety

- Never commit `.env`, `.env_encoded`, Docker secrets, credentials, tokens, private keys, local data, or generated artifacts.
- Keep real secrets out of documentation. Use placeholders and point to secret files or the secret manager.
- Verify `.dockerignore` before every image build; it must exclude secrets, data, caches, Git metadata, and generated local state.
- Parameterize SQL. Treat workbook cells, CSV fields, Telegram input, Kestra outputs, and environment values as untrusted data.
- Escape Telegram HTML and do not log raw authorization URLs, passwords, tokens, or unbounded source payloads.
- Destructive SQL must guard `current_database()`. Never delete production/shared PostgreSQL volumes. A dev-only volume reset requires fresh user approval for exact volumes and scope, stopped consumers, and a verified backup/restore plan; no-backup resets require explicit approval.
- Stop Kestra, Telegram, schedulers, and other mutation paths before live reset or checkpoint repair.
- Before any reset, repair, or destructive cleanup, require fresh, explicit authorization from the current user for the exact target, scope, operation, and backup plan. Create an external checksum-verified backup before live data reset, repair, or disposal; any no-backup exception requires fresh, explicit authorization from the current user for that consequence. [FinPay Section 6.1](../finpay_pipeline/PRODUCTION_REPLICATION.md#61-one-time-development-reset-exception) records historical approval only and grants no current or future authorization.
- Preserve immutable ledgers and source evidence. Use audited adjustments, reversals, or checkpoint procedures instead of ad hoc updates; Section 6.1 is historical and does not change normal application recovery rules.
- Keep approvals, rejections, manual adjustments, checkpoint overrides, and technical failures idempotent and attributable.
