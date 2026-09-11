# Security And Data Safety

- Never commit `.env`, `.env_encoded`, Docker secrets, credentials, tokens, private keys, local data, or generated artifacts.
- Keep real secrets out of documentation. Use placeholders and point to secret files or the secret manager.
- Verify `.dockerignore` before every image build; it must exclude secrets, data, caches, Git metadata, and generated local state.
- Parameterize SQL. Treat workbook cells, CSV fields, Telegram input, Kestra outputs, and environment values as untrusted data.
- Escape Telegram HTML and do not log raw authorization URLs, passwords, tokens, or unbounded source payloads.
- Destructive SQL must guard `current_database()` and must not delete the PostgreSQL Docker volume.
- Stop Kestra, Telegram, schedulers, and other mutation paths before live reset or checkpoint repair.
- Create an external checksum-verified backup before live data reset or repair.
- Preserve immutable ledgers and source evidence. Use audited adjustments, reversals, or checkpoint procedures instead of ad hoc updates.
- Keep approvals, rejections, manual adjustments, checkpoint overrides, and technical failures idempotent and attributable.
