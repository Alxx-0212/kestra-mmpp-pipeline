# Agent Lanes And Ownership

Declare one lane and an exact file allowlist before editing. One agent owns each
workflow, schema/migration, compatibility facade, and shared manifest at a time.

| Lane | Allowed paths |
|---|---|
| FinPay reporting | `finpay_pipeline/**`, related reporting tests |
| FinPay Top-Up | `finpay_topup_pipeline/**`, related Top-Up tests |
| Telegram | `services/telegram_bot/**`, related bot tests |
| LinkAja | `linkaja_fee_pipeline/**`, related tests |
| Integrator | `AGENTS.md`, `README.md`, `INSTRUCTIONS.md`, `docs/**`, `.kilo/**`, `.codex/**`, `scripts/**`, `docker-compose.yml`, `config/**`, root tests |

Use `scripts/codex-lanes` only from a clean `integration/mmpp-next` checkout.
It creates and checks isolated worktrees but never merges, deploys, migrates, or
cleans them. Shared-worktree agents do not stage, commit, stash, reset, or clean.

Preserve unrelated dirty/untracked changes. Domain handoffs must list changed
files, preserved contracts, cross-lane requests, checks, failures, and exact
test executed/skipped counts. The integrating agent coordinates shared changes.
