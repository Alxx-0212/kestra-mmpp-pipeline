# Telegram Agent Guide

Read [`README.md`](README.md) before changing the Telegram interface.

## Scope

- Bot package: `services/telegram_bot/**`
- Runtime image: `Dockerfile`
- Dependencies: `requirements.txt`
- Tests: `tests/`

## Invariants

- Keep the allowlist fail-closed and scope every command/callback to authorized chats/users.
- Preserve command names and compact callback contracts, especially review actions and classification callbacks.
- All FinPay Top-Up review and technical alerts use `finpay_topup_bot`; never route inline approval buttons through another bot.
- Keep Telegram as an interface layer. Domain calculations, SQL, checkpoint logic, and deduplication belong to FinPay modules.
- Escape dynamic HTML before using `parse_mode=HTML`; keep messages under Telegram limits and use code formatting for financial values.
- Keep `/topupexcel` read-only: it may query FinPay and send an in-memory workbook but must not mutate data.
- Handle callback queries exactly once and return generic errors without secrets or raw database messages.

Use [shared validation](../../docs/validation.md), [security](../../docs/security.md), and [lane rules](../../docs/agent-lanes.md) for repository-wide procedures.
