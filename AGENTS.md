# MMPP Monorepo Agent Guide

This repository contains separately packaged FinPay reporting, FinPay Top-Up,
Telegram, and LinkAja runtimes that share local infrastructure.

## Read first

- [Architecture and boundaries](docs/architecture.md)
- [Validation](docs/validation.md)
- [Dependencies](docs/dependencies.md)
- [Security and data safety](docs/security.md)
- [Git and agent lanes](docs/agent-lanes.md)
- [FinPay reporting guide](finpay_pipeline/README.md)
- [FinPay Top-Up guide](finpay_topup_pipeline/README.md)
- [Telegram guide](services/telegram_bot/README.md)
- [LinkAja guide](linkaja_fee_pipeline/README.md)

## Universal rules

- Keep domain calculations, schemas, migrations, and documentation in the owning package.
- Treat workflow IDs/task imports, image names, database relations, secret names, worksheet layouts, formulas, and artifact names as public contracts.
- Preserve unrelated dirty or untracked work; give each file one editing owner.
- Root/shared infrastructure and cross-domain changes belong to the integrating agent.
- Do not make production depend on the LinkAja dbt project until explicitly approved.
- Update the closest runtime README when implementation and operating behavior change together.

## Editing lanes

| Lane | Allowed paths |
|---|---|
| FinPay reporting | `finpay_pipeline/**`, related FinPay reporting tests |
| FinPay Top-Up | `finpay_topup_pipeline/**`, related Top-Up tests |
| Telegram | `services/telegram_bot/**`, related bot tests |
| LinkAja | `linkaja_fee_pipeline/**`, related LinkAja tests |
| Integrator | `AGENTS.md`, `README.md`, `INSTRUCTIONS.md`, `docs/**`, `.kilo/**`, `.codex/**`, `scripts/**`, `docker-compose.yml`, `config/**`, root tests |

Use [Git and agent lanes](docs/agent-lanes.md) for concurrent work and handoffs.
