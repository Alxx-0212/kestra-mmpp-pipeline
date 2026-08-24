# Telegram-Integrated Credit Classification Workflow (Kestra)

Plan file: `.kilo/plans/1787297095592-telegram-credit-classification.md`

## Goal

Automate daily credit top-up classification for the finance team: Telegram slash command triggers ingestion, bot presents unclassified transactions, finance classifies via inline-keyboard buttons only, results persist to `finpay_topup_classification`, group receives confirmation. No free-text data entry.

## Resolved Decisions

| Decision | Choice |
|---|---|
| Category model | **Fixed enum list** enforced in DB (lookup table + FK); buttons map 1:1 |
| Telegram update handler | **Dedicated bot service** (new container), mirrors existing `refresh_service.py` bridge pattern |
| HIL coupling | **Two decoupled flows**: ingestion/verification never waits on humans |
| Presentation UX | **Guided walkthrough**: one summary per cluster/cycle → sequential per-txn cards → Done |

## Architecture Overview

```
Telegram ──webhook──► Bot Service (FastAPI, new container)
                        │  validates chat/user whitelist, parses commands/callbacks
                        ├──── psycopg ──► PostgreSQL finpay DB (writes classification immediately)
                        └──── REST (basic auth, reuse refresh_service.py client code) ──► Kestra
                                                                                            │
Flow A finpay_topup_pipeline_v1 (Schedule 3–4×/day): extract → load → verify → checkpoint │
Flow B finpay_topup_classification_v1: query unclassified → notify group → Pause ─────────┘
                                          ▲        (bot resumes via POST .../resume)
                                          └── final task reads DB → confirmation message
```

**State ownership rule:** per-transaction pending/classified state lives **only in Postgres**
(`finpay_topup_txn LEFT JOIN finpay_topup_classification`). The Kestra Pause tracks *cycle*
completion only; the bot tracks *walkthrough position* in a DB-backed session row. No dual
state to drift.

## Analysis: Orchestration & State Management

Kestra handles the async human gap **natively**:

- `io.kestra.plugin.core.flow.Pause` halts execution; state is durably persisted in Kestra's
  Postgres metadata store — survives Kestra/bot restarts.
- Resume via REST: `POST /api/v1/{tenant}/executions/{executionId}/resume` (basic auth,
  same client pattern as `submit_kestra_refresh` in `refresh_service.py:58`).
- `timeout` on Pause bounds the wait (plan: PT12H); on timeout the flow takes a failure path
  that posts a reminder; `/pending` command can start a fresh cycle for stragglers.
- **No external AI agent / extra orchestration layer required.** The only external logic is
  the thin bot bridge (already decided).

## Analysis: Kestra vs n8n

| Concern | Kestra | n8n |
|---|---|---|
| Long-running HIL | Native Pause w/ durable state, timeouts, UI resume | Wait node / webhook-wait; works but execution state less observable at scale |
| Durable audit trail | Execution history + action log persisted in Postgres | Workflow run logs; adequate but lighter |
| Data pipeline fit | Already runs this repo's ingest/verify/checkpoint flows | Would duplicate or shadow existing Kestra flows |
| Telegram nodes | None native (HTTP Request to Bot API — already proven in `notify_unusual_telegram`) | First-class Telegram trigger/send/edit nodes |
| Dynamic multi-step keyboards | N/A either way — both need custom session logic | Same custom Code-node logic needed |
| Ops footprint | Zero new platform (already deployed here) | Second automation runtime to patch/secure |

**Verdict:** stay on Kestra + thin bot service. n8n's advantage (native Telegram nodes) does
not remove the custom keyboard/session work, and it adds a second runtime beside an existing
healthy orchestrator.

## Analysis: Communication Architecture

**Webhooks everywhere; no polling.**

1. Telegram → Bot: one webhook URL `POST /telegram/webhook` (public HTTPS, validated via
   `X-Telegram-Bot-Api-Secret-Token`). Set once via `setWebhook`. Sub-second latency;
   Telegram retries non-200s, so handler must be idempotent (dedupe on `update_id`).
2. Bot → Kestra: REST executions create + resume endpoints with existing basic-auth creds.
3. Bot → Postgres: direct `psycopg` writes using `upsert_classification`
   (`finpay_topup_pipeline/classification.py:19`) — idempotent ON CONFLICT upsert keyed by
   `txn_id`; `classified_by` records the Telegram username for audit.
4. Flow B → group: `io.kestra.plugin.core.http.Request` to Bot API `sendMessage`
   (pattern proven in `finpay_pipeline.yml`) or through the bot's internal `/announce`.

