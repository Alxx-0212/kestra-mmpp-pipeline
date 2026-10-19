# MMPP Monorepo Agent Guide

This repository contains separately packaged FinPay reporting, FinPay Top-Up,
Telegram, and LinkAja runtimes that share local infrastructure.

## Read By Task

Apply the universal rules and editing lanes below for every task. Read relevant
sections of these references when their trigger applies, not all runtime manuals:

- Before source, configuration, or runtime changes: [architecture and boundaries](docs/architecture.md) and [security/data safety](docs/security.md).
- Before assigning edits or parallel work: [Git and agent lanes](docs/agent-lanes.md).
- Before selecting, executing, or reporting checks: [validation](docs/validation.md).
- For dependency, manifest, Dockerfile, image, or shared-container changes: [dependency matrix](docs/dependencies.md).
- For FinPay reporting work: the closest package `AGENTS.md` and relevant [reporting guide](finpay_pipeline/README.md) sections.
- For FinPay Top-Up work: the closest package `AGENTS.md` and relevant [Top-Up guide](finpay_topup_pipeline/README.md) sections.
- For Telegram work: the closest package `AGENTS.md` and relevant [Telegram guide](services/telegram_bot/README.md) sections.
- For LinkAja work: the closest package `AGENTS.md` and relevant [LinkAja guide](linkaja_fee_pipeline/README.md) sections.

Cross-domain work must load all affected domains, not just the first one found.
Live data, credentials, destructive actions, reset/repair, and image builds always
require the security guide before acting, even during an otherwise read-only task.

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
