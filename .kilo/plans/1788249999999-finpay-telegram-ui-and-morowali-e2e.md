# FinPay Telegram UI and Morowali E2E

## Decisions

- Use Bahasa Indonesia for all user-facing menus, alerts, and Kredit cards.
- Add the guided `/topup` command. Keep `/refresh cluster <id>` and `/refresh all`
  as strict advanced commands.
- Register `/start`, `/topup`, `/status`, `/kredit`, and `/help` with Telegram's
  scoped command catalog and chat menu button for chat `2142781580`.
- Use inline menus for Top Up scope, status scope, and Kredit date window.
- Use `⏳`, `✅`, `❌`, `⚠️`, and `ℹ️` only as status/action cues. Keep messages
  compact and put audit IDs/timestamps behind `Lihat detail`.
- Outlet assignment is the Kredit flag. Do not add a separate flag model or
  schema column. Use active predefined `finpay_outlet` rows only.
- `Lewati` is an explicit deferred decision. `Selesai` reports assigned,
  deferred, and remaining counts.
- `finpay_topup_bot` sends interactive review alerts and owns callbacks.
  `kestraLog_bot` remains one-way technical alerting only.

## Implementation

1. Add `BotCommand`, `BotCommandScopeChat`, and `MenuButtonCommands` registration
   during bot startup. Registration failures must be logged without exposing
   secrets.
2. Add `/start` home menu and `/topup`, `/status`, and `/kredit` scope callbacks.
   Callback data must remain compact, allowlisted, and authorization-checked.
3. Add compact Bahasa formatters for menus, refresh status, refresh details,
   Kredit cards, assignment feedback, and completion summaries.
4. Add emoji action labels to review keyboards while preserving `r:a:<id>` and
   `r:r:<id>` callback contracts. Keep `Lihat detail` behind `r:d:<id>`.
5. Make refresh review notifications use the webhook-owning FinPay bot token;
   retain the newline-stripping secret bootstrap. Keep technical failure alerts
   on the outbound alert bot.
6. Replace raw Kestra object prints with structured `event=topup_*` summaries.
7. Add tests for command registration, menus, callback scope, Indonesian IDR/date
   formatting, Kredit progress/defer behavior, details, stale callbacks, and
   idempotent review actions.

## E2E Reset and Run

1. Do not delete the PostgreSQL Docker volume or unrelated databases. Back up
   exactly the seven FinPay Top-Up tables and verify its checksum.
2. Stop mutation paths and guard every destructive SQL operation with
   `current_database() = 'finpay'`.
3. Clear the seven Top-Up tables, preserve the five non-Morowali checkpoints,
   and seed Morowali with `421318 / 2026-08-11 / 68,837,100` from the trusted
   Aug 10 boundary.
4. Start PostgreSQL, Kestra, `telegram-bot`, ngrok, and the webhook registrar.
   Verify ngrok HTTPS, `getMe`, `getWebhookInfo`, and the command catalog before
   sending a refresh command.
5. From the allowlisted chat, use `/start`, choose `Top Up`, choose `Morowali`,
   and verify the automatic checkpoint-to-current-date window, target-only
   staging, running saldo, CMS BUCKET, and zero difference.
6. Confirm the review alert appears from `finpay_topup_bot`, not `kestraLog_bot`.
   Wait for finance to click `✅ Setujui` or `❌ Tolak`; never auto-approve or use
   synthetic webhook updates.
7. Verify committed/rejected state, staging cleanup, ledger scope, checkpoint
   behavior, repeated-click idempotency, and structured Kestra logs.
8. On any failed gate, stop mutation paths and restore the checksum-verified
   seven-table backup. Record execution IDs, row counts, statuses, and webhook
   evidence outside the repository.

## Acceptance Criteria

- Slash command menu is visible in the scoped finance chat.
- The guided Top Up flow requires no date or user input.
- Review alerts are readable in one screen and identify the current action.
- Every Kredit card supports safe outlet assignment or explicit deferment.
- No interactive approval callback is sent by the wrong bot.
- No unrelated LinkAja, invoice, outlet, or PostgreSQL-volume data is deleted.
- Focused tests, Compose validation, Kestra validation, and the guarded live E2E
  all pass with exact executed/skipped counts recorded.