Local dev note: Telegram requires public HTTPS; use cloudflared/ngrok tunnel in dev, real
endpoint in prod.

## Analysis: UX/UI (button-only input surface)

Commands (whitelist-enforced by chat_id + user ID):

| Command | Behavior |
|---|---|
| `/process [start] [end]` | Submits Flow A via existing Kestra REST client; acks in-group |
| `/pending` | Starts/restarts a classification cycle for unclassified txns |
| `/status` | Last refresh status + unclassified counts per cluster |
| `/cancel` | Aborts active walkthrough session |
| `/help` | Command list |

Non-command text → fixed refusal reply ("buttons only"). No free-text capture anywhere.

Walkthrough interaction:

1. Summary card per cluster: `Cluster 421318 — 12 unclassified, Rp 3,155,000` + `[Classify]`.
2. Per-txn card via `edit_message_text` (no message flood): date, type, amount, remarks +
   one button per category + `⏭ Skip`.
3. `[Done ✅]` finishes early; bot writes nothing for skipped rows, then calls Kestra resume.
4. `callback_data` compact form, ≤64 bytes: `c:<session_seq>:<txn_id>:<cat_code>` /
   `n:next` / `n:skip` / `n:done`. Always `answerCallbackQuery` immediately.
5. Every press → instant DB upsert → next card. Crash-safe: session rebuilt from DB
   (remaining unclassified txns) on restart.

Category enum (initial proposal, confirm labels with finance before build):
`AGENT_TOPUP`, `CUSTOMER_REFUND`, `INTERNAL_TRANSFER`, `ADJUSTMENT`, `OTHER` — stored in
`finpay_class_category` lookup table; FK from `finpay_topup_classification.category`.

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Bot service | Python 3.11, FastAPI + uvicorn, `python-telegram-bot` 21 (async) | Matches repo Python pin; async webhook handling |
| DB driver | `psycopg[binary]` | Already used repo-wide |
| Orchestrator | Existing Kestra v1.3.26 | Already deployed; Pause/resume verified in docs |
| Flows | New `finpay_topup_classification.yml` + Schedule trigger added to Flow A | Declarative, Git-versioned |
| Deploy | New compose service on `mmpp-finance-network`; secrets `telegram_bot_token`, `telegram_webhook_secret` | Follows Docker-secrets convention |

## Implementation Tasks

1. **DB**: migration adding `finpay_class_category` lookup + CHECK/FK on
   `finpay_topup_classification.category`; seed enum rows. Update `schema.py` DDL +
   contract tests.
2. **Flow A**: add `io.kestra.plugin.core.trigger.Schedule` (e.g., `06:00, 11:00, 15:00, 19:00`
   Asia/Makassar — configurable) alongside the existing webhook trigger.
3. **Flow B** `finpay_topup_classification_v1`: query unclassified for cycle window →
   `If count > 0` → announce to group (HTTP Request) → `Pause(timeout PT12H)` → final task
   queries DB and posts confirmation + audit row.
4. **Bot service** `services/telegram_bot/`: FastAPI app — webhook receiver with
   secret-token check, update_id dedupe, chat/user whitelist, command router, callback
   router, session CRUD (Postgres-backed), Kestra client (lift from
   `refresh_service.py`), Telegram client wrapper.
5. **Compose/secrets**: add `telegram-bot` service, secrets files, `.env.example` entries;
   document `setWebhook` bootstrap step.
6. **Tests** (repo unittest style): callback parsing/auth-filter unit tests; disposable-DB
   integration test simulating a full cycle (seed txns → simulated callbacks → assert
   classifications + resume payload).
7. **Validation**: pinned-Kestra flow validation per README; E2E in staging group;
   idempotent rerun check (duplicate webhook replay must not double-write);
   `docker compose config --quiet`; full repo unittest suite.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Finance ignores Pause > 12h | Timeout path posts reminder; `/pending` reopens cycle; pipeline never blocked (decoupled) |
| Telegram webhook needs public HTTPS | Tunnel in dev; TLS reverse proxy in prod; secret-token header validation |
| Duplicate updates double-write | `update_id` dedupe cache + DB upsert idempotency |
| Group flooding | Single editable message per walkthrough; summary-only announcements |
| Lane boundaries (AGENTS.md) | Touches `docker-compose.yml` + new top-level dirs → integrating-agent lane; FinPay flow edits stay in FinPay lane |

## Out of Scope

- Grist dashboard widgets (separate Phase 3 effort)
- Free-text notes per classification (enum-only v1; note field remains CLI/Grist-editable)
- AI/NLP suggestion of categories
