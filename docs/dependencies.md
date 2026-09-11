# Dependency And Image Matrix

Each runtime installs its own manifest in a builder stage and copies only the
runtime virtual environment into the final image. Do not place domain
dependencies in a shared manifest unless the owning Dockerfile and
compatibility contract require it.

| Runtime | Manifest | Dockerfile/image |
|---|---|---|
| FinPay reporting | `finpay_pipeline/requirements.txt` | `finpay_pipeline/Dockerfile` / `finpay-pipeline:3.11` |
| FinPay Top-Up | `finpay_topup_pipeline/requirements-finpay-topup.txt` | `finpay_topup_pipeline/Dockerfile` / `finpay-topup-pipeline:3.11` |
| Telegram | `services/telegram_bot/requirements.txt` | `services/telegram_bot/Dockerfile` / `mmpp-telegram-bot:local` |
| LinkAja | `linkaja_fee_pipeline/requirements.txt` | `linkaja_fee_pipeline/Dockerfile` / `linkaja-fee-pipeline:3.11` |
| Superset/MCP | image-managed, optional profile | `services/superset/Dockerfile` / `mmpp-superset:6.1.0-postgres-mcp` |
| ngrok | vendor image | `ngrok/ngrok:3.39.8-debian` |

`finpay_pipeline/requirements.txt` is the FinPay reporting manifest.
`finpay_topup_pipeline/requirements-finpay-topup.txt` owns Kestra, PostgreSQL, requests,
OpenPyXL, and YAML parsing. Each Dockerfile installs only its package-local
manifest.
The Telegram manifest owns FastAPI/Uvicorn, python-telegram-bot, PostgreSQL, and
Excel export. LinkAja remains independent from FinPay and Telegram.

When a manifest changes, rebuild its image and run behavior tests inside that
image. Avoid dependency upgrades in the same change as a workflow/schema
migration unless the upgrade is required and separately documented.

All five Dockerfiles use a `builder` stage plus a minimal `runtime` stage.
Optional Superset is built only when the `optional-dashboard` Compose profile is
enabled.
