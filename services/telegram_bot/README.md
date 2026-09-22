# FinPay Telegram Service

The `telegram-bot` service is the FinPay interface for scoped refreshes,
status, Kredit classification/reporting, review callbacks, and finance Excel
exports. It does not own FinPay calculations or schema.

## Commands

| Command | Purpose |
|---|---|
| `/start` | Open the scoped menu. |
| `/topup` | Guided Top-Up refresh menu for any configured region or all regions. |
| `/status` | View cluster/all-cluster saldo and refresh state. |
| `/kredit` | Review unclassified Kredit rows and assign mapped outlets. |
| `/kreditlist` | Read-only paginated Kredit list with outlet classification. |
| `/topupexcel` | Read-only finance-style XLSX export. |
| `/help` | Compact command help. |

`/topupexcel` supports `1`, `3`, `7`, or custom dates and one cluster or all
configured clusters:

```text
/topupexcel 3
/topupexcel all custom 2026-09-01 2026-09-04
/topupexcel cluster 421318 custom 2026-09-01 2026-09-04
```

The export contains one sheet per selected cluster with finance columns:
`Transaction Date`, `Sender`, `Receiver`, `Transaction Type`, `SETOR`,
`TOPUP`, `SALDO`, `Currency`, `Remarks`, `Outlet`, and `Source`. It uses the
latest checkpoint and approved manual adjustments, and is limited to 366 days,
200,000 rows, and 50 MB.

## Identity and webhook

`finpay_topup_bot` is the single bot for webhook ownership, review alerts, and
technical alerts. Local E2E uses the allowlisted private chat; production may
change the chat ID after the bot is added to the production group. Never place
tokens or chat secrets in this guide.

Selective Top-Up refresh is enabled by default in the production-like Compose
configuration; set `TELEGRAM_SELECTIVE_REFRESH_ENABLED=false` only for a
deliberate read-only bot deployment. The guided Top-Up menu lists all six
configured regions and explains that an all-region refresh uses each region's
own saved checkpoint. Review actions are explicit: `✅ Setujui & masukkan`
promotes staged rows into the ledger, while `❌ Tolak & hapus` discards only
that refresh's staged rows; a CMS mismatch is shown as a warning before these
actions. For an all-region refresh, approval moves only verified regions and
leaves incomplete or mismatched regions in staging for a later review.
When a mismatch is detected, the review alert also includes a read-only Excel
analysis workbook with daily coverage and staging rows. Finance does not enter
correction values in Telegram; corrections are handled by the restricted
Kestra operator flow.

## Validation

Run the bot tests inside the Python 3.11 bot image. Verify command registration,
webhook status, health, and an export smoke test without sending a refresh. See
[`../../docs/validation.md`](../../docs/validation.md) and
[`AGENTS.md`](AGENTS.md).
